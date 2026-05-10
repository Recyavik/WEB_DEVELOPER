"""Smoke-тесты для генератора Python-кода (helpers, дедуп, парсер).

Запуск:
    cd VAGAREX
    python -m tests.test_codegen
    # или
    python -m unittest tests.test_codegen -v
"""
import os
import sys
import re
import unittest
from dataclasses import dataclass

# Добавим корень VAGAREX в sys.path, чтобы запускать «python -m tests.test_codegen»
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# Лёгкий импорт: нам нужны только UserSession.* статические/методы codegen,
# не запуская никакой WebSocket / БД / физику.
from session import UserSession, UserCfg, RobotCmd  # noqa: E402


# ── Минимальный конфиг и фейковый robot_state, чтобы codegen запустился ────

def _make_cfg() -> UserCfg:
    return UserCfg(
        rex_host="127.0.0.1", rex_port=8765, simulation_mode=True,
        move_speed=40, turn_angle=35, wheel_circ_cm=28.30,
        speed_at_100=80.0, heading_per_rot=25.0, turn_speed_ref=40,
        world_w_cm=400, world_h_cm=400, wall_thickness_cm=5.0,
        robot_length_cm=20.0, robot_width_cm=12.0,
        start_x_cm=0, start_y_cm=100, start_heading_deg=0,
        sensor_type="laser", sonar_interval_ms=50, danger_zone_radius=20,
    )


@dataclass
class _RobotStateStub:
    steer: float = 0.0
    x:     float = 0.0
    y:     float = 0.0
    heading: float = 0.0


class _SessionStub(UserSession):
    """Минимальная версия UserSession, минующая __init__ (не нужны БД/WebSocket)."""
    def __init__(self, cfg: UserCfg):
        # Намеренно НЕ зовём super().__init__ — он бы поднял World/Driver/etc.
        import itertools
        self.cfg = cfg
        self.robot_state = _RobotStateStub()
        self._cmd_counter = itertools.count(1)
        self._program: list = []


def _cmd(intent: str, raw: str = "", code: str = "", label: str = "") -> RobotCmd:
    return RobotCmd(id=1, intent=intent, label=label or intent,
                    code=code or f"{intent}()", raw=raw or f"Вега {intent}")


# ────────────────────────────────────────────────────────────────────────────


class TestHelperRegistry(unittest.TestCase):
    """Реестр helpers: имена и зависимости консистентны."""

    def setUp(self):
        self.s = _SessionStub(_make_cfg())

    def test_every_helper_has_code(self):
        for name in self.s._HELPER_DEPS:
            self.assertIn(name, self.s._HELPER_CODE,
                          f"helper {name!r} в _HELPER_DEPS, но нет в _HELPER_CODE")

    def test_every_dep_is_known(self):
        for name, deps in self.s._HELPER_DEPS.items():
            for d in deps:
                self.assertIn(d, self.s._HELPER_DEPS,
                              f"зависимость {d!r} (helper {name!r}) не объявлена")

    def test_helper_code_compiles(self):
        """Каждый helper код должен парситься Python (без преамбулы констант)."""
        # Подставим заглушки констант, чтобы ast.parse прошёл
        prologue = (
            "import math, time\n"
            "DEFAULT_SPEED=40; DEFAULT_TURN_ANGLE=35\n"
            "SPEED_CM_PER_S_AT_100=80.0; WHEEL_CIRC_CM=28.3\n"
            "HEADING_DEG_PER_ROT=25.0; ROBOT_LENGTH_CM=20.0\n"
            "START_X=0; START_Y=100; START_HEADING_DEG=0\n"
            "class _R: pass\nrobot=_R()\n"
        )
        import ast
        for name, (_desc, code) in self.s._HELPER_CODE.items():
            try:
                ast.parse(prologue + code)
            except SyntaxError as e:
                self.fail(f"helper {name!r} не парсится: {e}")


