"""
Сборка для реального 1Т REX — самодостаточный Python.

Высокоуровневые методы (forward, back, arc, face, goto, ...) реализованы
ниже в классе Robot. Они используют ТОЛЬКО низкоуровневое API,
которое предоставляет мост к железу:

    robot.set_angle(deg)            угол сервы −45…+45
    robot.move(pct)                 % мощности мотора −100…+100
    robot.stop()                    мгновенно остановить мотор
    robot.set_rgb(i, (r,g,b), dly)  цвет LED
    robot.get_distance() → cm       лазерный/ультразвуковой дальномер
    robot.x, robot.y, robot.heading свойства dead-reckoning (read-only)

Диагностические print() внутри класса Robot (_clamp_speed, _zone_blocked,
show_danger_zones, remove_zone, recharge) на MicroPython/ESP32 уходят в
UART — открой монитор последовательного порта (Thonny / Arduino Serial /
minicom) и увидишь сообщения. На железе это полезный канал отладки.

ВЫБОР МОСТА (внизу файла):
  • robot = _LiveRobot()    — заглушка для теста кода БЕЗ робота
  • robot = _NetworkRobot() — боевой режим через server.py

Боевой режим требует:
  pip install websocket-client
  Запущенный server.py (рядом с прошивкой) на 127.0.0.1:41235
  Подключённый ESP32 — WiFi или USB-Serial (выбор в GUI server.py)

Зоны опасности — НЕ no-op: программа держит список (x, y, radius) и
во время движения tick-проверяет позицию. Координаты совпадают с
реальными препятствиями на поле; программа сама останавливается, если
въезжает в любую зону. `remove_zone(x, y)` снимает зону (= обучающийся
физически убрал препятствие). `attention_here` помечает координаты «не
посещать» (информативный список, не блокирует движение).

Калибровка повторяет config.py симулятора. Подстрой константы под
конкретную машину.
"""
import math
import time


