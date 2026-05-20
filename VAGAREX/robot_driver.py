"""
robot_driver.py — драйвер 1T REX

RexDriver     — реальный робот через WiFi/TCP (REX Board)
BridgeDriver  — реальный робот через server.py (WebSocket-мост браузер⇄ESP32)
SimDriver     — симулятор для отладки без железа

Документированные команды 1T REX Python API:
  robot.start(interval=10)       — инициализация, интервал опроса в мс
  robot.move(value, timeout=None)— движение (-100..100), timeout в секундах
  robot.invert_move()            — инвертировать направление motor
  robot.stop()                   — остановка
  robot.set_angle(value)         — руль -45..45°
  robot.set_servo_center()       — руль 0
  robot.enable_mpu(timeout=5)    — включить гироскоп
  robot.get_angle() / get_angles() — yaw/pitch/roll (Z/X/Y)
  robot.get_laser()              — дальность до препятствия
  robot.get_color()              — массив словарей TCA/R/G/B/C
  robot.init_led(addr,timeout=3) — инициализировать LED-модуль
  robot.set_rgb(idx,(R,G,B),delay=1.2) — RGB, delay в секундах
  robot.leds / leds[i] / leds.bind(list) — адреса светодиодов
  robot.init_sound(addr,timeout=2) / set_sound(0..100) — звук
  robot.set_timeout(sec) / set_multiplexer_channel(n) — служебное
  robot.send_command(str)        — отправка raw-команды
"""
import asyncio
import json
import logging
import uuid
from typing import Optional

log = logging.getLogger(__name__)


class RexDriver:
    """Асинхронный TCP-клиент к REX Board (WiFi)."""

    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port
        self.connected = False
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._lock = asyncio.Lock()

    async def connect(self) -> bool:
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port), timeout=5.0
            )
            self.connected = True
            log.info("REX connected at %s:%d", self.host, self.port)
            return True
        except Exception as e:
            self.connected = False
            log.warning("REX connect failed: %s", e)
            return False

    async def disconnect(self):
        self.connected = False
        if self._writer:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass

    async def _send(self, cmd: str) -> bool:
        if not self.connected or not self._writer:
            return False
        async with self._lock:
            try:
                self._writer.write((cmd + "\n").encode())
                await self._writer.drain()
                return True
            except Exception as e:
                log.error("REX send error: %s", e)
                self.connected = False
                return False

    async def _recv(self, timeout: float = 0.5) -> Optional[str]:
        if not self._reader:
            return None
        try:
            line = await asyncio.wait_for(self._reader.readline(), timeout=timeout)
            return line.decode().strip()
        except Exception:
            return None

    # ── Команды движения ─────────────────────────────────────────────────────

    async def move(self, speed: int) -> bool:
        """speed: -100..100, 0 = стоп"""
        speed = max(-100, min(100, speed))
        return await self._send(f"MOVE:{speed}")

    async def set_angle(self, angle: int) -> bool:
        """angle: -45..45, 0 = прямо"""
        angle = max(-45, min(45, angle))
        return await self._send(f"ANGLE:{angle}")

    async def set_servo_center(self) -> bool:
        return await self._send("ANGLE:0")

    async def stop(self) -> bool:
        ok1 = await self._send("MOVE:0")
        ok2 = await self._send("ANGLE:0")
        return ok1 and ok2

    # ── Датчики ──────────────────────────────────────────────────────────────

    async def get_laser(self) -> Optional[float]:
        await self._send("LASER?")
        resp = await self._recv(timeout=0.5)
        if resp and ":" in resp:
            try:
                return float(resp.split(":")[-1])
            except ValueError:
                pass
        return None

    async def get_color(self) -> Optional[tuple]:
        await self._send("COLOR?")
        resp = await self._recv(timeout=0.5)
        if resp:
            try:
                r, g, b = map(int, resp.split(","))
                return r, g, b
            except Exception:
                pass
        return None

    # ── Система ──────────────────────────────────────────────────────────────

    async def enable_mpu(self) -> bool:
        return await self._send("MPU:1")

    async def start(self) -> bool:
        return await self._send("START")

    async def set_rgb(self, index: int, color: tuple, delay: float = 1.2) -> bool:
        """API 1T REX: robot.set_rgb(index, (R,G,B), delay=1.2).
        delay — задержка между установкой каналов в секундах
        (0.0 = мгновенно, default 1.2 = плавный fade)."""
        r, g, b = color
        return await self._send(f"RGB:{index},{r},{g},{b},{delay}")

    async def ping(self) -> bool:
        await self._send("BpE")
        resp = await self._recv(timeout=1.0)
        return bool(resp and "pong" in resp.lower())


