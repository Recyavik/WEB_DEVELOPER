"""Smoke-тесты для генератора Python-кода (helpers, дедуп, парсер).

Запуск:
    cd VAGAREX
    python -m tests.test_codegen
    # или
    python -m unittest tests.test_codegen -v

ПРИМЕЧАНИЕ (Day 2 миграция на Python-API): часть классов помечена
@unittest.skip — они проверяли старую систему хелперов-def'ов и парсинга
текстаря в `_program`. С переходом на `robot.X(...)` + exec() эти модули
удаляются, тесты получат свежий аналог в Day 3 (интеграционные тесты
Python-API). Не удаляем сразу, чтобы можно было посмотреть, что было."""
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
        move_speed=40, turn_angle=36, wheel_circ_cm=28.30,
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


@unittest.skip("Day 2: проверяет старую codegen-систему, удалена с парсером")
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
            "DEFAULT_SPEED=40; DEFAULT_TURN_ANGLE=36\n"
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

    def test_helpers_use_only_real_1trex_api(self):
        """Регрессия: helpers НЕ должны использовать выдуманные robot.X
        вызовы (которых нет в 1T REX API). Список реальных примитивов —
        см. templates/system.html. Если код попадёт на железо с такой
        командой — сразу AttributeError. Проверяем явные «нельзя» —
        get_gps, wait_for_charge, report_position и т.п."""
        FORBIDDEN = [
            "robot.get_gps",
            "robot.wait_for_charge",
            "report_position(",
            "report_full_status(",
        ]
        for name, (_desc, code) in self.s._HELPER_CODE.items():
            for bad in FORBIDDEN:
                self.assertNotIn(
                    bad, code,
                    f"helper {name!r} использует НЕРЕАЛЬНЫЙ API {bad!r} — "
                    f"при копировании на железо вызов упадёт. См. system.html.")


class TestExtractCoordinates(unittest.TestCase):
    """⛯ extract_coordinates должен робастно понимать отрицательные координаты,
    запятые, скобки, пробелы вокруг минуса, ASR-«минус» словом.

    Регрессия v4.x: regex `(-?\\d+)\\s+(-?\\d+)` ломался на «- 50 -50» —
    минус отделялся пробелом от числа, group(1) захватывала «50» без знака,
    и goto уходил в (50, -50) вместо (-50, -50)."""

    def test_simple_negative(self):
        from nlu import extract_coordinates
        self.assertEqual(extract_coordinates("вега в точку -50 -50"),
                         (-50.0, -50.0))

    def test_space_after_minus(self):
        """Главный bug: «- 50» с пробелом должно сохранять знак."""
        from nlu import extract_coordinates
        self.assertEqual(extract_coordinates("в точку - 50 -50"),
                         (-50.0, -50.0))
        self.assertEqual(extract_coordinates("в точку - 50 - 50"),
                         (-50.0, -50.0))

    def test_comma_separator(self):
        from nlu import extract_coordinates
        self.assertEqual(extract_coordinates("в точку -50, -50"),
                         (-50.0, -50.0))
        self.assertEqual(extract_coordinates("в точку -50,-50"),
                         (-50.0, -50.0))

    def test_parens(self):
        from nlu import extract_coordinates
        self.assertEqual(extract_coordinates("в точку (-50, -50)"),
                         (-50.0, -50.0))

    def test_minus_as_word_asr(self):
        """ASR может распознавать знак минуса как слово «минус»."""
        from nlu import extract_coordinates
        self.assertEqual(extract_coordinates("в точку минус 50 минус 50"),
                         (-50.0, -50.0))

    def test_mixed_signs(self):
        from nlu import extract_coordinates
        self.assertEqual(extract_coordinates("в точку 100 -50"),
                         (100.0, -50.0))
        self.assertEqual(extract_coordinates("в точку -100 50"),
                         (-100.0, 50.0))

    def test_positive_unchanged(self):
        from nlu import extract_coordinates
        self.assertEqual(extract_coordinates("в точку 100 50"),
                         (100.0, 50.0))


class TestCircleVoicePhrases(unittest.TestCase):
    """«Вега круг» (одно слово) должно распознаваться как circle.
    При этом «вега кругом» — это turn_around (разворот на 180°), не circle."""

    def test_short_krug(self):
        from nlu import predict
        intent, _ = predict("вега круг")
        self.assertEqual(intent, "circle",
                         "«круг» должно быть circle (одиночное слово)")

    def test_krugom_still_turn_around(self):
        """Регрессия: «кругом» НЕ должно перехватываться правилом circle
        через substring «круг» — это команда разворота."""
        from nlu import predict
        intent, _ = predict("вега кругом")
        self.assertEqual(intent, "turn_around",
                         "«кругом» должно остаться turn_around, не circle")

    def test_vokrug_still_circle(self):
        from nlu import predict
        intent, _ = predict("вега вокруг")
        self.assertEqual(intent, "circle")