class Robot:
    """Высокоуровневый фасад над низкоуровневым мостом."""

    # ── Калибровка (см. config.py симулятора) ──────────────────────────
    MOVE_SPEED          = 40    # %, скорость по умолчанию для прямых
    TURN_ANGLE          = 36    # °, угол руля по умолчанию (1..45)
    TURN_SPEED_REF      = 40    # %, опорная скорость дуг
    WHEEL_CIRC_CM       = 28.3  # см, длина окружности колеса (D90)
    HEADING_DEG_PER_ROT = 25.0  # °, sweep курса за один оборот колеса
    SPEED_AT_100        = 80.0  # см/с при 100% мощности
    WALL_STOP_CM        = 12.0  # стоп перед стеной (forward_to_wall)
    MIN_SPEED_PCT       = 30    # минимум — ниже мотор не тянет
    ROBOT_SAFETY_CM     = 10.0  # запас вокруг корпуса при проверке зон
    ZONE_TICK_SEC       = 0.05  # частота проверки зон во время движения

    def __init__(self):
        # Список опасных зон: [(x, y, radius), …]. Робот их сам не ставит —
        # они задаются `load_danger_zones([(x, y, r), …])` ДО старта программы
        # или при движении НЕ заходит в радиус (тик-проверка).
        self.danger_zones = []
        # Зоны внимания — информативные пометки (не блокируют движение).
        # Заполняются `attention_here(r)` / `attention_zone(x, y, r)`.
        self.attention_zones = []

    # ── Внутренние помощники ───────────────────────────────────────────
    def _clamp_speed(self, spd):
        sign = -1 if spd < 0 else 1
        mag = abs(int(spd))
        if 0 < mag < self.MIN_SPEED_PCT:
            print(f"WARN: скорость {spd}% поднята до {sign*self.MIN_SPEED_PCT}%")
            return sign * self.MIN_SPEED_PCT
        return int(spd)

    def _arc_time(self, deg, steer_abs):
        """Время на дугу `deg` градусов курса при руле `steer_abs`."""
        spd = self.TURN_SPEED_REF
        cm_per_s = (spd / 100.0) * self.SPEED_AT_100
        if cm_per_s <= 0:
            return 0
        steer_ratio = max(1, steer_abs) / 45.0
        arc_len_cm = abs(deg) * self.WHEEL_CIRC_CM / (self.HEADING_DEG_PER_ROT * steer_ratio)
        return arc_len_cm / cm_per_s

    def _zone_blocked(self, x, y):
        """Если (x, y) попадает в любую опасную зону с учётом ROBOT_SAFETY_CM —
        возвращает (zx, zy, zr) этой зоны, иначе None."""
        for zx, zy, zr in self.danger_zones:
            if math.hypot(x - zx, y - zy) < zr + self.ROBOT_SAFETY_CM:
                return (zx, zy, zr)
        return None

    def _drive_with_zone_check(self, spd, duration):
        """Едет на скорости `spd` (со знаком) `duration` секунд, проверяя
        позицию против опасных зон каждые ZONE_TICK_SEC. При въезде в
        зону мотор глушится, печатается предупреждение, метод выходит."""
        self.move(spd)
        elapsed = 0.0
        try:
            while elapsed < duration:
                step = min(self.ZONE_TICK_SEC, duration - elapsed)
                time.sleep(step)
                elapsed += step
                zone = self._zone_blocked(self.x, self.y)
                if zone:
                    print(f"⛔ Опасная зона ({zone[0]:.0f}, {zone[1]:.0f}) "
                          f"r={zone[2]:.0f} — стоп.")
                    return False
        finally:
            self.move(0)
        return True

    # ── Прямое движение ────────────────────────────────────────────────
    def forward(self, dist_cm, speed=None):
        """Едет вперёд на dist_cm с проверкой опасных зон."""
        spd = self._clamp_speed(speed if speed is not None else self.MOVE_SPEED)
        cm_per_s = (abs(spd) / 100.0) * self.SPEED_AT_100
        if cm_per_s <= 0:
            return
        self.set_angle(0)
        self._drive_with_zone_check(spd, dist_cm / cm_per_s)

    def back(self, dist_cm, speed=None):
        spd = self._clamp_speed(speed if speed is not None else self.MOVE_SPEED)
        cm_per_s = (abs(spd) / 100.0) * self.SPEED_AT_100
        if cm_per_s <= 0:
            return
        self.set_angle(0)
        self._drive_with_zone_check(-spd, dist_cm / cm_per_s)

    def forward_to_wall(self, speed=None):
        """Едет вперёд пока дальномер не покажет ≤ WALL_STOP_CM (или въезд в зону)."""
        spd = self._clamp_speed(speed if speed is not None else self.MOVE_SPEED)
        self.set_angle(0)
        self.move(spd)
        try:
            while self.get_distance() > self.WALL_STOP_CM:
                time.sleep(self.ZONE_TICK_SEC)
                zone = self._zone_blocked(self.x, self.y)
                if zone:
                    print(f"⛔ Опасная зона ({zone[0]:.0f}, {zone[1]:.0f}) — стоп.")
                    return
        finally:
            self.move(0)

    def backward_to_wall(self, speed=None):
        spd = self._clamp_speed(speed if speed is not None else self.MOVE_SPEED)
        self.set_angle(0)
        self.move(-spd)
        try:
            while self.get_distance() > self.WALL_STOP_CM:
                time.sleep(self.ZONE_TICK_SEC)
                zone = self._zone_blocked(self.x, self.y)
                if zone:
                    print(f"⛔ Опасная зона ({zone[0]:.0f}, {zone[1]:.0f}) — стоп.")
                    return
        finally:
            self.move(0)

    # ── Дуги (всегда на TURN_SPEED_REF — калиброванной скорости) ──────
    def arc(self, angle_deg, direction=-1):
        """Дуга на angle_deg при максимальном угле руля.
        direction=-1 — против часовой (CCW), +1 — по часовой."""
        steer = int(direction * self.TURN_ANGLE)
        self.set_angle(steer)
        try:
            self._drive_with_zone_check(self.TURN_SPEED_REF,
                                        self._arc_time(angle_deg, self.TURN_ANGLE))
        finally:
            self.set_angle(0)

    def figure_eight(self, direction=-1):
        """Восьмёрка: две дуги по 360° в противоположных направлениях."""
        self.arc(360, direction)
        self.arc(360, -direction)

    def spiral(self, direction=1, outward=True):
        """Спираль — 2 оборота с линейной интерполяцией руля."""
        if outward:
            steer_start, steer_end = 36, 24
        else:
            steer_start, steer_end = 24, 36
        steps = 24                  # 2 круга × 12 шагов
        deg_per_step = 720 / steps
        try:
            for i in range(steps):
                frac = i / max(1, steps - 1)
                steer = steer_start + (steer_end - steer_start) * frac
                self.set_angle(int(direction * steer))
                ok = self._drive_with_zone_check(self.TURN_SPEED_REF,
                                                  self._arc_time(deg_per_step, steer))
                if not ok:
                    return
        finally:
            self.set_angle(0)

    def bypass(self, start_dir=1):
        """Объезд S-волной — 4 четверти дуг по 90°."""
        signs = [start_dir, start_dir, -start_dir, -start_dir]
        for sign in signs:
            self.arc(90, -sign)

    # ── Повороты курса (упрощённый K-turn) ─────────────────────────────
    def face(self, target_deg):
        """Развернуться курсом на target_deg через 3-дуговой K-turn."""
        diff = (target_deg - self.heading + 540) % 360 - 180
        if abs(diff) < 2:
            return
        direction = 1 if diff > 0 else -1
        steer_full = int(direction * self.TURN_ANGLE)
        steps = 3
        deg_per_pair = abs(diff) / steps
        t = self._arc_time(deg_per_pair, self.TURN_ANGLE)
        spd = self.TURN_SPEED_REF
        for _ in range(steps):
            self.set_angle(steer_full)
            if not self._drive_with_zone_check(spd, t): return
            self.set_angle(-steer_full)
            if not self._drive_with_zone_check(-spd, t): return
        self.set_angle(0)

    def turn_right(self, angle_deg):
        self.face((self.heading + angle_deg) % 360)

    def turn_left(self, angle_deg):
        self.face((self.heading - angle_deg) % 360)

    def turn_around(self, direction=1):
        self.face((self.heading + 180 * direction) % 360)

    def kturn(self, steps=3, direction=1):
        """Разворот на 180° (параметр steps на железе игнорируется)."""
        self.face((self.heading + 180 * direction) % 360)

    def set_course(self, target_deg):
        """На железе set_course = face (без in-motion дуги)."""
        self.face(target_deg)

    # ── Навигация ───────────────────────────────────────────────────────
    def goto(self, x, y):
        """Подъехать в точку (x, y): face → forward."""
        dx = x - self.x
        dy = y - self.y
        dist = math.hypot(dx, dy)
        if dist < 2:
            return
        target_heading = math.degrees(math.atan2(dx, dy)) % 360
        self.face(target_heading)
        self.forward(dist)

    def autopilot(self, x, y):
        """На железе без A*-планировщика — обычный goto.
        Опасные зоны проверяются движением, но не учитываются при
        построении маршрута. Если зона на пути — робот остановится."""
        self.goto(x, y)

    def curve(self, route):
        """Pure-pursuit на железе — последовательность goto по точкам."""
        for px, py in route:
            self.goto(px, py)

    def home(self):
        self.goto(0, 0)

    # ── Конфигурация ────────────────────────────────────────────────────
    def set_default_speed(self, speed_pct):
        self.MOVE_SPEED = abs(self._clamp_speed(speed_pct))

    def set_default_turn_angle(self, angle_deg):
        self.TURN_ANGLE = max(1, min(45, int(angle_deg)))

    def wait(self, seconds):
        time.sleep(seconds)

    def set_servo_center(self):
        self.set_angle(0)

    # ── Зоны опасности (реальные препятствия на поле) ──────────────────
    def load_danger_zones(self, zones):
        """Установить список опасных зон. `zones` — последовательность
        кортежей (x, y, radius). Координаты должны совпадать с реальными
        препятствиями на поле."""
        self.danger_zones = [(float(x), float(y), float(r)) for x, y, r in zones]
        return len(self.danger_zones)

    def show_danger_zones(self):
        """Распечатать и вернуть список активных опасных зон."""
        if not self.danger_zones:
            print("🗺 Опасных зон сейчас нет.")
            return []
        print(f"🗺 Активны опасные зоны ({len(self.danger_zones)}):")
        for i, (x, y, r) in enumerate(self.danger_zones, 1):
            print(f"   #{i}: ({x:.0f}, {y:.0f}) r={r:.0f}")
        return list(self.danger_zones)

    def remove_zone(self, x, y):
        """Удалить опасную зону, центр которой ближе всего к (x, y).
        Использовать когда обучающийся ФИЗИЧЕСКИ убрал препятствие
        с поля — после этого робот сможет проехать через эти координаты."""
        if not self.danger_zones:
            return False
        idx, _ = min(enumerate(self.danger_zones),
                     key=lambda iz: math.hypot(iz[1][0] - x, iz[1][1] - y))
        zx, zy, zr = self.danger_zones.pop(idx)
        print(f"🗑 Удалена опасная зона ({zx:.0f}, {zy:.0f}) r={zr:.0f}")
        return True

    def remove_zone_here(self):
        """Удалить ближайшую опасную зону к текущей позиции робота
        (= робот стоит внутри убранного препятствия)."""
        return self.remove_zone(self.x, self.y)

    def attention_zone(self, x, y, radius=None):
        """Пометить координаты (x, y) как «зону внимания» — место, куда
        не стоит ехать. На движение НЕ влияет (информативная пометка),
        но программа может потом проверить self.attention_zones."""
        r = float(radius) if radius is not None else 15.0
        self.attention_zones.append((float(x), float(y), r))

    def attention_here(self, radius=None):
        """Пометить ТЕКУЩУЮ позицию как зону внимания."""
        self.attention_zone(self.x, self.y, radius)

    def recharge(self):
        """На железе батарея заряжается физически — здесь просто заметка."""
        print("🔌 Подключи зарядку и нажми пуск, когда будет 100%.")