class TestCollectHelpers(unittest.TestCase):
    """Транзитивный сбор зависимостей."""

    def setUp(self):
        self.s = _SessionStub(_make_cfg())

    def test_circle_only(self):
        helpers = self.s._collect_helpers([_cmd("circle", "Вега вокруг")])
        self.assertEqual(helpers, ["circle_cmd"])

    def test_home_pulls_all_deps(self):
        helpers = self.s._collect_helpers([_cmd("home", "Вега домой")])
        # home → goto → face_cardinal + Odometry + duration_for_distance
        #            → drive_to_point_closed_loop (closed-loop проезд)
        self.assertEqual(set(helpers),
                         {"Odometry", "duration_for_distance",
                          "face_cardinal_cmd", "drive_to_point_closed_loop",
                          "goto_cmd", "home_cmd"})
        # Каждая зависимость должна стоять ДО зависимого:
        idx = {n: i for i, n in enumerate(helpers)}
        self.assertLess(idx["Odometry"], idx["face_cardinal_cmd"])
        self.assertLess(idx["duration_for_distance"], idx["face_cardinal_cmd"])
        self.assertLess(idx["Odometry"], idx["drive_to_point_closed_loop"])
        self.assertLess(idx["face_cardinal_cmd"], idx["goto_cmd"])
        self.assertLess(idx["drive_to_point_closed_loop"], idx["goto_cmd"])
        self.assertLess(idx["goto_cmd"], idx["home_cmd"])

    def test_goto_pulls_closed_loop(self):
        """goto_cmd должна тянуть drive_to_point_closed_loop (коррекция курса)."""
        c = _cmd("goto", raw="Вега в точку 100 50", code="goto(100,50)")
        helpers = self.s._collect_helpers([c])
        self.assertIn("drive_to_point_closed_loop", helpers,
                      "goto_cmd должна включать closed-loop helper")

    def test_no_dup_when_two_circles(self):
        cmds = [_cmd("circle"), _cmd("circle")]
        helpers = self.s._collect_helpers(cmds)
        self.assertEqual(helpers, ["circle_cmd"])

    def test_forward_with_dist_pulls_duration(self):
        c = _cmd("forward", raw="Вега вперед 100 см", code="forward(100)")
        helpers = self.s._collect_helpers([c])
        self.assertEqual(helpers, ["duration_for_distance"])

    def test_forward_no_dist_no_helpers(self):
        c = _cmd("forward", raw="Вега вперед", code="forward()")
        helpers = self.s._collect_helpers([c])
        self.assertEqual(helpers, [])


class TestPreambleEmission(unittest.TestCase):
    """Преамбула: содержит только нужные def-блоки."""

    def setUp(self):
        self.s = _SessionStub(_make_cfg())

    def test_empty_program_only_constants(self):
        out = self.s._python_code_preamble(None)
        self.assertIn("DEFAULT_SPEED", out)
        # ни одной def-функции
        self.assertNotRegex(out, r"^def \w+", )

    def test_circle_only_circle_helper(self):
        out = self.s._python_code_preamble([_cmd("circle")])
        self.assertIn("def circle_cmd(", out)
        # никаких посторонних helpers
        self.assertNotIn("def goto_cmd(", out)
        self.assertNotIn("class Odometry", out)

    def test_helper_has_russian_header(self):
        out = self.s._python_code_preamble([_cmd("circle")])
        # перед def должна стоять русская комментарий-шапка
        self.assertRegex(out, r"# Движение по окружности.*\ndef circle_cmd\(")

    def test_helpers_in_dep_order(self):
        out = self.s._python_code_preamble([_cmd("home", "Вега домой")])
        # home -> goto -> face_cardinal -> Odometry/duration
        deps_order = [m.start() for m in re.finditer(
            r"def (?:duration_for_distance|face_cardinal_cmd|goto_cmd|home_cmd)\(", out)]
        # помимо порядка проверяем, что всё есть
        self.assertEqual(len(deps_order), 4)
        # порядок вхождения должен быть отсортирован
        self.assertEqual(deps_order, sorted(deps_order))