@unittest.skip("Day 2: проверяет старую codegen-систему, перепишется в Day 3")
class TestCollectHelpers(unittest.TestCase):
    """Транзитивный сбор зависимостей."""

    def setUp(self):
        self.s = _SessionStub(_make_cfg())

    def test_circle_only(self):
        helpers = self.s._collect_helpers([_cmd("circle", "Вега вокруг")])
        self.assertEqual(helpers, ["circle_cmd"])

    def test_home_pulls_all_deps(self):
        helpers = self.s._collect_helpers([_cmd("home", "Вега домой")])
        # home → goto → face_cardinal + Odometry + duration
        #            → drive_loop (closed-loop проезд)
        self.assertEqual(set(helpers),
                         {"Odometry", "duration",
                          "face_cmd", "drive_loop",
                          "goto_cmd", "home_cmd"})
        # Каждая зависимость должна стоять ДО зависимого:
        idx = {n: i for i, n in enumerate(helpers)}
        self.assertLess(idx["Odometry"], idx["face_cmd"])
        self.assertLess(idx["duration"], idx["face_cmd"])
        self.assertLess(idx["Odometry"], idx["drive_loop"])
        self.assertLess(idx["face_cmd"], idx["goto_cmd"])
        self.assertLess(idx["drive_loop"], idx["goto_cmd"])
        self.assertLess(idx["goto_cmd"], idx["home_cmd"])

    def test_goto_pulls_closed_loop(self):
        """goto_cmd должна тянуть drive_loop (коррекция курса)."""
        c = _cmd("goto", raw="Вега в точку 100 50", code="goto(100,50)")
        helpers = self.s._collect_helpers([c])
        self.assertIn("drive_loop", helpers,
                      "goto_cmd должна включать closed-loop helper")

    def test_no_dup_when_two_circles(self):
        cmds = [_cmd("circle"), _cmd("circle")]
        helpers = self.s._collect_helpers(cmds)
        self.assertEqual(helpers, ["circle_cmd"])

    def test_forward_with_dist_pulls_duration(self):
        """forward/back с дистанцией используют helper duration(D, P) для
        расчёта секунд на лету — пользователь видит явное соответствие
        дистанция ↔ время и может легко править."""
        c = _cmd("forward", raw="Вега вперед 100 см", code="forward(100)")
        helpers = self.s._collect_helpers([c])
        self.assertEqual(helpers, ["duration"])

    def test_forward_no_dist_no_helpers(self):
        c = _cmd("forward", raw="Вега вперед", code="forward()")
        helpers = self.s._collect_helpers([c])
        self.assertEqual(helpers, [])


@unittest.skip("Day 2: проверяет старую codegen-систему, перепишется в Day 3")
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
            r"def (?:duration|face_cmd|goto_cmd|home_cmd)\(", out)]
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

    def test_circle_call_has_no_inline_comment(self):
        """Регрессия: ранее в call-строки добавлялся inline `# окружность`
        — пользователь правил его, думая что это и есть команда. Теперь
        комментариев в генерируемом коде нет, источник истины — тело.
        Day 2: circle_cmd → robot.circle (новый Python-API)."""
        lines = self.s._python_call_lines_for_cmd(_cmd("circle", raw="Вега вокруг"))
        joined = "\n".join(lines)
        self.assertIn("robot.circle(", joined)
        self.assertNotIn("#", joined,
                         f"в call-строках не должно быть комментариев: {lines!r}")

    def test_face_cardinal_no_inline_comment(self):
        # Day 2: face_cmd(180) → robot.face(180).
        lines = self.s._python_call_lines_for_cmd(
            _cmd("face_s", raw="Вега на юг", code="face_s()", label="Лицом на юг"))
        self.assertEqual(lines, ["robot.face(180)"])


