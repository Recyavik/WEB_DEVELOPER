"""
bridge.py — лёгкий релей WebSocket'ов между VEGAREX и 1Т REX.

Заменяет старый server.py (tkinter-GUI ~500 строк). Headless, ~140 строк.
Запускается из терминала:

    python bridge.py                       # только WiFi (порты 41235 / 8080)
    python bridge.py --com COM3            # + USB-Serial для ESP32
    python bridge.py --browser-port 41235 --esp-port 8080 --baud 38400

Архитектура:

    Browser/VEGAREX ──ws://*:41235──→ bridge ──ws://*:8080──→ ESP32 (WiFi)
                                        │
                                        └─── serial COM3@38400 ──→ ESP32 (USB)

Релей прозрачный: команды от VEGAREX (JSON {id, command, usb=…})
пересылаются на ESP32, ответы ESP32 — обратно. Если usb=true в команде
и --com задан → команда уходит по Serial. Иначе → по WebSocket.

Установка зависимостей на хосте:
    pip install websockets
    pip install pyserial-asyncio       # ТОЛЬКО если нужен USB-режим (--com)

Логи в stdout. Запусти и оставь окно открытым. Ctrl+C — остановка.
"""
import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime
from typing import Optional

import websockets

# Глушим шумные traceback'и websockets-библиотеки про невалидные
# TCP-пробы. /api/launch_bridge в VEGAREX делает plain-TCP коннект на
# порт 41235 чтобы проверить «жив ли мост», а это не WS-handshake.
# Библиотека websockets логирует это через logger.error("opening
# handshake failed", exc_info=True) — обычное подавление уровнем не
# помогает (ERROR-сообщения и так пропускаются). Поэтому ставим
# именованный фильтр, который дропает конкретно эти строки.
class _SilenceProbeNoise(logging.Filter):
    NOISY_FRAGMENTS = (
        "opening handshake failed",
        "did not receive a valid HTTP request",
        "InvalidMessage",
        "connection closed while reading HTTP request line",
    )
    def filter(self, record):
        try:
            msg = record.getMessage()
        except Exception:
            return True
        return not any(s in msg for s in self.NOISY_FRAGMENTS)

_websockets_logger = logging.getLogger("websockets")
_websockets_logger.addFilter(_SilenceProbeNoise())
# Подстраховка: некоторые версии используют дочерние логгеры
for _name in ("websockets.server", "websockets.asyncio.server",
              "websockets.protocol"):
    logging.getLogger(_name).addFilter(_SilenceProbeNoise())

try:
    import serial_asyncio                       # noqa
except ImportError:
    serial_asyncio = None


# ── Глобальное состояние моста ───────────────────────────────────────
state: dict = {
    "browser_ws":     None,   # одно подключение от VEGAREX (или браузера)
    "esp32_ws":       None,   # одно подключение от ESP32 по WiFi
    "serial_w":       None,   # writer USB-Serial
    "serial_r":       None,   # reader USB-Serial
    "pending_serial": [],     # FIFO id команд, ожидающих ответа по Serial
}


def log(tag: str, msg: str) -> None:
    t = datetime.now().strftime("%H:%M:%S")
    print(f"[{t}] [{tag}] {msg}", flush=True)


# ── WebSocket: обработчик подключения VEGAREX ────────────────────────
async def browser_handler(ws):
    """VEGAREX (или браузер) подключается сюда — порт 41235 по умолчанию.
    Пересылает команды на ESP32: по Serial (если usb=true и порт открыт)
    или по WiFi-WebSocket."""
    if state["browser_ws"]:
        try: await state["browser_ws"].close()
        except Exception: pass
    state["browser_ws"] = ws
    log("BROWSER", f"подключён: {ws.remote_address}")
    try:
        async for raw in ws:
            try:
                data = json.loads(raw)
                cmd_id = data.get("id")
                command = data.get("command", "")
                use_usb = str(data.get("usb", "")).lower() == "true"

                if use_usb and state["serial_w"] is not None:
                    log("→USB", command)
                    state["pending_serial"].append(cmd_id)
                    state["serial_w"].write(f"{command}\n".encode())
                    await state["serial_w"].drain()
                elif state["esp32_ws"] is not None:
                    log("→WiFi", command)
                    await state["esp32_ws"].send(raw)
                else:
                    log("DROP", f"нет канала к ESP32: {command}")
            except Exception as e:
                log("ERR", f"browser msg: {e}")
    finally:
        state["browser_ws"] = None
        log("BROWSER", "отключился")