class TestCallLines(unittest.TestCase):
    """Строки вызова: НЕ содержат `# CMD:` маркера, есть русский inline-комментарий."""

    def setUp(self):
        self.s = _SessionStub(_make_cfg())

    def test_no_cmd_marker(self):
        lines = self.s._python_call_lines_for_cmd(_cmd("circle"))
        for line in lines:
            self.assertFalse(line.lstrip().startswith("# CMD"),
                             f"маркер # CMD должен быть удалён: {line!r}")

    def test_circle_call_has_russian_comment(self):
        lines = self.s._python_call_lines_for_cmd(_cmd("circle", raw="Вега вокруг"))
        joined = "\n".join(lines)
        self.assertIn("circle_cmd(", joined)
        self.assertRegex(joined, r"# окружность")

    def test_face_cardinal_inline_comment(self):
        lines = self.s._python_call_lines_for_cmd(
            _cmd("face_s", raw="Вега на юг", code="face_s()", label="Лицом на юг"))
        self.assertEqual(lines, ["face_cardinal_cmd(180)  # на юг"])


class TestParser(unittest.TestCase):
    """Парсер _parse_dsl_line распознаёт суффикс _cmd и legacy префикс."""

    def test_suffix_cmd(self):
        result = UserSession._parse_dsl_line("circle_cmd", "-1")
        self.assertIsNotNone(result)
        intent, raw = result
        self.assertEqual(intent, "circle")

    def test_legacy_prefix_cmd(self):
        result = UserSession._parse_dsl_line("cmd_circle", "-1")
        self.assertIsNotNone(result)
        intent, _ = result
        self.assertEqual(intent, "circle")

    def test_short_form(self):
        result = UserSession._parse_dsl_line("forward", "100")
        self.assertIsNotNone(result)
        intent, raw = result
        self.assertEqual(intent, "forward")
        self.assertIn("100", raw)

    def test_here_suffix(self):
        result = UserSession._parse_dsl_line("mark_danger_here_cmd", "")
        self.assertIsNotNone(result)
        intent, _ = result
        self.assertEqual(intent, "mark_danger")

    def test_unknown(self):
        self.assertIsNone(UserSession._parse_dsl_line("definitely_not_a_command", ""))

    def test_face_cardinal_generic_180_maps_to_south(self):
        """face_cardinal(180) должна распознаваться как face_s (на юг)."""
        result = UserSession._parse_dsl_line("face_cardinal", "180")
        self.assertIsNotNone(result, "face_cardinal(180) теряется парсером")
        intent, _ = result
        self.assertEqual(intent, "face_s")

    def test_face_cardinal_generic_via_cmd_suffix(self):
        """face_cardinal_cmd(180) (как в сгенерированном коде) тоже работает."""
        result = UserSession._parse_dsl_line("face_cardinal_cmd", "180")
        self.assertIsNotNone(result)
        intent, _ = result
        self.assertEqual(intent, "face_s")

    def test_face_cardinal_all_8_directions(self):
        cases = [
            (0,   "face_n"),
            (45,  "face_ne"),
            (90,  "face_e"),
            (135, "face_se"),
            (180, "face_s"),
            (225, "face_sw"),
            (270, "face_w"),
            (315, "face_nw"),
        ]
        for deg, expected_intent in cases:
            with self.subTest(deg=deg):
                result = UserSession._parse_dsl_line("face_cardinal_cmd", str(deg))
                self.assertIsNotNone(result, f"face_cardinal({deg}) не распознан")
                self.assertEqual(result[0], expected_intent)

    def test_face_cardinal_rounds_to_nearest(self):
        """face_cardinal(190) → ближайшее = face_s (180°)."""
        result = UserSession._parse_dsl_line("face_cardinal_cmd", "190")
        self.assertEqual(result[0], "face_s")
        # 350° ближе к 0° (= 10° дельты), чем к 315° (= 35° дельты) → face_n
        result = UserSession._parse_dsl_line("face_cardinal_cmd", "350")
        self.assertEqual(result[0], "face_n")