@unittest.skip("Day 2: проверяет старую codegen-систему, удалена с парсером")
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
        result = UserSession._parse_dsl_line("danger_here_cmd", "")
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
        """face_cmd(180) (как в сгенерированном коде) тоже работает."""
        result = UserSession._parse_dsl_line("face_cmd", "180")
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
                result = UserSession._parse_dsl_line("face_cmd", str(deg))
                self.assertIsNotNone(result, f"face_cardinal({deg}) не распознан")
                self.assertEqual(result[0], expected_intent)

    def test_face_cardinal_exact_value_routes_to_cardinal(self):
        """face_cmd(N) с углом ≈ кардинальной точки (±2°) даёт face_X.
        Иначе — новый интент face_to с точным значением (не округляем,
        чтобы пользователь мог произвольно повернуться на 70/190/350°)."""
        # 0/45/90/.../315 (или ±2° от них) → конкретный face_X
        for deg, expected in [(0, "face_n"), (180, "face_s"), (315, "face_nw"),
                               (1, "face_n"), (179, "face_s")]:
            result = UserSession._parse_dsl_line("face_cmd", str(deg))
            self.assertEqual(result[0], expected,
                             f"face_cmd({deg}) ожидался {expected}, получен {result}")
        # Произвольный угол → face_to с точным значением (НЕ округляется)
        for deg in (70, 190, 350):
            result = UserSession._parse_dsl_line("face_cmd", str(deg))
            self.assertEqual(result[0], "face_to",
                             f"face_cmd({deg}) должен быть face_to, получен {result}")
            self.assertIn(str(deg), result[1],
                          f"raw-текст для face_to должен содержать угол {deg}")


@unittest.skip("Day 2: проверяет старую codegen-систему, перепишется в Day 3")
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
        Должен генерироваться простой forward через duration(), без goto_cmd."""
        self.s.robot_state.x, self.s.robot_state.y = 0, 0
        self.s.robot_state.heading = 0   # north
        cmd = self._goto(0, 100)

        # Helpers: только duration, не goto_cmd.
        helpers = self.s._helpers_for_cmd(cmd)
        self.assertEqual(helpers, ["duration"])

        # Call lines: robot.move с явным литералом мощности.
        lines = self.s._python_call_lines_for_cmd(cmd)
        joined = "\n".join(lines)
        self.assertIn("robot.move(40, duration(100", joined)
        self.assertNotIn("goto_cmd", joined)
        self.assertNotIn("-40", joined)         # не задом

    def test_opposite_heading_emits_backward(self):
        """Робот в (0,100) heading=0 (N), цель (0, 0) — курс противоположен.
        Должен генерироваться backward (минусовая мощность), без goto_cmd."""
        self.s.robot_state.x, self.s.robot_state.y = 0, 100
        self.s.robot_state.heading = 0   # north, but target is south
        cmd = self._goto(0, 0)

        helpers = self.s._helpers_for_cmd(cmd)
        self.assertEqual(helpers, ["duration"])

        lines = self.s._python_call_lines_for_cmd(cmd)
        joined = "\n".join(lines)
        self.assertIn("robot.move(-40, duration(100", joined)
        self.assertNotIn("goto_cmd", joined)

    def test_arbitrary_angle_uses_full_goto(self):
        """Робот в (0,0) heading=0 (N), цель (50, 50) — bearing 45°.
        45° > ALIGN_TOL (5°), нужен полный goto_cmd."""
        self.s.robot_state.x, self.s.robot_state.y = 0, 0
        self.s.robot_state.heading = 0
        cmd = self._goto(50, 50)

        helpers = self.s._helpers_for_cmd(cmd)
        self.assertIn("goto_cmd", helpers)
        self.assertNotIn("duration", helpers)   # transitively через goto_cmd

        lines = self.s._python_call_lines_for_cmd(cmd)
        joined = "\n".join(lines)
        self.assertIn("goto_cmd(50, 50)", joined)

    def test_already_at_target_skips_record(self):
        """Робот уже в нужной точке (distance < 5 см) — микро-движение не
        реализовано: helpers пустые, ни одной строки кода, cmd.skip_record=True
        (команда не запишется в программу). Пользователь получит подсказку
        в журнал из диспетчера."""
        self.s.robot_state.x, self.s.robot_state.y = 100, 100
        self.s.robot_state.heading = 0
        cmd = self._goto(102, 101)   # 2.2 см от цели

        helpers = self.s._helpers_for_cmd(cmd)
        self.assertEqual(helpers, [])   # никаких хелперов

        lines = self.s._python_call_lines_for_cmd(cmd)
        self.assertEqual(lines, [],
                         "при skip строк кода быть не должно")
        self.assertTrue(cmd.skip_record,
                        "skip-команда не должна записываться в программу")

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
        # Должна быть РЕАЛЬНАЯ команда движения, не «уже в точке».
        self.assertIn("robot.move(40, duration(", joined)
        self.assertNotIn("уже в точке", joined,
                         "Если бы codegen видел post-state (50,50), было бы skip")


@unittest.skip("Day 2: проверяет старую codegen-систему, перепишется в Day 3")
class TestProgramTextParser(unittest.TestCase):
    """Парсер _parse_program_text должен принимать строки с inline-комментариями."""

    def setUp(self):
        self.s = _SessionStub(_make_cfg())

    def test_call_with_trailing_inline_comment(self):
        """Регулярка _DSL_LINE должна допускать `func(args)  # комментарий` в конце строки.
        Иначе все сгенерированные нами call-строки тихо отбрасываются."""
        text = """
