"""Тесты генератора миссий (mission_generator.py).

Запуск:
    cd VAGAREX
    python -m unittest tests.test_mission_generator -v
"""
import json
import math
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from mission_generator import (
    WorldGeom, generate_mission,
    _wall_clearance_cm, _inside_field,
    _format_description, _format_reference_code,
)  # noqa: E402


def _geom_default() -> WorldGeom:
    return WorldGeom(
        world_w_cm=500, world_h_cm=500, wall_thick_cm=5,
        robot_w_cm=12, robot_l_cm=20, safety_margin_cm=5,
        start_x=0, start_y=0, start_heading=0,
    )


class TestLevel1Shape(unittest.TestCase):
    """Структура сгенерированной миссии level 1."""

    def test_level_1_has_2_or_3_waypoints(self):
        for seed in range(20):
            with self.subTest(seed=seed):
                m = generate_mission(level=1, geom=_geom_default(), seed=seed)
                wp = json.loads(m["waypoints"])
                self.assertGreaterEqual(len(wp), 2,
                                        f"seed={seed}: меньше 2 точек")
                self.assertLessEqual(len(wp), 3,
                                     f"seed={seed}: больше 3 точек")

    def test_level_1_no_zones(self):
        for seed in range(10):
            with self.subTest(seed=seed):
                m = generate_mission(level=1, seed=seed)
                self.assertEqual(json.loads(m["danger_zones"]), [])
                self.assertEqual(json.loads(m["actions_required"]), [])

    def test_required_fields_present(self):
        m = generate_mission(level=1, seed=42)
        for key in ("level", "title", "description", "waypoints",
                    "danger_zones", "actions_required",
                    "reference_voice", "reference_code", "safety_margin_cm"):
            self.assertIn(key, m, f"отсутствует поле {key!r}")

    def test_level_1_title_empty_and_description_meaningful(self):
        """Title оставляем пустым — пользователь введёт сам, fallback
        «Миссия #N» делается в /missions/save. Описание в формате:
        🟢 Начало → 📍 Контрольные точки → ⭐."""
        m = generate_mission(level=1, seed=7)
        self.assertEqual(m["title"], "",
                         "генератор не должен задавать title — это делает "
                         "пользователь или сервер при сохранении")
        desc = m["description"]
        # Стартовая точка
        self.assertIn("Начало маршрута", desc)
        # Контрольные точки с координатами
        self.assertIn("Контрольные точки", desc)
        self.assertIn("(", desc)
        # Звёзды
        self.assertIn("звёзды", desc.lower())
        # И НЕ содержит подсказок «forward_cmd» / «face_cmd»
        self.assertNotIn("forward_cmd", desc)
        self.assertNotIn("face_cmd",    desc)

    def test_reference_voice_and_code_are_lists_strings(self):
        m = generate_mission(level=1, seed=1)
        voice = json.loads(m["reference_voice"])
        self.assertIsInstance(voice, list)
        self.assertGreater(len(voice), 0)
        for phrase in voice:
            self.assertTrue(phrase.startswith("Вега"),
                            f"эталонная фраза должна начинаться с 'Вега': {phrase!r}")
        code = m["reference_code"]
        self.assertIsInstance(code, str)
        self.assertIn("(", code, "эталонный код должен содержать вызовы")


class TestWaypointsInsideField(unittest.TestCase):
    """Все waypoints должны быть внутри поля с запасом 2×габариты."""

    def test_all_waypoints_within_safe_field(self):
        g = _geom_default()
        margin = _wall_clearance_cm(g)
        half_w = g.world_w_cm / 2.0
        half_h = g.world_h_cm / 2.0
        for seed in range(30):
            m = generate_mission(level=1, geom=g, seed=seed)
            wp = json.loads(m["waypoints"])
            for x, y in wp:
                with self.subTest(seed=seed, x=x, y=y):
                    self.assertGreaterEqual(x, -half_w + margin - 0.01)
                    self.assertLessEqual(x,    half_w - margin + 0.01)
                    self.assertGreaterEqual(y, -half_h + margin - 0.01)
                    self.assertLessEqual(y,    half_h - margin + 0.01)