class TestGotoOptimization(unittest.TestCase):
    """goto оптимизируется в forward/backward, если курс совпадает/противоположен."""

    def setUp(self):
        self.s = _SessionStub(_make_cfg())

    def _goto(self, tx, ty):
        return _cmd("goto",
                    raw=f"Вега в точку {tx:g} {ty:g}",
                    code=f"goto({tx:g},{ty:g})")

    def test_aligned_forward_emits_simple_forward(self):
        """Робот в (0,0) heading=0 (N), цель (0, 100) — курс совпадает.
        Должен генерироваться простой forward, без goto_cmd."""
        self.s.robot_state.x, self.s.robot_state.y = 0, 0
        self.s.robot_state.heading = 0   # north
        cmd = self._goto(0, 100)

        # Helpers: только duration_for_distance, не goto_cmd
        helpers = self.s._helpers_for_cmd(cmd)
        self.assertEqual(helpers, ["duration_for_distance"])

        # Call lines: robot.move с положительной мощностью
        lines = self.s._python_call_lines_for_cmd(cmd)
        joined = "\n".join(lines)
        self.assertIn("robot.move(40, duration_for_distance(100", joined)
        self.assertNotIn("goto_cmd", joined)
        self.assertNotIn("-40", joined)   # не задом

    def test_opposite_heading_emits_backward(self):
        """Робот в (0,100) heading=0 (N), цель (0, 0) — курс противоположен.
        Должен генерироваться backward (минусовая мощность), без goto_cmd."""
        self.s.robot_state.x, self.s.robot_state.y = 0, 100
        self.s.robot_state.heading = 0   # north, but target is south
        cmd = self._goto(0, 0)

        helpers = self.s._helpers_for_cmd(cmd)
        self.assertEqual(helpers, ["duration_for_distance"])

        lines = self.s._python_call_lines_for_cmd(cmd)
        joined = "\n".join(lines)
        self.assertIn("robot.move(-40, duration_for_distance(100", joined)
        self.assertNotIn("goto_cmd", joined)

    def test_arbitrary_angle_uses_full_goto(self):
        """Робот в (0,0) heading=0 (N), цель (50, 50) — bearing 45°.
        45° > ALIGN_TOL (5°), нужен полный goto_cmd."""
        self.s.robot_state.x, self.s.robot_state.y = 0, 0
        self.s.robot_state.heading = 0
        cmd = self._goto(50, 50)

        helpers = self.s._helpers_for_cmd(cmd)
        self.assertIn("goto_cmd", helpers)
        self.assertNotIn("duration_for_distance", helpers)   # transitively через goto_cmd

        lines = self.s._python_call_lines_for_cmd(cmd)
        joined = "\n".join(lines)
        self.assertIn("goto_cmd(50, 50)", joined)

    def test_already_at_target_emits_skip(self):
        """Робот уже в нужной точке (distance < 5 см) — пустая команда."""
        self.s.robot_state.x, self.s.robot_state.y = 100, 100
        self.s.robot_state.heading = 0
        cmd = self._goto(102, 101)   # 2.2 см от цели

        helpers = self.s._helpers_for_cmd(cmd)
        self.assertEqual(helpers, [])   # ничего не нужно

        lines = self.s._python_call_lines_for_cmd(cmd)
        joined = "\n".join(lines)
        self.assertIn("уже в точке", joined)
        self.assertNotIn("robot.move", joined)
        self.assertNotIn("goto_cmd", joined)

    def test_almost_aligned_within_tolerance(self):
        """Курс отличается на 3° — в пределах ALIGN_TOL=5° → forward."""
        self.s.robot_state.x, self.s.robot_state.y = 0, 0
        self.s.robot_state.heading = 3   # чуть отклонён
        cmd = self._goto(0, 100)
        mode, _, _ = self.s._goto_execution_mode(cmd)
        self.assertEqual(mode, "forward")

    def test_just_outside_tolerance_uses_full_goto(self):
        """Курс отличается на 6° — за пределами ALIGN_TOL=5° → goto."""
        self.s.robot_state.x, self.s.robot_state.y = 0, 0
        self.s.robot_state.heading = 6
        cmd = self._goto(0, 100)
        mode, _, _ = self.s._goto_execution_mode(cmd)
        self.assertEqual(mode, "goto")

    def test_codegen_uses_pre_command_state_not_post(self):
        """Регрессия. Кодоген goto должен видеть СТАРТ-позицию робота,
        а не финальную (где он окажется ПОСЛЕ выполнения goto).

        Раньше был баг: codegen вызывался в конце _dispatch (после
        выполнения), и для goto(50, 50) симулятор уже довёз робота до
        (50, 50) → distance=0 → mode="skip" → в код шло
        `# уже в точке (50, 50)` вместо реального вызова."""
        # Симулируем: робот в (0, 0) heading 45° (NE), команда goto(50, 50).
        # Так как _dispatch теперь делает codegen ПЕРЕД exec, robot_state
        # должен отражать СТАРТОВУЮ позицию.
        self.s.robot_state.x, self.s.robot_state.y = 0, 0
        self.s.robot_state.heading = 45     # NE
        cmd = self._goto(50, 50)            # bearing тоже 45° → mode="forward"

        mode, distance, _ = self.s._goto_execution_mode(cmd)
        self.assertEqual(mode, "forward",
                         "Робот в старте (0,0) NE, цель (50,50) NE → forward")
        self.assertGreater(distance, 60.0)  # ≈ 70.7 см

        lines = self.s._python_call_lines_for_cmd(cmd)
        joined = "\n".join(lines)
        # Должна быть РЕАЛЬНАЯ команда движения, не «уже в точке»
        self.assertIn("robot.move(40, duration_for_distance(", joined)
        self.assertNotIn("уже в точке", joined,
                         "Если бы codegen видел post-state (50,50), было бы skip")