to_wall_cmd(40)  # вперёд до стены
face_cmd(180)  # на юг
"""
        cmds = self.s._parse_program_text(text)
        self.assertEqual(len(cmds), 2,
                         f"должны распознаться 2 команды, а получили {len(cmds)}: "
                         f"{[c.intent for c in cmds]}")
        self.assertEqual(cmds[0].intent, "forward_to_wall")
        self.assertEqual(cmds[1].intent, "face_s")

    def test_body_distance_overrides_marker(self):
        """⛯ Пользовательская правка `robot.move(P, duration(D, ...))` в textarea
        должна переопределять distance из маркера `# CMD: forward(N)`.

        Сценарий: пользователь сгенерировал forward(40), потом руками в коде
        поменял число в duration на 80. При запуске робот должен проехать
        80 см, а не 40 (textarea — источник истины)."""
        text = """
# CMD: forward(40)
robot.set_angle(0)
robot.move(40, duration(80, 40))  # пользователь поменял 40 → 80
robot.stop()
"""
        cmds = self.s._parse_program_text(text)
        self.assertEqual(len(cmds), 1)
        self.assertEqual(cmds[0].intent, "forward")
        # raw должен содержать «80», а не «40» — distance взят из тела
        self.assertIn("80", cmds[0].raw)
        self.assertNotIn(" 40 см", cmds[0].raw)

    def test_body_distance_back_too(self):
        """То же для back — distance берётся из `robot.move(-P, duration(D, P))`."""
        text = """
# CMD: back(20)
robot.set_angle(0)
robot.move(-40, duration(120, 40))
robot.stop()
"""
        cmds = self.s._parse_program_text(text)
        self.assertEqual(len(cmds), 1)
        self.assertEqual(cmds[0].intent, "back")
        self.assertIn("120", cmds[0].raw)

    def test_body_steer_emits_separate_steer_cmd(self):
        """⛯ Пользовательская правка `robot.set_angle(N)` в теле forward должна
        давать дополнительную команду steer ПЕРЕД forward.

        Сценарий: пользователь сгенерировал forward(40), потом поменял
        `robot.set_angle(0)` на `robot.set_angle(15)`. При запуске робот
        должен сначала повернуть руль на 15°, потом проехать."""
        text = """
# CMD: forward(40)
robot.set_angle(15)                # пользователь поставил 15°
robot.move(40, duration(40, 40))
robot.stop()
"""
        cmds = self.s._parse_program_text(text)
        self.assertEqual(len(cmds), 2,
                         f"должны быть 2 команды (steer + forward), "
                         f"получено {[c.intent for c in cmds]}")
        self.assertEqual(cmds[0].intent, "steer_right")
        self.assertIn("15", cmds[0].raw)
        self.assertEqual(cmds[1].intent, "forward")

    def test_body_steer_negative_emits_steer_left(self):
        """Отрицательный угол в теле — команда steer_left."""
        text = """
# CMD: forward(40)
robot.set_angle(-20)
robot.move(40, duration(40, 40))
robot.stop()
"""
        cmds = self.s._parse_program_text(text)
        self.assertEqual(len(cmds), 2)
        self.assertEqual(cmds[0].intent, "steer_left")
        self.assertIn("20", cmds[0].raw)

    def test_body_assign_default_speed(self):
        """⛯ Присваивание `DEFAULT_SPEED = N` распознаётся как set_speed."""
        text = "DEFAULT_SPEED = 80\nrobot.set_angle(0)\n"
        cmds = self.s._parse_program_text(text)
        self.assertEqual(len(cmds), 1)
        self.assertEqual(cmds[0].intent, "set_speed")
        self.assertIn("80", cmds[0].raw)

    def test_body_assign_default_turn_angle(self):
        """⛯ Присваивание `DEFAULT_TURN_ANGLE = N` → set_turn_angle."""
        text = "DEFAULT_TURN_ANGLE = 25\n"
        cmds = self.s._parse_program_text(text)
        self.assertEqual(len(cmds), 1)
        self.assertEqual(cmds[0].intent, "set_turn_angle")
        self.assertIn("25", cmds[0].raw)

    def test_body_steer_zero_no_extra_cmd(self):
        """Если в теле `set_angle(0)` — лишняя steer-команда НЕ нужна
        (forward сам поставит руль в 0)."""
        text = """
