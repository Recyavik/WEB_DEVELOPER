"""
fake_robot.py — фейк-ESP32 для тестирования цепочки VEGAREX ↔ bridge.py
без реального 1Т REX.

Подключается к bridge.py на порт 8080 как настоящий ESP32 по WiFi:
читает команды, генерирует разумные ответы на запросы данных
(LASER?, COLOR?, BpE/ping), глушит fire-and-forget (MOVE, ANGLE, RGB
и др.). В логах видно весь поток команд от VEGAREX.

Зависимости: pip install websockets

Использование:
    python bridge.py            # терминал 1 — мост (порты 41235, 8080)
    python fake_robot.py        # терминал 2 — фейк-ESP32

После этого в VEGAREX (в Docker или нативно) поставьте
`ws://host.docker.internal:41235` или `ws://127.0.0.1:41235` —
индикатор станет «● Онлайн», команды будут видны и в логе моста,
и в логе фейк-робота.
"""
import argparse
import asyncio
import json
import random
import sys
from datetime import datetime

import websockets


def log(tag: str, msg: str) -> None:
    t = datetime.now().strftime("%H:%M:%S")
    print(f"[{t}] [{tag}] {msg}", flush=True)


async def handle_message(ws, msg_id, command: str) -> None:
    """Эмуляция ответа ESP32 на одну команду."""
    cmd_upper = command.upper().split(":")[0].strip()
    response_value = None

    if cmd_upper == "LASER?":
        # Случайное расстояние от 30 до 300 см
        response_value = f"LASER:{random.randint(30, 300)}"
    elif cmd_upper == "COLOR?":
        response_value = "128,128,128"
    elif cmd_upper == "BPE":
        response_value = "pong"
    elif cmd_upper in ("MOVE", "ANGLE", "STOP", "RGB", "MPU", "START"):
        # Fire-and-forget — реальный ESP32 тоже не отвечает на эти команды.
        # Просто логируем и молчим.
        return
    else:
        log("WARN", f"неизвестная команда: {command}")
        return

    payload = {"id": msg_id, "value": response_value}
    await ws.send(json.dumps(payload))
    sid = (str(msg_id) or "")[:8]
    log("→VEGA", f"id={sid}…  value={response_value}")


async def main(url: str):
    log("INFO", f"Фейк-ESP32 стартует. Bridge: {url}")
    while True:
        try:
            async with websockets.connect(url) as ws:
                log("INFO", "Подключён к bridge.py. Жду команды от VEGAREX…")
                async for raw in ws:
                    try:
                        data = json.loads(raw)
                        cid = data.get("id")
                        cmd = data.get("command", "")
                        sid = (str(cid) or "")[:8]
                        log("VEGA→", f"id={sid}…  cmd={cmd}")
                        await handle_message(ws, cid, cmd)
                    except json.JSONDecodeError:
                        log("WARN", f"не JSON: {raw[:80]}")
                    except Exception as e:
                        log("ERR", f"{e}")
        except (ConnectionRefusedError, OSError) as e:
            log("ERR", f"bridge.py недоступен ({url}): {e}")
            log("INFO", "Повтор через 3 секунды…")
            await asyncio.sleep(3)
        except websockets.exceptions.ConnectionClosed:
            log("WARN", "соединение закрыто, переподключаюсь через 1 сек…")
            await asyncio.sleep(1)


if __name__ == "__main__":
    p = argparse.ArgumentParser(
        description="VEGAREX Fake ESP32 — фейк-робот для теста без железа")
    p.add_argument("--url", default="ws://localhost:8080",
                   help="WebSocket-адрес ESP32-порта bridge.py "
                        "(по умолчанию ws://localhost:8080)")
    args = p.parse_args()
    try:
        asyncio.run(main(args.url))
    except KeyboardInterrupt:
        print("\n[INFO] Остановлено пользователем.")
        sys.exit(0)