# ── Мост к 1Т REX ─────────────────────────────────────────────────────
# Низкоуровневое API класса Robot выше требует мост к железу. Доступны
# ДВА варианта:
#
#   1. _Bridge — пустые методы (pass). Для теста кода на ПК без робота:
#      Robot.X() выполнятся, x/y/heading остаются 0.
#
#   2. _NetworkBridge — реальный мост через server.py (тот, что в репо
#      рядом с прошивкой). Требует:
#        pip install websocket-client
#        запущенный server.py на 127.0.0.1:41235
#        подключённый ESP32 (WiFi или USB-Serial — выбирается в GUI server.py)
#      Команды отправляются в формате JSON {id, command, usb=false},
#      server.py пересылает на ESP32. Командный словарь: MOVE:N, ANGLE:N,
#      LASER?, RGB:i,r,g,b,delay (см. RexDriver в проекте VEGAREX).
#      Свойства x, y, heading считаются ФОНОВЫМ потоком dead-reckoning:
#      каждые 50 мс интегрируется позиция по скорости × времени. Точность
#      ограничена калибровкой (проскальзывание шин не учитывается).
#
# В конце файла выбери активный robot (одна из двух строчек раскоммен-
# тирована, другая — нет).
class _Bridge:
    """Заглушка для теста кода на ПК БЕЗ робота."""
    x = 0.0
    y = 0.0
    heading = 0.0

    def set_angle(self, deg):
        pass

    def move(self, pct):
        pass

    def stop(self):
        pass

    def set_rgb(self, idx, color, delay=0.0):
        pass

    def get_distance(self):
        return 999.0