class TestProgramTextParser(unittest.TestCase):
    """Парсер _parse_program_text должен принимать строки с inline-комментариями."""

    def setUp(self):
        self.s = _SessionStub(_make_cfg())

    def test_call_with_trailing_inline_comment(self):
        """Регулярка _DSL_LINE должна допускать `func(args)  # комментарий` в конце строки.
        Иначе все сгенерированные нами call-строки тихо отбрасываются."""
        text = """
forward_to_wall_cmd(40)  # вперёд до стены
face_cardinal_cmd(180)  # на юг
"""
        cmds = self.s._parse_program_text(text)
        self.assertEqual(len(cmds), 2,
                         f"должны распознаться 2 команды, а получили {len(cmds)}: "
                         f"{[c.intent for c in cmds]}")
        self.assertEqual(cmds[0].intent, "forward_to_wall")
        self.assertEqual(cmds[1].intent, "face_s")

    def test_atomic_cmds_via_cmd_marker(self):
        """Регрессия: атомарные команды (forward, steer, stop) генерируют
        сырые `robot.X(...)` вызовы. regex `_DSL_LINE` их не пропускает
        из-за точки в имени, поэтому без маркера `# CMD:` парсер находил 0
        команд → «В текстовом поле не найдено команд»."""
        text = """
robot.set_angle(13)  # руль направо на 13°
# CMD: steer(13)

# CMD: forward(20)
robot.set_angle(13)                      # руль текущий (13°)
robot.move(40, duration_for_distance(20, 40))  # вперёд 20 см
robot.stop()                            # остановка

# CMD: forward(20)
robot.set_angle(13)
robot.move(40, duration_for_distance(20, 40))
robot.stop()

# CMD: steer(0)
robot.set_angle(0)  # руль прямо
"""
        cmds = self.s._parse_program_text(text)
        intents = [c.intent for c in cmds]
        # 1×steer_right(13) + 2×forward + 1×steer_center
        self.assertEqual(intents,
                         ["steer_right", "forward", "forward", "steer_center"],
                         f"должны распознаться 4 команды, найдено: {intents}")

    def test_codegen_emits_marker_for_atomic_intents(self):
        """forward/steer/stop должны генерировать `# CMD: ...` маркер."""
        cmd = _cmd("forward", raw="Вега вперед 50 см", code="forward(50)")
        lines = self.s._python_call_lines_for_cmd(cmd)
        self.assertTrue(any(l.lstrip().startswith("# CMD: forward(50)") for l in lines),
                        f"forward должен иметь # CMD: маркер. Получено: {lines}")

    def test_codegen_no_marker_for_compound_intents(self):
        """face_cardinal_cmd / circle_cmd / goto_cmd — call-строка сама
        парсится, маркер не нужен (иначе двойной счёт)."""
        # circle_cmd
        cmd = _cmd("circle", raw="Вега вокруг", code="circle()")
        lines = self.s._python_call_lines_for_cmd(cmd)
        for line in lines:
            self.assertFalse(line.lstrip().startswith("# CMD"),
                             f"circle не должен иметь # CMD маркер: {line!r}")

    def test_full_textarea_with_helpers_and_calls(self):
        """Полный сценарий: преамбула + def-блоки + call-строки с комментариями.
        Должны быть распознаны ТОЛЬКО call-строки (def/class/etc игнорируются)."""
        text = """
# === НАЧАЛО ПРОГРАММЫ ===

# Движение вперёд до препятствия по дальномеру
def forward_to_wall_cmd(power_pct=DEFAULT_SPEED, stop_margin_mm=150):
    robot.move(power_pct)
    while True:
        dist = robot.get_laser()
        if dist is None or dist <= stop_margin_mm:
            robot.stop()
            break
        time.sleep(0.05)

robot.set_angle(0)
forward_to_wall_cmd(40)  # вперёд до стены

face_cardinal_cmd(180)  # на юг
robot.set_angle(0)
forward_to_wall_cmd(40)  # вперёд до стены
face_cardinal_cmd(180)  # на юг
"""
        cmds = self.s._parse_program_text(text)
        intents = [c.intent for c in cmds]
        # Ожидаем 4 команды (2 forward_to_wall + 2 face_s),
        # def-блок и robot.set_angle(0) должны быть проигнорированы
        self.assertEqual(intents,
                         ["forward_to_wall", "face_s",
                          "forward_to_wall", "face_s"])


