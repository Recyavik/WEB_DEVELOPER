"""
robot_api.py — высокоуровневый Python-API для пользовательского кода.

Когда обучающийся пишет в редакторе:

    for n in range(4):
        robot.forward(50)
        robot.turn_right(90)

этот модуль предоставляет:
  • объект `robot` (RobotProxy) — фасад над async-методами Session;
  • sandbox для exec() — белый список встроенных, никаких `import`/`open`/`eval`;
  • синхронный мост над асинхронным executor'ом через
    asyncio.run_coroutine_threadsafe(...).result();
  • флаг отмены + watchdog по времени.

Дизайн: RobotProxy НЕ дублирует логику Session — он только обращается к
существующим `_run_*` методам. Mission-трекинг точек срабатывает в physics
tick автоматически; action-команды (mark_danger / attention_here)
дополнительно дёргают `_mission_check_action`.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import threading
import traceback
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from session import Session


class RobotInterrupted(Exception):
    """Поднимается, когда пользователь нажал ■ СТОП — прерывает цикл/код."""


class RobotProxy:
    """Объект `robot` в коде пользователя. Все методы синхронные.

    Внутри каждой команды:
      1) Проверяем флаг отмены — если поднят, кидаем RobotInterrupted;
      2) Шлём корутину `_run_X` в event-loop сервера;
      3) Блокируем поток исполнения пользовательского кода до конца команды.

    Доступ к состоянию:
      `robot.x`, `robot.y`, `robot.heading` — текущие координаты/курс,
      читаются из robot_state (можно использовать в `if`).
    """
    # Watchdog: одна команда не должна выполняться дольше этого времени
    # (страховка от бесконечного цикла внутри _run_X).
    _CMD_TIMEOUT_SEC = 60.0

    def __init__(self, session: "Session",
                 loop: asyncio.AbstractEventLoop,
                 cancel_flag: threading.Event):
        self._session = session
        self._loop = loop
        self._cancel = cancel_flag

    # ── Инфраструктура ──────────────────────────────────────────────────────

    def _run(self, coro):
        """Прокидывает корутину в event-loop сервера и ждёт результат.
        Между шагами пользовательской программы проверяет флаг отмены.
        Регистрирует активный future на session — ■ СТОП может его
        отменить мгновенно, чтобы текущая команда не доехала до конца."""
        if self._cancel.is_set():
            raise RobotInterrupted("Прервано пользователем (■ СТОП).")
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        # Регистрируем — _do_stop отменит этот future при ■ СТОП.
        self._session._python_active_future = fut
        try:
            return fut.result(timeout=self._CMD_TIMEOUT_SEC)
        except asyncio.TimeoutError:
            fut.cancel()
            raise RobotInterrupted(
                f"Команда не завершилась за {self._CMD_TIMEOUT_SEC:.0f} с — прервано.")
        except asyncio.CancelledError:
            # _do_stop отменил future — пользователь нажал ■ СТОП.
            raise RobotInterrupted("Прервано пользователем (■ СТОП).")
        finally:
            self._session._python_active_future = None

    @property
    def x(self) -> float:
        return float(self._session.robot_state.x)

    @property
    def y(self) -> float:
        return float(self._session.robot_state.y)

    @property
    def heading(self) -> float:
        return float(self._session.robot_state.heading)

    # ── Движение ────────────────────────────────────────────────────────────

    def forward(self, distance_cm: float, speed: Optional[int] = None):
        """Едет ВПЕРЁД на `distance_cm` см. Скорость по умолчанию — из настроек."""
        spd = int(speed) if speed is not None else int(self._session.cfg.move_speed)
        self._run(self._session._run_forward(float(distance_cm), spd))

    def back(self, distance_cm: float, speed: Optional[int] = None):
        """Едет НАЗАД на `distance_cm` см."""
        spd = int(speed) if speed is not None else int(self._session.cfg.move_speed)
        self._run(self._session._run_back(float(distance_cm), spd))

    def turn_right(self, angle_deg: float):
        """Поворот НА МЕСТЕ направо на `angle_deg` градусов."""
        s = self._session.robot_state
        target = (s.heading + float(angle_deg)) % 360
        self._run(self._session._run_face_cardinal(target, f"{angle_deg:g}° вправо"))

    def turn_left(self, angle_deg: float):
        """Поворот НА МЕСТЕ налево на `angle_deg` градусов."""
        s = self._session.robot_state
        target = (s.heading - float(angle_deg)) % 360
        self._run(self._session._run_face_cardinal(target, f"{angle_deg:g}° влево"))

    def set_course(self, target_deg: float):
        """Развернуться курсом на абсолютный угол (0=север, 90=восток, …)."""
        self._run(self._session._run_set_course(int(target_deg)))

    def home(self):
        """Вернуться в стартовую точку. Центрирует руль перед стартом —
        иначе оставшийся угол с прошлого `set_angle(...)` испортил бы
        прямую фазу маршрута (робот ехал бы по дуге)."""
        self._center_steer()
        self._run(self._session._run_home())

    def _center_steer(self):
        """Установить руль в 0° и в драйвере, и в robot_state.steer.
        Используется навигационными командами (goto/home), которые
        внутри полагаются на «прямой ход» во время прямых фаз."""
        self._run(self._session.robot.set_servo_center())
        self._session.robot_state.steer = 0.0

    def wait(self, seconds: float):
        """Стоять на месте `seconds` секунд."""
        self._run(self._session._run_pause(float(seconds)))

    # ── Низкоуровневое API (как у 1T REX) ───────────────────────────────────
    # Эти методы — прямой проброс к драйверу (sim/rex/bridge). Полезны, когда
    # высокоуровневые forward/back/turn_X не подходят (например, плавное
    # движение по дуге с ручным временем).

    def move(self, speed_pct: int, timeout_sec: Optional[float] = None):
        """Подать на мотор `speed_pct` мощности (-100..+100).
        Если задан `timeout_sec` — едет это время и затем останавливается.
        Иначе крутит до следующего `robot.stop()`/`robot.move(0)`.

        Внутри: дублируем команду драйверу И в `robot_state.speed`, чтобы
        симулятор интегрировал позицию (физика читает state, не драйвер)."""
        spd = int(speed_pct)
        s = self._session.robot_state
        if timeout_sec is None:
            self._run(self._session.robot.move(spd))
            s.speed     = float(spd)
            s.dist_left = 0.0          # 0 = «ехать без авто-остановки»
            return
        # С таймаутом: запускаем, ждём, гасим.
        self._run(self._session.robot.move(spd))
        s.speed     = float(spd)
        s.dist_left = 0.0
        self._run(asyncio.sleep(float(timeout_sec)))
        self._run(self._session.robot.move(0))
        s.speed = 0.0

    def set_angle(self, angle_deg: int):
        """Установить угол руля: -45..+45° (0 = прямо).
        Обновляет и драйвер, и robot_state.steer — иначе симулятор
        не узнаёт об изменении угла и движение forward будет прямым."""
        clamped = max(-45, min(45, int(angle_deg)))
        self._run(self._session.robot.set_angle(clamped))
        self._session.robot_state.steer = float(clamped)

    def set_servo_center(self):
        """Поставить руль в 0° (alias `set_angle(0)`)."""
        self._run(self._session.robot.set_servo_center())
        self._session.robot_state.steer = 0.0

    def stop(self):
        """Аварийная остановка робота И программы. Мотор глушится мгновенно,
        дальнейшие строки кода НЕ выполняются (как ■ СТОП в UI).

        Если нужно просто притормозить и продолжить — используй
        `robot.move(0)` (без прерывания exec)."""
        # 1) Заглушить мотор синхронно — на случай если RobotInterrupted
        #    поймают где-то выше и обработают.
        self._run(self._session.robot.stop())
        s = self._session.robot_state
        s.speed = 0
        s.dist_left = 0
        # 2) Поднять флаг отмены — все последующие robot.X() кинут
        #    RobotInterrupted даже если кто-то поймает наш raise ниже.
        self._cancel.set()
        # 3) Прервать exec прямо в этой строке.
        raise RobotInterrupted("Остановлено командой robot.stop().")

    def clear(self):
        """Очистить превью: убрать опасные/жёлтые зоны, стереть пройденный
        путь, телепортировать робота в стартовую точку (START_X/Y/HEADING).
        То же что кнопка «↺ Поле», но из кода."""
        self._run(self._session._run_reset(db=None, keep_mode=True))

    def set_rgb(self, index: int, color: tuple, delay: float = 0.0):
        """Установить цвет LED №`index`. `color` = (R, G, B), 0..255."""
        self._run(self._session.robot.set_rgb(int(index), tuple(color),
                                              float(delay)))

    # ── Манёвры (высокоуровневые) ───────────────────────────────────────────

    def forward_to_wall(self, speed: Optional[int] = None):
        """Едет вперёд до ближайшей стены или препятствия."""
        spd = int(speed) if speed is not None else int(self._session.cfg.move_speed)
        self._run(self._session._run_forward_to_wall(spd))

    def backward_to_wall(self, speed: Optional[int] = None):
        """Едет назад до ближайшей стены или препятствия."""
        spd = int(speed) if speed is not None else int(self._session.cfg.move_speed)
        self._run(self._session._run_backward_to_wall(spd))

    def turn_around(self, direction: int = 1):
        """Разворот на 180° через дугу. `direction`: +1 = вправо, -1 = влево."""
        s = self._session.robot_state
        target = (s.heading + 180.0 * (1 if direction >= 0 else -1)) % 360.0
        self._run(self._session._run_face_cardinal(
            target, "разворот вправо" if direction >= 0 else "разворот влево"))

    def kturn(self, steps: int = 3, direction: int = 1):
        """Разворот на 180° на месте за `steps` шагов K-turn'а.
        Чем больше шагов, тем плавнее и компактнее манёвр."""
        self._run(self._session._k_turn_n(int(direction), int(steps)))

    def arc(self, angle_deg: float, direction: int = -1):
        """Дуга на угол `angle_deg` градусов при МАКСИМАЛЬНОМ угле руля
        (из настроек «Максимальный угол руля»). По умолчанию ПРОТИВ
        часовой стрелки (математическое +). `direction`: +1 = по часовой,
        -1 = против часовой. `arc(360)` = полный круг."""
        self._run(self._session._run_arc(float(angle_deg), int(direction)))

    def figure_eight(self, direction: int = -1):
        """Восьмёрка через две дуги по 360°: круг в одну сторону + круг
        в другую. `direction` задаёт направление ПЕРВОГО круга
        (+1 = по часовой, -1 = против часовой)."""
        self._run(self._session._run_figure_eight(int(direction)))

    def spiral(self, direction: int = 1, outward: bool = True):
        """Спираль: 2 оборота с меняющимся радиусом.
        `outward=True` — раскручивается (радиус растёт);
        `outward=False` — скручивается (радиус сужается)."""
        self._run(self._session._run_spiral(int(direction), bool(outward)))

    def bypass(self, start_dir: int = 1):
        """S-волна для объезда препятствия. `start_dir`: +1 = сначала вправо,
        -1 = сначала влево. После 4-х дуг возвращается на исходный курс."""
        self._run(self._session._run_bypass(int(start_dir)))

    def face(self, target_deg: float):
        """Развернуться лицом к абсолютному курсу (0=N, 90=E, 180=S, 270=W).
        Использует дуговой поворот (K-turn геометрия)."""
        self._run(self._session._run_face_cardinal(
            float(target_deg) % 360.0, f"{target_deg:g}°"))

    def goto(self, x: float, y: float):
        """Перейти в точку (x, y): поворот + прямая. Высокоуровневый goto.
        Центрирует руль перед стартом — иначе оставшийся угол с прошлого
        `set_angle(...)` испортил бы прямую фазу (робот ехал бы по дуге
        и промахивался мимо цели).
        Во время миссии команда блокируется (см. _dispatch goto-ветку) —
        обучающийся должен составить маршрут вручную."""
        self._center_steer()
        self._run(self._session._run_goto(float(x), float(y)))

    def remove_zone(self, x: float, y: float):
        """Удалить зону, накрывающую точку (x, y). Робот должен быть внутри."""
        self._run(self._session._run_remove_zone(float(x), float(y), None))
        # Mission tracking: пробуем матч на оба типа зон.
        self._session._mission_check_action("remove_danger", float(x), float(y))
        self._session._mission_check_action("remove_attention", float(x), float(y))

    def recharge(self):
        """Зарядить батарею (в симуляторе — мгновенно до 100%)."""
        self._run(self._session._run_recharge())

    def set_default_speed(self, speed_pct: int):
        """Изменить скорость по умолчанию для последующих `robot.forward()`
        / `robot.back()` без явного `speed=`. Применяется только в этой
        сессии (в настройки не сохраняется)."""
        self._session.cfg.move_speed = int(speed_pct)

    def set_default_turn_angle(self, angle_deg: int):
        """Изменить угол руля по умолчанию (−45..+45) для последующих
        манёвров. Применяется в текущей сессии."""
        self._session.cfg.turn_angle = int(angle_deg)

    # ── Зоны (action-команды — нужен mission tracking) ──────────────────────
    # «Опасные» (красные) зоны — это обстановка, выставляется ДО запуска
    # программы (UI-мышь / mission_generator / pre-flight). Программно
    # СТАВИТЬ их нельзя — только удалять командой `remove_zone*`, когда
    # робот стоит внутри.

    def load_danger_zones(self, zones) -> int:
        """Установить опасные зоны обстановки на карте из списка
        `[(x, y, radius), ...]`. Заменяет текущие красные зоны
        (идемпотентно: повторный вызов с теми же данными даст тот же
        результат). Используется в авто-генерируемом блоке начала
        программы, чтобы пользователь видел установку явно.

        Жёлтые зоны внимания (kind='algorithm') этим методом НЕ
        затрагиваются — они часть runtime-логики, не обстановки.

        Возвращает: количество установленных зон."""
        triples: list[tuple[float, float, float]] = []
        for item in (zones or []):
            try:
                x, y, r = item
                triples.append((float(x), float(y), float(r)))
            except (TypeError, ValueError):
                continue
        self._run(self._session._run_load_danger_zones(triples))
        return len(triples)

    def show_danger_zones(self) -> list[tuple[float, float, float]]:
        """Показать активные опасные зоны в журнале и вернуть их список.

        Использует текущее состояние world (то же, что в `DANGER_ZONES`
        в преамбуле кода). Полезно для проверки обстановки в начале
        алгоритма — обучающийся видит сколько и каких зон сейчас активно.

        Возвращает: список троек (x, y, radius) в порядке номеров #1..#N.
        Можно итерировать в своём коде:
            for x, y, r in robot.show_danger_zones():
                ...
        """
        danger = sorted(
            (z for z in self._session.world.danger_zones if z.kind == "danger"),
            key=lambda z: z.display_no or 0)
        if not danger:
            self._run(self._session.push_message(
                "🗺 Опасных зон сейчас нет.", "info"))
            return []
        lines = [f"🗺 Активны опасные зоны ({len(danger)}):"]
        for z in danger:
            no = z.display_no or 0
            lines.append(f"   #{no}: ({z.x:.0f}, {z.y:.0f}) r={z.radius:.0f}")
        self._run(self._session.push_message("\n".join(lines), "info"))
        return [(z.x, z.y, z.radius) for z in danger]

    def attention_zone(self, x: float, y: float, radius: Optional[float] = None):
        """Поставить жёлтую зону внимания в точке (x, y) — РИСУЕТ, не едет."""
        r = float(radius) if radius is not None else None
        # _run_set_algorithm_zone делает goto+place — для API мы не хотим
        # неявной поездки. Просто ставим зону на месте через тот же helper,
        # что и UI-кнопка, но с явными координатами.
        s = self._session.robot_state
        # Переносим робота? Нет — просто ставим зону в (x, y) без движения.
        # Используем внутренний world API, миссию чекаем по (x, y).
        self._run(self._add_attention_at(float(x), float(y), r))
        self._session._mission_check_action("place_attention", float(x), float(y))

    def attention_here(self, radius: Optional[float] = None):
        """Поставить жёлтую зону внимания в текущей позиции робота."""
        s = self._session.robot_state
        r = float(radius) if radius is not None else None
        self._run(self._session._run_place_attention_here(r, None))
        self._session._mission_check_action("place_attention", s.x, s.y)

    def remove_zone_here(self):
        """Убрать зону (опасную или внимания) под текущей позицией робота."""
        self._run(self._session._run_remove_zone(None, None, None))
        s = self._session.robot_state
        # Пробуем матч на оба типа — try_match_action игнорит несовпавшие.
        self._session._mission_check_action("remove_danger", s.x, s.y)
        self._session._mission_check_action("remove_attention", s.x, s.y)

    # ── Вспомогательные внутренние корутины ─────────────────────────────────

    async def _add_attention_at(self, x: float, y: float, radius: Optional[float]):
        """Ставит зону внимания в произвольной точке (x, y) — без поездки."""
        sess = self._session
        r = float(radius) if radius is not None else float(sess.cfg.danger_zone_radius)
        zone = sess.world.add_danger_zone(x, y, radius=r,
                                          label="Зона внимания",
                                          kind="algorithm")
        await sess.push_message(
            f"🟡 Зона внимания в ({x:.0f}, {y:.0f}), радиус {r:.0f}.", "info")
        await sess.push_world()


# ── Sandbox ─────────────────────────────────────────────────────────────────

# Белый список модулей, разрешённых через `import` — только чистая математика,
# время и случайные числа. Никакого `os`, `sys`, `subprocess`, `socket`.
_SAFE_MODULES = {"math", "time", "random"}


def _safe_import(name, globals=None, locals=None, fromlist=(), level=0):
    """Замена встроенному __import__: разрешает только модули из _SAFE_MODULES.
    Без этой подмены `import math` упирается в `ImportError: __import__ not found`."""
    if name in _SAFE_MODULES:
        return __import__(name, globals, locals, fromlist, level)
    raise ImportError(
        f"Импорт «{name}» запрещён. Доступны: {sorted(_SAFE_MODULES)}.")


# Безопасные builtins для exec() — никакого file I/O, eval, exec.
# Дополнительно: для `for n in range(...)` нужны `range`, `len`, и т.п.
# `print` подменяется в build_sandbox_globals на функцию, шлющую сообщения
# в журнал клиента (см. ниже).
_SAFE_BUILTINS_CORE = {
    # Базовые типы/конструкторы
    "True":  True, "False": False, "None":  None,
    "int":   int,  "float": float, "str":   str,
    "bool":  bool, "list":  list,  "tuple": tuple, "dict": dict, "set": set,
    # Итерация и числа
    "range": range, "len": len, "abs": abs, "min": min, "max": max,
    "sum":   sum,   "round": round, "enumerate": enumerate, "zip": zip,
    "sorted": sorted, "reversed": reversed,
    # Контролируемый import (math/time/random)
    "__import__": _safe_import,
}


def build_sandbox_globals(robot: RobotProxy) -> dict:
    """Глобальный scope для exec(): пользовательскому коду доступен только
    `robot`, безопасные builtins и стандартные math/time/random через import.

    `print(...)` перехвачен — выводит в журнал клиента (push_message),
    а не в stdout сервера. Поддерживает обычный синтаксис
    `print(a, b, sep=', ')`."""
    def journal_print(*args, sep=" ", end="\n", **_ignored):
        # Форматируем как стандартный print, но без stream-аргумента.
        text = sep.join(str(a) for a in args)
        if end and end != "\n":
            text += end.rstrip("\n")
        # Шлём push_message на event-loop сервера. Блокируем поток до
        # отправки, чтобы порядок сообщений совпал с порядком вызовов print.
        coro = robot._session.push_message(text or "", "info")
        try:
            asyncio.run_coroutine_threadsafe(coro, robot._loop).result(timeout=5)
        except Exception:
            # Если что-то не так с loop — не валим программу из-за print.
            pass

    builtins = dict(_SAFE_BUILTINS_CORE)
    builtins["print"] = journal_print
    return {
        "__builtins__": builtins,
        "robot": robot,
    }


# ── Запуск пользовательского кода ───────────────────────────────────────────

async def run_user_python(
    session: "Session",
    code: str,
    total_timeout_sec: float = 120.0,
) -> str:
    """Запускает пользовательский Python-код с фасадом `robot`.

    Возвращает строку для отображения в журнале:
      • «Выполнено за N.N с.» при успехе;
      • traceback (последние строки) при ошибке;
      • сообщение об отмене при ■ СТОП или превышении таймаута.

    Архитектура:
      1) Создаём флаг отмены (threading.Event) — связываем с session
         (нужно прерывать из ■ СТОП, который сейчас прерывает _executing
         через asyncio.CancelledError);
      2) RobotProxy получает ссылку на session, loop и флаг;
      3) Запускаем exec() в отдельном потоке (чтобы не блокировать
         event-loop), ждём окончания через asyncio.to_thread;
      4) Общий таймаут — страховка от «зомби-программ» (повисший robot.wait
         или бесконечный цикл без вызовов robot.*).
    """
    loop = asyncio.get_running_loop()
    cancel_flag = threading.Event()
    # Регистрируем флаг на сессии — кнопка ■ СТОП его поднимет.
    session._python_cancel_flag = cancel_flag
    robot = RobotProxy(session, loop, cancel_flag)
    sandbox = build_sandbox_globals(robot)

    # Каждый прогон ▶ начинается со стартовой точки — иначе робот
    # «продолжал бы» с того места, где остановился прошлый запуск.
    # Передаём `code_text=code` напрямую: _run_reset спарсит START_X/Y/
    # HEADING_DEG из САМОГО СВЕЖЕГО текста textarea (не из кэша). Это
    # важно: если пользователь только что отредактировал START_X и сразу
    # нажал ▶ — должны взять новое значение, а не закэшированное.
    # keep_mode=True — оставляем «осторожно»/«инспектор» как было.
    await session._run_reset(db=None, keep_mode=True, code_text=code)

    import time
    started = time.monotonic()

    def _exec_in_thread() -> Optional[str]:
        """Запускает exec() в потоке. Возвращает None при успехе,
        либо короткий текст ошибки (1 строка) для журнала."""
        try:
            compiled = compile(code, "<пользовательский код>", "exec")
            exec(compiled, sandbox)
            return None
        except RobotInterrupted as e:
            return f"⏹ {e}"
        except (asyncio.CancelledError,
                concurrent.futures.CancelledError):
            # ■ СТОП пришёл во время fut.result(): catches in _run могли
            # быть обойдены если CancelledError пришёл из concurrent.futures
            # (другая ветка иерархии BaseException/Exception в Py<3.8).
            # Здесь — последний рубеж, форматируем как «прервано».
            return "⏹ Прервано пользователем (■ СТОП)."
        except SyntaxError as e:
            # Синтаксическая ошибка — компактный однострочник.
            return f"Ошибка синтаксиса в строке {e.lineno}: {e.msg}"
        except Exception as e:
            # Любая runtime-ошибка → один компакт-line с номером строки
            # пользовательского кода (а не глубокий traceback).
            user_line = None
            for fr in traceback.extract_tb(e.__traceback__):
                if fr.filename == "<пользовательский код>":
                    user_line = fr.lineno
                    break
            loc = f" в строке {user_line}" if user_line else ""
            return f"Ошибка ({type(e).__name__}){loc}: {e}"

    try:
        err = await asyncio.wait_for(
            asyncio.to_thread(_exec_in_thread),
            timeout=total_timeout_sec,
        )
    except asyncio.TimeoutError:
        cancel_flag.set()
        err = (f"⏹ Программа не завершилась за {total_timeout_sec:.0f} с — "
               f"прервано. Проверьте бесконечные циклы.")
    finally:
        session._python_cancel_flag = None

    elapsed = time.monotonic() - started
    if err is None:
        return f"✓ Выполнено за {elapsed:.1f} с."
    return err