class BridgeDriver:
    """Реальный 1Т REX через мост server.py (WebSocket браузер⇄ESP32).

    Тот же интерфейс, что у RexDriver, но команды идут не прямым TCP,
    а как JSON {id, command, usb=false} на server.py:41235, который
    транслирует их на ESP32 (через WiFi или USB-Serial — выбор в GUI
    server.py). Командный словарь идентичен RexDriver:
        MOVE:N (-100..100), ANGLE:N (-45..45),
        LASER? → "LASER:NNN" или "NNN",
        RGB:i,r,g,b,delay, MPU:1, START, BpE (ping)

    Используется, если в настройках указан host вида ws://… (см. make_driver).
    """

    def __init__(self, url: str):
        self.url = url
        self.connected = False
        self._ws = None
        self._pending: dict[str, asyncio.Future] = {}
        self._receiver_task: Optional[asyncio.Task] = None
        self._send_lock = asyncio.Lock()

    async def connect(self) -> bool:
        # Локальный импорт — не тянем websockets, если не используется.
        try:
            import websockets
        except ImportError:
            log.error("Bridge driver requires `websockets` package")
            return False
        try:
            self._ws = await asyncio.wait_for(
                websockets.connect(self.url), timeout=5.0)
            self.connected = True
            self._receiver_task = asyncio.create_task(self._receiver())
            log.info("Bridge connected to %s", self.url)
            return True
        except Exception as e:
            self.connected = False
            log.warning("Bridge connect failed: %s", e)
            return False

    async def _receiver(self):
        """Читает ответы от server.py и будит ожидающие future по id."""
        try:
            async for raw in self._ws:
                try:
                    data = json.loads(raw)
                    cid = data.get("id")
                    fut = self._pending.get(cid)
                    if fut and not fut.done():
                        fut.set_result(data.get("value"))
                except Exception as e:
                    log.error("Bridge receiver parse error: %s", e)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            log.warning("Bridge connection lost: %s", e)
        finally:
            self.connected = False

    async def disconnect(self):
        self.connected = False
        if self._receiver_task:
            self._receiver_task.cancel()
            try:
                await self._receiver_task
            except Exception:
                pass
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass

    async def _send(self, command: str) -> bool:
        """Fire-and-forget команда: отправили — не ждём ответ."""
        if not self.connected or not self._ws:
            return False
        cid = str(uuid.uuid4())
        msg = json.dumps({"id": cid, "command": command, "usb": False})
        try:
            async with self._send_lock:
                await self._ws.send(msg)
            return True
        except Exception as e:
            log.error("Bridge send error: %s", e)
            self.connected = False
            return False

    async def _send_with_response(self, command: str,
                                   timeout: float = 1.0) -> Optional[str]:
        """Команда с ожиданием ответа. server.py пересылает ответ ESP32
        обратно как JSON {id, value} — мы сопоставляем по id."""
        if not self.connected or not self._ws:
            return None
        cid = str(uuid.uuid4())
        loop = asyncio.get_event_loop()
        future: asyncio.Future = loop.create_future()
        self._pending[cid] = future
        msg = json.dumps({"id": cid, "command": command, "usb": False})
        try:
            async with self._send_lock:
                await self._ws.send(msg)
            return await asyncio.wait_for(future, timeout=timeout)
        except Exception as e:
            log.warning("Bridge response timeout/err for %s: %s", command, e)
            return None
        finally:
            self._pending.pop(cid, None)

    # ── Команды движения ─────────────────────────────────────────────────────

    async def move(self, speed: int) -> bool:
        return await self._send(f"MOVE:{max(-100, min(100, speed))}")

    async def set_angle(self, angle: int) -> bool:
        return await self._send(f"ANGLE:{max(-45, min(45, angle))}")

    async def set_servo_center(self) -> bool:
        return await self._send("ANGLE:0")

    async def stop(self) -> bool:
        ok1 = await self._send("MOVE:0")
        ok2 = await self._send("ANGLE:0")
        return ok1 and ok2

    # ── Датчики ──────────────────────────────────────────────────────────────

    async def get_laser(self) -> Optional[float]:
        resp = await self._send_with_response("LASER?", timeout=0.5)
        if resp is None:
            return None
        try:
            s = str(resp)
            return float(s.split(":")[-1] if ":" in s else s)
        except (ValueError, TypeError):
            return None

    async def get_color(self) -> Optional[tuple]:
        resp = await self._send_with_response("COLOR?", timeout=0.5)
        if not resp:
            return None
        try:
            r, g, b = map(int, str(resp).split(","))
            return r, g, b
        except Exception:
            return None

    # ── Система ──────────────────────────────────────────────────────────────

    async def enable_mpu(self) -> bool:
        return await self._send("MPU:1")

    async def start(self) -> bool:
        return await self._send("START")

    async def set_rgb(self, index: int, color: tuple, delay: float = 1.2) -> bool:
        r, g, b = color
        return await self._send(f"RGB:{index},{r},{g},{b},{delay}")

    async def ping(self) -> bool:
        resp = await self._send_with_response("BpE", timeout=1.0)
        return bool(resp and "pong" in str(resp).lower())


class SimDriver:
    """Симулятор 1T REX для разработки без физического робота."""

    def __init__(self):
        self.connected = True
        self.speed     = 0
        self.angle     = 0
        self._laser_cm = 200.0

    async def connect(self) -> bool:
        self.connected = True
        return True

    async def disconnect(self):
        self.connected = False

    async def move(self, speed: int) -> bool:
        self.speed = max(-100, min(100, speed))
        return True

    async def set_angle(self, angle: int) -> bool:
        self.angle = max(-45, min(45, angle))
        return True

    async def set_servo_center(self) -> bool:
        self.angle = 0
        return True

    async def stop(self) -> bool:
        self.speed = 0
        self.angle = 0
        return True

    async def get_laser(self) -> float:
        return self._laser_cm

    async def get_color(self) -> tuple:
        return 128, 128, 128

    async def enable_mpu(self) -> bool:
        return True

    async def start(self) -> bool:
        return True

    async def set_rgb(self, index: int, color: tuple, delay: float = 1.2) -> bool:
        return True

    async def ping(self) -> bool:
        return True


def make_driver(simulation: bool, host: str, port: int):
    """Возвращает драйвер по типу URL/host'а.

    simulation=True               → SimDriver (виртуальный)
    host начинается с ws:// или wss:// → BridgeDriver (через server.py)
    иначе                         → RexDriver (прямой TCP к REX Board)
    """
    if simulation:
        return SimDriver()
    if host and host.startswith(("ws://", "wss://")):
        return BridgeDriver(host)
    return RexDriver(host, port)