class TestProgramTextRoundTrip(unittest.TestCase):
    """`_program_text()` собирает: константы → сентинель → helpers (дедуп) → вызовы."""

    def setUp(self):
        self.s = _SessionStub(_make_cfg())
        self.s._program = []

    def test_two_circles_one_helper(self):
        self.s._program = [_cmd("circle"), _cmd("circle")]
        text = self.s._program_text()
        # def circle_cmd встречается ровно один раз
        self.assertEqual(text.count("def circle_cmd("), 1,
                         "helper circle_cmd должен попасть в преамбулу один раз")
        # вызов встречается дважды
        self.assertEqual(text.count("circle_cmd("), 1 + 2,
                         "ожидаем 1 def + 2 вызова circle_cmd")

    def test_forward_then_circle_emits_both_helpers(self):
        self.s._program = [
            _cmd("forward", raw="Вега вперед 100 см", code="forward(100)"),
            _cmd("circle"),
        ]
        text = self.s._program_text()
        self.assertIn("def duration_for_distance(", text)
        self.assertIn("def circle_cmd(", text)


class TestZoneCancellation(unittest.TestCase):
    """Взаимное гашение mark_danger ↔ remove_zone — только для опасных зон.
    Зоны внимания (set_algorithm_zone) НЕ гасятся."""

    def setUp(self):
        self.s = _SessionStub(_make_cfg())
        self.s._program = []
        # _save_program ходит в БД, в тестах не нужно — заменяем заглушкой.
        self.s._save_program = lambda: None

    def test_danger_zone_inside_radius_cancels(self):
        """mark_danger(100,100,r=20) → remove_zone(105,98) внутри кольца → гасится."""
        self.s._program = [
            _cmd("mark_danger", raw="Вега опасная зона 100 100 20",
                 code="mark_danger(100,100,20)"),
        ]
        cancelled = self.s._cancel_matching_mark_danger(105.0, 98.0)
        self.assertTrue(cancelled, "точка внутри 20см от центра — должна гаситься")
        self.assertEqual(len(self.s._program), 0,
                         "опасная зона должна исчезнуть из программы")

    def test_danger_zone_outside_radius_keeps(self):
        """mark_danger(100,100,r=10) → remove_zone(150,150) за кольцом → не гасится."""
        self.s._program = [
            _cmd("mark_danger", raw="Вега опасная зона 100 100 10",
                 code="mark_danger(100,100,10)"),
        ]
        cancelled = self.s._cancel_matching_mark_danger(150.0, 150.0)
        self.assertFalse(cancelled, "точка вне кольца — не должна гасить")
        self.assertEqual(len(self.s._program), 1)

    def test_attention_zone_NEVER_cancelled(self):
        """⚠ Регрессия: set_algorithm_zone (зона внимания) НЕ должна гаситься,
        даже если remove_zone попадает в её кольцо.

        Зоны внимания ставятся алгоритмом по условию (радиация/температура),
        и их история важна для понимания работы программы."""
        self.s._program = [
            _cmd("set_algorithm_zone",
                 raw="Вега установи зону 100 100 радиус 20",
                 code="set_algorithm_zone(100,100,20)"),
        ]
        cancelled = self.s._cancel_matching_mark_danger(100.0, 100.0)
        self.assertFalse(cancelled,
                         "зона внимания никогда не гасится взаимным удалением")
        self.assertEqual(len(self.s._program), 1,
                         "set_algorithm_zone должна остаться в программе")

    def test_remove_danger_zone_voice_phrase_recognized(self):
        """⛯ Режим зон + ПКМ шлёт «Вега убрать опасную зону X Y» — должна
        распознаваться как НОВЫЙ intent remove_danger_zone (не remove_zone)."""
        from nlu import predict
        intent, conf = predict("Вега убрать опасную зону 100 50")
        self.assertEqual(intent, "remove_danger_zone",
                         "ПКМ-фраза должна давать UI-интент, а не общий remove_zone")

    def test_remove_danger_zone_distinct_from_remove_zone(self):
        from nlu import predict
        cases = [
            ("Вега убери опасную зону 0 0",     "remove_danger_zone"),
            ("Вега удали опасную зону",         "remove_danger_zone"),
            ("Вега убрать опасную зону 50 50",  "remove_danger_zone"),
            ("Вега убрать зону",                "remove_zone"),
            ("Вега удали зону 100 100",         "remove_zone"),
        ]
        for phrase, expected in cases:
            with self.subTest(phrase=phrase):
                intent, _ = predict(phrase)
                self.assertEqual(intent, expected,
                                 f"{phrase!r} → ожидался {expected}, получен {intent}")

    def test_dispatcher_blocks_mark_danger_on_playback(self):
        """⛔ Регрессия: опасные зоны нельзя создавать программно.
        При cmd.playback=True (replay через ▶ Запуск) — no-op."""
        # Читаем код _dispatch и проверяем что в ветке mark_danger
        # есть обработка cmd.playback.
        import inspect
        from session import UserSession
        src = inspect.getsource(UserSession._dispatch)
        # Должна быть проверка на playback в ветке mark_danger
        self.assertIn("if cmd.playback:", src)
        # И конкретно — игнорирование mark_danger при replay
        self.assertRegex(src,
                         r'mark_danger.*игнорируется',
                         "_dispatch должен игнорировать mark_danger при replay")

    def test_danger_among_attention_only_danger_cancelled(self):
        """Если в одной точке висят обе зоны, погасится только опасная,
        зона внимания останется в логе программы."""
        self.s._program = [
            _cmd("set_algorithm_zone", raw="Вега установи зону 50 50 радиус 30",
                 code="set_algorithm_zone(50,50,30)"),
            _cmd("mark_danger",        raw="Вега опасная зона 50 50 25",
                 code="mark_danger(50,50,25)"),
        ]
        cancelled = self.s._cancel_matching_mark_danger(50.0, 50.0)
        self.assertTrue(cancelled)
        self.assertEqual(len(self.s._program), 1)
        self.assertEqual(self.s._program[0].intent, "set_algorithm_zone",
                         "зона внимания должна остаться, опасная — пропасть")


# ────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    unittest.main(verbosity=2)
