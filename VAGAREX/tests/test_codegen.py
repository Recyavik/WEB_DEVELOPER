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


class _SessionStub(UserSession):
    """Минимальная версия UserSession, минующая __init__ (не нужны БД/WebSocket)."""
    def __init__(self, cfg: UserCfg):
        # Намеренно НЕ зовём super().__init__ — он бы поднял World/Driver/etc.
        self.cfg = cfg
        self.robot_state = _RobotStateStub()


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
            "START_X_CM=0; START_Y_CM=100; START_HEADING_DEG=0\n"
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
        # Порядок: каждая зависимость должна стоять ДО зависимого.
        self.assertEqual(set(helpers),
                         {"Odometry", "duration_for_distance",
                          "face_cardinal_cmd", "goto_cmd", "home_cmd"})
        # Проверяем что зависимости идут до своих зависимых:
        idx = {n: i for i, n in enumerate(helpers)}
        self.assertLess(idx["Odometry"], idx["face_cardinal_cmd"])
        self.assertLess(idx["duration_for_distance"], idx["face_cardinal_cmd"])
        self.assertLess(idx["face_cardinal_cmd"], idx["goto_cmd"])
        self.assertLess(idx["goto_cmd"], idx["home_cmd"])

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


# ────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    unittest.main(verbosity=2)