# CMD: forward(40)
robot.set_angle(0)
robot.move(40, duration(40, 40))
robot.stop()
"""
        cmds = self.s._parse_program_text(text)
        self.assertEqual(len(cmds), 1, "только forward, без лишнего steer")
        self.assertEqual(cmds[0].intent, "forward")

    def test_body_steer_clamps_above_45(self):
        """Угол > 45° зажимается до 45 (физический предел сервопривода)."""
        text = """
# CMD: forward(40)
robot.set_angle(90)
robot.move(40, duration(80, 40))
robot.stop()
"""
        cmds = self.s._parse_program_text(text)
        self.assertEqual(len(cmds), 2)
        self.assertEqual(cmds[0].intent, "steer_right")
        # 90 → должно зажаться до 45 (см. extract_angle)
        self.assertIn("45", cmds[0].code)
        # distance тоже из тела — 80, не 40 из маркера
        self.assertEqual(cmds[1].intent, "forward")
        self.assertIn("80", cmds[1].raw)

    def test_marker_is_decorative_body_is_truth(self):
        """⛯ Маркер `# CMD:` — декоративен. Источник истины — тело.

        Регрессия v4.0+: парсер больше не fallback'ает на аргумент маркера.
        Если пользователь поправил тело так, что там нет `duration(D, ...)`,
        парсится то, что реально написано в `robot.move(...)` — continuous
        режим без distance (как и есть на железе). Из аргумента маркера
        ничего не подтягивается."""
        text = """
# CMD: forward(15)
robot.set_angle(0)
robot.move(40, 1.0)                # без duration() — continuous 1 сек
"""
        cmds = self.s._parse_program_text(text)
        # Парсится только тело: set_angle(0) при last_steer=0 → skip,
        # robot.move(40, 1.0) → forward без distance, robot.stop() — нет.
        self.assertEqual(len(cmds), 1)
        self.assertEqual(cmds[0].intent, "forward")
        # Дистанции в raw не должно быть — её нет в теле.
        self.assertNotIn("15", cmds[0].raw,
                         "args маркера НЕ должны просачиваться в команду")

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
robot.move(40, duration(20, 40))  # вперёд 20 см
robot.stop()                            # остановка

# CMD: forward(20)
robot.set_angle(13)
robot.move(40, duration(20, 40))
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

    def test_codegen_no_marker_for_atomic_intents(self):
        """Регрессия: ранее forward/steer/stop генерировали `# CMD: …`
        маркер для парсера. Сейчас маркеров НЕТ — пользователь правил
        этот «комментарий-команду», думая что это исполняемая строка,
        и не понимал почему правки не применяются. Источник истины —
        тело программы (`robot.move(...)` и т.п.)."""
        cmd = _cmd("forward", raw="Вега вперед 50 см", code="forward(50)")
        lines = self.s._python_call_lines_for_cmd(cmd)
        for line in lines:
            self.assertFalse(line.lstrip().startswith("# CMD"),
                             f"маркер # CMD должен быть удалён: {line!r}")
        # И никаких inline-комментариев тоже:
        joined = "\n".join(lines)
        self.assertNotIn("#", joined,
                         f"call-строки forward не должны содержать комментариев: {lines!r}")

    def test_codegen_no_marker_for_compound_intents(self):
        """face_cmd / circle_cmd / goto_cmd — без маркеров и без
        inline-комментариев."""
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
def to_wall_cmd(power_pct=DEFAULT_SPEED, stop_margin_mm=150):
    robot.move(power_pct)
    while True:
        dist = robot.get_laser()
        if dist is None or dist <= stop_margin_mm:
            robot.stop()
            break
        time.sleep(0.05)

robot.set_angle(0)
to_wall_cmd(40)  # вперёд до стены

face_cmd(180)  # на юг
robot.set_angle(0)
to_wall_cmd(40)  # вперёд до стены
face_cmd(180)  # на юг
"""
        cmds = self.s._parse_program_text(text)
        intents = [c.intent for c in cmds]
        # Ожидаем 4 команды (2 forward_to_wall + 2 face_s),
        # def-блок и robot.set_angle(0) должны быть проигнорированы
        self.assertEqual(intents,
                         ["forward_to_wall", "face_s",
                          "forward_to_wall", "face_s"])


