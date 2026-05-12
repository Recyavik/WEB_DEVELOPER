"""
robot_driver.py — драйвер 1T REX

RexDriver  — реальный робот через WiFi/TCP (REX Board)
SimDriver  — симулятор для отладки без железа

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
import logging
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
    if simulation:
        return SimDriver()
    return RexDriver(host, port)