class _NetworkBridge:
    """Реальный мост к 1Т REX через server.py (WebSocket).

    Подключается к ws://127.0.0.1:41235 при __init__. Все команды
    (set_angle/move/stop/set_rgb/get_distance) обёрнуты в JSON
    {id, command, usb=false} и пересылаются server.py на ESP32.

    Свойства x, y, heading обновляются фоновым потоком _dr_loop:
    каждые 50 мс интегрирует позицию по time × speed. Калибровка
    (SPEED_AT_100, WHEEL_CIRC_CM, HEADING_DEG_PER_ROT) синхронизирована
    с классом Robot выше — если меняешь там, поправь и здесь."""

    BROWSER_WS_HOST = "127.0.0.1"
    BROWSER_WS_PORT = 41235

    # Калибровка для счисления пути (та же, что в классе Robot).
    _SPEED_AT_100        = 80.0    # см/с при 100% мощности
    _WHEEL_CIRC_CM       = 28.3    # см, окружность колеса
    _HEADING_DEG_PER_ROT = 25.0    # °, sweep курса за оборот колеса при руле 45°

    def __init__(self):
        import json, threading, time, math, uuid
        try:
            import websocket  # pip install websocket-client
        except ImportError:
            raise RuntimeError(
                "Для _NetworkBridge нужен websocket-client. "
                "Установи: pip install websocket-client")

        self._json = json
        self._uuid = uuid
        self._math = math
        self._time = time

        self.x = 0.0
        self.y = 0.0
        self.heading = 0.0

        self._cur_speed_pct = 0      # последняя поданная скорость (для DR)
        self._cur_angle_deg = 0      # последний угол руля (для DR)
        self._last_tick = None
        self._stop_threads = False

        self._pending = {}           # id → [Event, value]
        self._pending_lock = threading.Lock()
        self._send_lock = threading.Lock()

        url = f"ws://{self.BROWSER_WS_HOST}:{self.BROWSER_WS_PORT}"
        self._ws = websocket.WebSocketApp(
            url,
            on_message=self._on_message,
            on_error=lambda ws, e: print(f"[bridge] WS error: {e}"),
            on_close=lambda ws, c, m: print("[bridge] WS closed"),
            on_open=lambda ws: print(f"[bridge] WS connected to {url}"),
        )
        threading.Thread(target=self._ws.run_forever, daemon=True).start()
        # Маленькая пауза, чтобы сокет успел подняться до первой команды.
        time.sleep(0.5)
        # Запуск dead-reckoning потока
        threading.Thread(target=self._dr_loop, daemon=True).start()

    # ── WebSocket ──────────────────────────────────────────────────────
    def _on_message(self, ws, raw):
        try:
            data = self._json.loads(raw)
            cid = data.get("id")
            with self._pending_lock:
                if cid in self._pending:
                    self._pending[cid][1] = data.get("value")
                    self._pending[cid][0].set()
        except Exception as e:
            print(f"[bridge] parse error: {e}")

    def _send_cmd(self, command, expect_response=False, timeout=2.0):
        import threading
        cid = str(self._uuid.uuid4())
        msg = self._json.dumps({"id": cid, "command": command, "usb": False})
        if expect_response:
            evt = threading.Event()
            with self._pending_lock:
                self._pending[cid] = [evt, None]
        try:
            with self._send_lock:
                self._ws.send(msg)
        except Exception as e:
            print(f"[bridge] send failed: {e}")
            if expect_response:
                with self._pending_lock:
                    self._pending.pop(cid, None)
            return None
        if expect_response:
            evt.wait(timeout)
            with self._pending_lock:
                _, value = self._pending.pop(cid, [None, None])
            return value
        return None

    # ── Dead-reckoning ─────────────────────────────────────────────────
    def _dr_loop(self):
        while not self._stop_threads:
            self._time.sleep(0.05)
            now = self._time.monotonic()
            if self._last_tick is None:
                self._last_tick = now
                continue
            dt = now - self._last_tick
            self._last_tick = now
            spd = self._cur_speed_pct
            if spd == 0:
                continue
            sign = 1 if spd > 0 else -1
            cm_per_s = (abs(spd) / 100.0) * self._SPEED_AT_100
            if cm_per_s <= 0:
                continue
            dist = cm_per_s * dt
            ang = self._cur_angle_deg
            if ang != 0:
                steer_ratio = ang / 45.0
                heading_delta = ((dist / self._WHEEL_CIRC_CM)
                                 * self._HEADING_DEG_PER_ROT
                                 * steer_ratio * sign)
                self.heading = (self.heading + heading_delta) % 360
            h_rad = self._math.radians(self.heading)
            self.x += dist * self._math.sin(h_rad) * sign
            self.y += dist * self._math.cos(h_rad) * sign

    # ── Низкоуровневое API ─────────────────────────────────────────────
    def set_angle(self, deg):
        deg = max(-45, min(45, int(deg)))
        self._cur_angle_deg = deg
        self._send_cmd(f"ANGLE:{deg}")

    def move(self, pct):
        pct = max(-100, min(100, int(pct)))
        self._cur_speed_pct = pct
        self._send_cmd(f"MOVE:{pct}")

    def stop(self):
        self._cur_speed_pct = 0
        self._cur_angle_deg = 0
        self._send_cmd("MOVE:0")
        self._send_cmd("ANGLE:0")

    def set_rgb(self, idx, color, delay=0.0):
        r, g, b = color
        self._send_cmd(f"RGB:{idx},{r},{g},{b},{delay}")

    def get_distance(self):
        """Запрашивает LASER? и парсит ответ «LASER:NNN» или «NNN»."""
        resp = self._send_cmd("LASER?", expect_response=True)
        if resp is None:
            return 999.0
        try:
            s = str(resp)
            return float(s.split(":")[-1] if ":" in s else s)
        except (TypeError, ValueError):
            return 999.0


class _LiveRobot(_Bridge, Robot):
    """STUB-режим: моторы не дёргаются, x/y/heading = 0.
    Используется для проверки кода на ПК без подключения к роботу."""
    def __init__(self):
        Robot.__init__(self)


class _NetworkRobot(_NetworkBridge, Robot):
    """БОЕВОЙ режим: при __init__ подключается к server.py через WebSocket.
    Требуется запущенный server.py и подключённый ESP32 (WiFi или USB)."""
    def __init__(self):
        _NetworkBridge.__init__(self)
        Robot.__init__(self)


# Активный робот. Раскомментируй ОДНУ из двух строк:
robot = _LiveRobot()              # тест на ПК без железа
# robot = _NetworkRobot()         # боевой режим через server.py
