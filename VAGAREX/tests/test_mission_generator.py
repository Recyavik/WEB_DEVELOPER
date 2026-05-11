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
    WorldGeom, generate_mission, _wall_clearance_cm, _inside_field,
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