@unittest.skip("Day 2: проверяет старую codegen-систему, перепишется в Day 3")
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
        """forward с дистанцией тянет duration(), а circle тянет circle_cmd —
        оба helper'a должны попасть в преамбулу."""
        self.s._program = [
            _cmd("forward", raw="Вега вперед 100 см", code="forward(100)"),
            _cmd("circle"),
        ]
        text = self.s._program_text()
        self.assertIn("def duration(", text)
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

    def test_dispatcher_mark_danger_idempotent_on_playback(self):
        """⏵ Регрессия: при cmd.playback=True диспетчер должен ВОССТАНАВЛИВАТЬ
        опасные зоны, если их ещё нет, и пропускать дубликаты, если уже есть.
        Раньше было полное игнорирование — это ломало replay."""
        import inspect
        from session import UserSession
        src = inspect.getsource(UserSession._dispatch)
        # В ветке mark_danger должна быть проверка playback и обращение
        # к идемпотентному helper'у _has_danger_zone_at.
        self.assertIn("if cmd.playback:", src)
        self.assertIn("_has_danger_zone_at", src,
                      "при playback должна быть проверка дубликата")
        # Helper должен быть определён на классе.
        self.assertTrue(hasattr(UserSession, "_has_danger_zone_at"),
                        "UserSession._has_danger_zone_at должен существовать")

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