class TestDeterminism(unittest.TestCase):
    """Один и тот же seed → одна и та же миссия (для воспроизводимости)."""

    def test_same_seed_same_mission(self):
        a = generate_mission(level=1, seed=123)
        b = generate_mission(level=1, seed=123)
        self.assertEqual(a["waypoints"],       b["waypoints"])
        self.assertEqual(a["reference_voice"], b["reference_voice"])
        self.assertEqual(a["reference_code"],  b["reference_code"])

    def test_different_seeds_different_missions(self):
        a = generate_mission(level=1, seed=1)
        b = generate_mission(level=1, seed=999)
        # С большой вероятностью waypoints разные. Иногда могут совпасть
        # — проверяем что хотя бы один из reference != тот же.
        same_wp    = a["waypoints"]       == b["waypoints"]
        same_voice = a["reference_voice"] == b["reference_voice"]
        self.assertFalse(same_wp and same_voice,
                         "Разные seed дали идентичную миссию")


class TestStartPoint(unittest.TestCase):
    """Регрессии: генератор должен корректно учитывать пользовательский
    start_x/start_y, а не предполагать (0, 0).

    Хороший тест бьёт по этому багу: SVG-превью брал старт из path[0],
    и если path[0] не совпадал со start_x/start_y из настроек —
    зелёная точка рисовалась в начале координат вместо реальной.
    """

    def test_path_starts_at_geom_start(self):
        """Первая точка full_path == (start_x, start_y) для любого старта."""
        for sx, sy in [(0, 0), (100, 50), (-150, 80), (200, -200)]:
            with self.subTest(start=(sx, sy)):
                g = WorldGeom(world_w_cm=600, world_h_cm=600,
                              robot_w_cm=12, robot_l_cm=20,
                              start_x=sx, start_y=sy, start_heading=0)
                m = generate_mission(level=1, geom=g, seed=42)
                path = json.loads(m["path"])
                self.assertGreaterEqual(len(path), 1,
                                        f"path не должен быть пустым (start={sx},{sy})")
                self.assertAlmostEqual(path[0][0], sx, places=1,
                                       msg=f"path[0].x ≠ start_x для start=({sx},{sy})")
                self.assertAlmostEqual(path[0][1], sy, places=1,
                                       msg=f"path[0].y ≠ start_y для start=({sx},{sy})")

    def test_description_includes_offset_start_coords(self):
        """🟢 Начало маршрута содержит фактические start_x/start_y, не (0,0)."""
        g = WorldGeom(world_w_cm=600, world_h_cm=600, start_x=150, start_y=-80)
        m = generate_mission(level=1, geom=g, seed=7)
        self.assertIn("Начало маршрута (150, -80)", m["description"],
                      f"описание не содержит start-координаты: {m['description']!r}")

    def test_waypoints_reflect_start_offset(self):
        """С большим смещением старта waypoints НЕ должны кучковаться у нуля —
        они идут от старта в координатах мира."""
        g = WorldGeom(world_w_cm=600, world_h_cm=600,
                      robot_w_cm=12, robot_l_cm=20,
                      start_x=200, start_y=200, start_heading=0)
        # Подберём seed чтобы хотя бы одна waypoint была далеко от (0, 0).
        m = generate_mission(level=1, geom=g, seed=3)
        wp = json.loads(m["waypoints"])
        self.assertTrue(any(abs(x) > 50 or abs(y) > 50 for x, y in wp),
                        f"при start=(200,200) хотя бы одна waypoint должна "
                        f"быть далеко от (0,0); получены: {wp}")