# ── WebSocket: обработчик подключения ESP32 ──────────────────────────
async def esp32_handler(ws):
    """ESP32 подключается сюда по WiFi — порт 8080 по умолчанию.
    Пересылает ответы и события в VEGAREX as-is."""
    if state["esp32_ws"]:
        try: await state["esp32_ws"].close()
        except Exception: pass
    state["esp32_ws"] = ws
    log("ESP32", f"подключён: {ws.remote_address}")
    try:
        async for raw in ws:
            log("WiFi→", raw[:120])
            if state["browser_ws"] is not None:
                await state["browser_ws"].send(raw)
    finally:
        state["esp32_ws"] = None
        log("ESP32", "отключился")


# ── Serial: чтение ответов ESP32 и пересылка в VEGAREX ───────────────
async def serial_reader_loop():
    while True:
        try:
            line = await state["serial_r"].readline()
            text = line.decode("utf-8", errors="ignore").strip()
            if not text:
                continue
            log("USB→", text)
            if state["pending_serial"] and state["browser_ws"] is not None:
                cmd_id = state["pending_serial"].pop(0)
                await state["browser_ws"].send(
                    json.dumps({"id": cmd_id, "value": text}))
        except Exception as e:
            log("ERR", f"serial: {e}")
            break


# ── Запуск ──────────────────────────────────────────────────────────
async def main(args):
    log("INFO", "VEGAREX Bridge стартует:")
    log("INFO", f"  Browser WS:  ws://0.0.0.0:{args.browser_port}")
    log("INFO", f"  ESP32 WS:    ws://0.0.0.0:{args.esp_port}")

    if args.com:
        if serial_asyncio is None:
            log("ERR", "pyserial-asyncio не установлен. "
                       "Поставь: pip install pyserial-asyncio")
            return
        try:
            r, w = await serial_asyncio.open_serial_connection(
                url=args.com, baudrate=args.baud)
            state["serial_r"], state["serial_w"] = r, w
            asyncio.create_task(serial_reader_loop())
            log("INFO", f"  USB-Serial:  {args.com} @ {args.baud}")
        except Exception as e:
            log("ERR", f"COM-порт не открыт: {e}")
    else:
        log("INFO", "  USB-Serial:  не задан (--com PORT для USB-режима)")

    async with websockets.serve(browser_handler, "0.0.0.0", args.browser_port), \
               websockets.serve(esp32_handler,   "0.0.0.0", args.esp_port):
        log("INFO", "Готов. Ctrl+C для остановки.")
        await asyncio.Future()


def list_serial_ports():
    """Печатает доступные COM-порты и выходит. Помогает найти валидный
    --com PORT перед запуском моста (если COM3 не существует — увидите)."""
    try:
        import serial.tools.list_ports as lp
    except ImportError:
        print("Для --list-ports нужен pyserial: pip install pyserial")
        sys.exit(1)
    ports = list(lp.comports())
    if not ports:
        print("COM-порты не найдены. Подключите устройство по USB и повторите.")
        return
    print(f"Найдено COM-портов: {len(ports)}")
    for p in ports:
        desc = (p.description or "").strip()
        print(f"  {p.device:10}  {desc}")
    print("\nЗапустите: python bridge.py --com <PORT>")


if __name__ == "__main__":
    p = argparse.ArgumentParser(
        description="VEGAREX Bridge — релей WebSocket'ов VEGAREX↔ESP32 (1Т REX)")
    p.add_argument("--browser-port", type=int, default=41235,
                   help="WebSocket-порт для VEGAREX/браузера (по умолчанию 41235)")
    p.add_argument("--esp-port", type=int, default=8080,
                   help="WebSocket-порт для ESP32 по WiFi (по умолчанию 8080)")
    p.add_argument("--com", type=str, default=None,
                   help="COM-порт ESP32 для USB-Serial режима. "
                        "Пример: --com COM3 (Windows) или --com /dev/ttyUSB0 (Linux). "
                        "Список доступных портов — флаг --list-ports.")
    p.add_argument("--baud", type=int, default=38400,
                   help="Скорость Serial (по умолчанию 38400)")
    p.add_argument("--list-ports", action="store_true",
                   help="Показать доступные COM-порты и выйти")
    args = p.parse_args()
    if args.list_ports:
        list_serial_ports()
        sys.exit(0)
    try:
        asyncio.run(main(args))
    except KeyboardInterrupt:
        print("\n[INFO] Остановлено пользователем.")
        sys.exit(0)