@unittest.skip("Day 2: проверяет старую codegen-систему, удалена с парсером")
class TestFaceTo(unittest.TestCase):
    """Регрессии для нового intent `face_to` (поворот на месте на любой угол).

    Все эти фразы должны быть ОДНОЙ командой — face_cmd(N):
      «поверни на N» / «повернись на N» / «поворот на N»
      «разверни на N» / «развернись на N»
      «лицом на N»  (с ЧИСЛОМ; без числа — это сторона света)
    """

    def setUp(self):
        self.s = _SessionStub(_make_cfg())

    # ── NLU: фразировки → один intent face_to ───────────────────────────

    def test_voice_phrases_route_to_face_to(self):
        from nlu import predict
        cases = [
            "вега поверни на 70",
            "вега повернись на 70",
            "вега поворот на 70",
            "вега разверни на 70",
            "вега развернись на 70",
            "вега развернись на 90 градусов",
            "вега лицом на 70",
            "вега лицом на 137",
            "вега повернись на -30",       # отрицательный угол
            # Инфинитивы — раньше не понимались, теперь обязательно должны.
            "вега повернуть на 70",
            "вега повернуться на 70",
            "вега развернуть на 70",
            "вега развернуться на 70",
        ]
        for phrase in cases:
            with self.subTest(phrase=phrase):
                intent, _ = predict(phrase)
                self.assertEqual(
                    intent, "face_to",
                    f"{phrase!r} → ожидался face_to, получен {intent}")

    def test_infinitive_without_number_is_turn_around(self):
        """Инфинитивы «развернуться»/«повернуться» без числа = разворот 180°,
        как «развернись» / «разворот»."""
        from nlu import predict
        for phrase in ["вега развернуться", "вега повернуться",
                       "вега повернись"]:
            with self.subTest(phrase=phrase):
                intent, _ = predict(phrase)
                self.assertEqual(intent, "turn_around",
                                 f"{phrase!r} должен быть turn_around, получен {intent}")

    def test_litsom_na_cardinal_still_works(self):
        """«лицом на восток/север/...» БЕЗ числа должно идти в face_X,
        а не перехватываться правилом face_to (callable-предикат
        срабатывает только при наличии цифры после «на»)."""
        from nlu import predict
        cases = [
            ("вега лицом на восток",       "face_e"),
            ("вега лицом на север",        "face_n"),
            ("вега лицом на юго-запад",    "face_sw"),
            ("вега на запад",              "face_w"),
            ("вега к северу",              "face_n"),
        ]
        for phrase, expected in cases:
            with self.subTest(phrase=phrase):
                intent, _ = predict(phrase)
                self.assertEqual(
                    intent, expected,
                    f"{phrase!r} → ожидался {expected}, получен {intent}")

    def test_razvernis_without_number_still_turn_around(self):
        """«Вега развернись» без числа = разворот 180° (turn_around),
        НЕ face_to. Регрессия: правило face_to идёт раньше turn_around,
        но без «на N» оно не должно матчиться."""
        from nlu import predict
        intent, _ = predict("вега развернись")
        self.assertEqual(intent, "turn_around")

    # ── extract_face_angle ──────────────────────────────────────────────

    def test_extract_face_angle_all_verbs(self):
        from nlu import extract_face_angle
        cases = [
            ("поверни на 70",                70),
            ("повернись на 45",              45),
            ("поворот на 180",               180),
            ("разверни на 90",               90),
            ("развернись на 270",            270),
            ("лицом на 137",                 137),
            ("повернись на -30",             330),  # модуль 360
            ("развернись на 90 градусов",    90),
        ]
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(extract_face_angle(text), expected)

    def test_extract_face_angle_returns_none_when_no_number(self):
        from nlu import extract_face_angle
        self.assertIsNone(extract_face_angle("лицом на восток"))
        self.assertIsNone(extract_face_angle("развернись"))
        self.assertIsNone(extract_face_angle("просто текст"))

    # ── _build_cmd ──────────────────────────────────────────────────────

    def test_build_cmd_emits_face_cmd_call(self):
        cmd = self.s._build_cmd("face_to", "вега поверни на 70")
        self.assertIsNotNone(cmd, "_build_cmd для face_to не должен возвращать None")
        self.assertEqual(cmd.intent, "face_to")
        self.assertIn("face_cmd(70)", cmd.code)
        self.assertIn("70", cmd.label)

    def test_build_cmd_returns_none_without_angle(self):
        """_build_cmd для face_to без угла → None (нечего исполнять)."""
        cmd = self.s._build_cmd("face_to", "вега поверни на")
        self.assertIsNone(cmd)

    # ── Codegen: face_to → face_cmd(N) ──────────────────────────────────

    def test_codegen_emits_face_cmd_line(self):
        # Day 2: face_cmd(N) → robot.face(N).
        cmd = _cmd("face_to", raw="вега лицом на 70",
                   code="robot.face(70)", label="Поворот на 70°")
        lines = self.s._python_call_lines_for_cmd(cmd)
        joined = "\n".join(lines)
        self.assertIn("robot.face(70)", joined,
                      "Codegen должен эмитить robot.face(70) для face_to")

    def test_helpers_for_face_to_includes_face_cmd(self):
        cmd = _cmd("face_to", raw="вега поверни на 70",
                   code="face_cmd(70)")
        helpers = self.s._helpers_for_cmd(cmd)
        self.assertIn("face_cmd", helpers,
                      "face_to должен тянуть в преамбулу def face_cmd")

    # ── Parser: face_cmd(N) round-trip ──────────────────────────────────

    def test_parser_face_cmd_arbitrary_to_face_to(self):
        """face_cmd(70) → face_to (произвольный угол сохраняется в raw)."""
        result = UserSession._parse_dsl_line("face_cmd", "70")
        self.assertEqual(result[0], "face_to")
        self.assertIn("70", result[1])

    def test_parser_face_cmd_cardinal_to_face_X(self):
        """face_cmd(0/45/90/...) → конкретный face_X (точное совпадение)."""
        for deg, expected in [(0, "face_n"), (45, "face_ne"), (90, "face_e"),
                               (180, "face_s"), (315, "face_nw")]:
            with self.subTest(deg=deg):
                result = UserSession._parse_dsl_line("face_cmd", str(deg))
                self.assertEqual(result[0], expected)