class TestDescriptionFormat(unittest.TestCase):
    """`_format_description` — структура: 🟢 → 📍 → 📌 → ❌ → ⚠ → ⭐.

    Все блоки опциональные кроме первого (🟢) и последнего (⭐).
    Регрессии — формат менялся пять раз за последние коммиты.
    """

    def test_minimal_only_start_and_stars(self):
        """Пустые waypoints и actions — только 🟢 и ⭐."""
        d = _format_description(level=1, waypoints=[], actions=[],
                                start_x=0, start_y=0)
        self.assertIn("Начало маршрута (0, 0)", d)
        self.assertIn("⭐", d)
        # Нет лишних блоков
        self.assertNotIn("Контрольные точки", d)
        self.assertNotIn("Установите зоны", d)
        self.assertNotIn("Опасные зоны", d)
        self.assertNotIn("Удалите", d)

    def test_with_waypoints_includes_coords_and_count(self):
        d = _format_description(level=1,
                                waypoints=[[100, 50], [200, -30]],
                                actions=[], start_x=0, start_y=0)
        self.assertIn("Контрольные точки маршрута (2 шт.)", d)
        self.assertIn("(100, 50)", d)
        self.assertIn("(200, -30)", d)

    def test_place_attention_actions_appear_as_pin(self):
        d = _format_description(level=2, waypoints=[],
                                actions=[
                                    {"type": "place_attention", "x": 50, "y": 50},
                                    {"type": "place_attention", "x": 100, "y": 100},
                                ],
                                start_x=0, start_y=0)
        self.assertIn("Установите зоны внимания", d)
        self.assertIn("(50, 50)", d)
        self.assertIn("(100, 100)", d)
        # Только place_attention — без блока ❌
        self.assertNotIn("Удалите", d)

    def test_remove_actions_produce_separate_lines(self):
        """remove_danger и remove_attention идут двумя отдельными строками
        с правильными счётчиками."""
        d = _format_description(level=4, waypoints=[],
                                actions=[
                                    {"type": "remove_danger",    "x": 0, "y": 0},
                                    {"type": "remove_danger",    "x": 1, "y": 1},
                                    {"type": "remove_attention", "x": 2, "y": 2},
                                ],
                                start_x=0, start_y=0)
        self.assertIn("Удалите все опасные зоны (2 шт)", d)
        self.assertIn("Удалите зоны внимания (1 шт)", d)

    def test_danger_zones_block_with_count(self):
        d = _format_description(level=3, waypoints=[],
                                actions=[],
                                danger_zones=[[100, 0, 20], [-50, 80, 15]],
                                start_x=0, start_y=0)
        self.assertIn("Опасные зоны на карте (2 шт.)", d)
        self.assertIn("(100, 0)", d)
        self.assertIn("(-50, 80)", d)
        self.assertIn("Не задевайте", d)

    def test_block_order_is_start_waypoints_actions_zones_stars(self):
        """Стабильный порядок блоков (на нём держится UI-парсинг описания)."""
        d = _format_description(level=4, waypoints=[[100, 0]],
                                actions=[{"type": "place_attention", "x": 50, "y": 50}],
                                danger_zones=[[200, 0, 10]],
                                start_x=0, start_y=0)
        i_start = d.index("Начало маршрута")
        i_wp    = d.index("Контрольные точки")
        i_pin   = d.index("Установите зоны")
        i_dz    = d.index("Опасные зоны на карте")
        i_star  = d.index("⭐")
        self.assertLess(i_start, i_wp)
        self.assertLess(i_wp,    i_pin)
        self.assertLess(i_pin,   i_dz)
        self.assertLess(i_dz,    i_star)

    def test_no_command_hints_leak_into_description(self):
        """Регрессия: ранее описание содержало подсказки `forward_cmd(N)` /
        `face_cmd(N)` — это эталонное решение, его НЕ должно быть видно
        пользователю. Подсказки идут только в reference_code."""
        d = _format_description(level=1, waypoints=[[100, 0]], actions=[],
                                start_x=0, start_y=0)
        for token in ("forward_cmd", "face_cmd", "goto_cmd",
                      "back_cmd", "circle_cmd"):
            self.assertNotIn(token, d,
                             f"подсказка {token!r} утекла в описание миссии")


class TestReferenceCode(unittest.TestCase):
    """`_format_reference_code` — admin-only outline."""

    def test_empty_steps_returns_no_actions_marker(self):
        out = _format_reference_code([])
        self.assertEqual(out, "# (нет действий)\n")

    def test_steps_wrapped_in_admin_header(self):
        out = _format_reference_code(["forward_cmd(100)", "face_n_cmd()"])
        self.assertIn("admin-only", out)
        self.assertIn("forward_cmd(100)", out)
        self.assertIn("face_n_cmd()", out)
        # Каждый шаг — отдельная строка
        self.assertEqual(out.count("\n"), out.strip().count("\n") + 1,
                         "должен заканчиваться единственным переводом строки")


class TestGeomHelpers(unittest.TestCase):
    def test_wall_clearance_uses_max_robot_dim(self):
        g = WorldGeom(robot_w_cm=12, robot_l_cm=20)
        self.assertEqual(_wall_clearance_cm(g), 40.0)
        g = WorldGeom(robot_w_cm=20, robot_l_cm=12)
        self.assertEqual(_wall_clearance_cm(g), 40.0)

    def test_inside_field_respects_clearance(self):
        g = _geom_default()
        # margin = 40см, поле 500×500 → допустимая зона [-210, +210]
        self.assertTrue(_inside_field(0, 0, g))
        self.assertTrue(_inside_field(200, 200, g))
        self.assertFalse(_inside_field(220, 0, g))    # за пределами
        self.assertFalse(_inside_field(0, -240, g))


if __name__ == "__main__":
    unittest.main(verbosity=2)