class TestStableCommandSurface(unittest.TestCase):
    """Smoke-тест: каждый известный intent должен либо строиться через
    _build_cmd в валидный RobotCmd, либо явно возвращать None по причине
    «без обязательного аргумента». Никаких исключений."""

    def setUp(self):
        self.s = _SessionStub(_make_cfg())

    # Карта: intent → (raw для теста, ожидание None?)
    INTENT_PROBES = [
        # Движение
        ("forward",            "вега вперед 100 см",          False),
        ("back",               "вега назад 50 см",            False),
        ("forward_to_wall",    "вега вперед до упора",        False),
        ("backward_to_wall",   "вега назад до упора",         False),
        ("brake",              "вега тормоз",                 False),
        ("stop",               "вега стоп",                   False),
        # Руль
        ("steer_right",        "вега направо 20",             False),
        ("steer_left",         "вега налево 20",              False),
        ("steer_right_small",  "вега правее",                 False),
        ("steer_left_small",   "вега левее",                  False),
        ("steer_center",       "вега руль прямо",             False),
        ("set_speed",          "вега скорость 50",            False),
        # Маневры
        ("turn_around",        "вега разворот",               False),
        ("turn_around_place",  "вега разворот на месте",      False),
        ("circle",             "вега вокруг",                 False),
        ("figure_eight",       "вега восьмерка",              False),
        ("spiral_out",         "вега спираль наружу",         False),
        ("spiral_in",          "вега спираль внутрь",         False),
        ("bypass_right",       "вега объезд справа",          False),
        ("bypass_left",        "вега объезд слева",           False),
        # Навигация
        ("home",               "вега домой",                  False),
        ("goto",               "вега в точку 100 50",         False),
        ("goto",               "вега в точку",                True),   # без коорд
        # Стороны света и поворот на угол
        ("face_n",             "вега север",                  False),
        ("face_ne",            "вега северо-восток",          False),
        ("face_e",             "вега восток",                 False),
        ("face_se",            "вега юго-восток",             False),
        ("face_s",             "вега юг",                     False),
        ("face_sw",            "вега юго-запад",              False),
        ("face_w",             "вега запад",                  False),
        ("face_nw",            "вега северо-запад",           False),
        ("face_to",            "вега поверни на 70",          False),
        ("face_to",            "вега лицом на 137",           False),
        ("face_to",            "вега поверни на",             True),   # без угла
        ("set_course",         "вега курс 90",                False),
        ("set_course",         "вега курс",                   True),   # без угла
        # Зоны
        ("mark_danger",        "вега опасная зона 100 50 20", False),
        ("mark_danger",        "вега опасная зона",           False),  # under robot
        ("set_algorithm_zone", "вега установи зону 100 100",  False),
        # Без координат — теперь валидно: ставит в текущей позиции робота.
        ("set_algorithm_zone", "вега установи зону",          False),
        ("remove_zone",        "вега убрать зону",            False),
        ("remove_zone",        "вега убрать зону 100 100",    False),
        ("remove_danger_zone", "вега убрать опасную зону 100 50", False),
        # Режимы / сервис
        ("mode_inspector",     "вега инспектор",              False),
        ("mode_cautious",      "вега осторожно",              False),
        ("path_show",          "вега показать путь",          False),
        ("path_hide",          "вега скрыть путь",            False),
        ("reset",              "вега новое поле",             False),
        ("recharge",           "вега зарядить",               False),
        ("pause",              "вега пауза 2",                False),
        ("light_on",           "вега включи свет",            False),
        ("light_off",          "вега выключи свет",           False),
        ("light_color",        "вега свет красный",           False),
        ("report_pos",         "вега где ты",                 False),
        ("report_status",      "вега статус робота",          False),
    ]

    def test_every_intent_builds_or_explicitly_returns_none(self):
        for intent, raw, expect_none in self.INTENT_PROBES:
            with self.subTest(intent=intent, raw=raw):
                try:
                    cmd = self.s._build_cmd(intent, raw)
                except Exception as e:
                    self.fail(f"_build_cmd({intent!r}, {raw!r}) "
                              f"бросил {type(e).__name__}: {e}")
                if expect_none:
                    self.assertIsNone(
                        cmd, f"{intent!r} должен вернуть None для {raw!r}")
                else:
                    self.assertIsNotNone(
                        cmd, f"{intent!r} вернул None для {raw!r}")
                    self.assertEqual(cmd.intent, intent)
                    self.assertTrue(cmd.code, f"{intent!r}: пустой code")
                    self.assertTrue(cmd.label, f"{intent!r}: пустой label")

    def test_codegen_for_built_cmds_does_not_crash(self):
        """Пайплайн: _build_cmd → _python_call_lines_for_cmd.
        Регрессия: ни один known intent не должен бросать исключение
        при codegen. Day 2: _helpers_for_cmd удалён вместе с парсером."""
        for intent, raw, expect_none in self.INTENT_PROBES:
            if expect_none:
                continue
            with self.subTest(intent=intent, raw=raw):
                cmd = self.s._build_cmd(intent, raw)
                if cmd is None:
                    continue   # tolerated for some optional argument cases
                try:
                    self.s._python_call_lines_for_cmd(cmd)
                except Exception as e:
                    self.fail(f"codegen для {intent!r} {raw!r} бросил "
                              f"{type(e).__name__}: {e}")

    @unittest.skip("Day 2: парсер _parse_dsl_line удалён вместе с системой хелперов")
    def test_dsl_parser_does_not_crash_on_known_calls(self):
        pass


# ────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    unittest.main(verbosity=2)
