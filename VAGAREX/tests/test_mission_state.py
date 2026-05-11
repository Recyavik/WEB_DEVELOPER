"""Тесты mission_state.py — runtime-состояние активной миссии.

Изолированные unit-тесты: проверяем геометрию (расстояние до пути),
обновление коэффициента, обнаружение waypoint visits, матчинг действий,
финальный подсчёт звёзд.
"""
import math
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from mission_state import (
    ActiveMission, dist_to_path, path_total_length,
    _dist_point_to_segment,
    WAYPOINT_TOLERANCE_CM, ACTION_TOLERANCE_CM, COEFF_STEP_PER_TICK,
)  # noqa: E402


def _mk_mission(**overrides) -> ActiveMission:
    """Конструктор тестового ActiveMission с разумными дефолтами."""
    defaults = dict(
        mission_id=1, run_id=None, user_id=1,
        waypoints=[(100.0, 0.0), (100.0, 100.0)],
        danger_zones=[],
        actions_required=[],
        safety_margin_cm=5.0,
        start_x=0.0, start_y=0.0,
    )
    defaults.update(overrides)
    return ActiveMission(**defaults)


class TestGeometry(unittest.TestCase):
    """Расстояние от точки до отрезка / ломаной."""

    def test_point_on_segment(self):
        # точка на середине отрезка [(0,0)-(10,0)]
        self.assertAlmostEqual(_dist_point_to_segment(5, 0, 0, 0, 10, 0), 0)

    def test_point_perpendicular_to_segment(self):
        # точка (5, 7) — перпендикулярно к отрезку [(0,0)-(10,0)]
        self.assertAlmostEqual(_dist_point_to_segment(5, 7, 0, 0, 10, 0), 7)

    def test_point_beyond_segment_endpoint(self):
        # точка (15, 0) за концом отрезка [(0,0)-(10,0)] — расстояние до B=(10,0)
        self.assertAlmostEqual(_dist_point_to_segment(15, 0, 0, 0, 10, 0), 5)

    def test_dist_to_path_picks_nearest_segment(self):
        path = [(0, 0), (100, 0), (100, 100)]
        # Точка близко ко второму отрезку
        self.assertAlmostEqual(dist_to_path(105, 50, path), 5)
        # Точка близко к первому отрезку
        self.assertAlmostEqual(dist_to_path(50, 3, path), 3)

    def test_path_total_length(self):
        path = [(0, 0), (100, 0), (100, 100)]
        self.assertAlmostEqual(path_total_length(path), 200)


class TestCoefficientUpdate(unittest.TestCase):
    """Коэффициент растёт внутри margin, падает вне него, в пределах [0,1]."""

    def test_in_margin_keeps_or_grows_to_max_1(self):
        m = _mk_mission(safety_margin_cm=5.0)
        m.coefficient = 0.5
        # Робот ровно на траектории (на отрезке start→wp1).
        m.update_coefficient(50, 0)
        self.assertGreater(m.coefficient, 0.5)
        # При непрерывном «в margin» доходит до 1.0
        for _ in range(1000):
            m.update_coefficient(50, 0)
        self.assertEqual(m.coefficient, 1.0)

    def test_out_of_margin_decreases_to_zero(self):
        m = _mk_mission(safety_margin_cm=5.0)
        # 30 см от траектории — далеко вне margin
        for _ in range(1000):
            m.update_coefficient(50, 30)
        self.assertEqual(m.coefficient, 0.0)

    def test_deviation_counted_once_per_excursion(self):
        m = _mk_mission(safety_margin_cm=5.0)
        # Сначала в margin
        m.update_coefficient(50, 0)
        self.assertEqual(m.deviations, 0)
        # Уехали — один deviation
        m.update_coefficient(50, 30)
        self.assertEqual(m.deviations, 1)
        # Ещё раз вне margin — deviation НЕ инкрементируется (тот же эпизод)
        m.update_coefficient(50, 30)
        self.assertEqual(m.deviations, 1)
        # Вернулись на траекторию
        m.update_coefficient(50, 0)
        self.assertEqual(m.deviations, 1)
        # Снова уехали — новый deviation
        m.update_coefficient(50, 30)
        self.assertEqual(m.deviations, 2)


class TestWaypointVisits(unittest.TestCase):
    def test_waypoint_marked_when_within_tolerance(self):
        m = _mk_mission(waypoints=[(100.0, 0.0), (200.0, 0.0)])
        # tolerance = 10 см. Робот в (95, 5) — dist ≈ 7.07 от первой точки
        new = m.mark_waypoint_visits(95, 5)
        self.assertEqual(new, [0])
        self.assertIn(0, m.waypoints_visited)
        # Повторный заход в ту же точку — не считается заново
        new = m.mark_waypoint_visits(95, 5)
        self.assertEqual(new, [])

    def test_waypoint_not_marked_when_far(self):
        m = _mk_mission(waypoints=[(100.0, 0.0)])
        new = m.mark_waypoint_visits(50, 0)
        self.assertEqual(new, [])
        self.assertNotIn(0, m.waypoints_visited)

    def test_multiple_waypoints_visited_in_one_tick(self):
        # Если две точки в радиусе — обе помечаются
        m = _mk_mission(waypoints=[(100.0, 0.0), (105.0, 5.0)])
        new = m.mark_waypoint_visits(102, 2)
        self.assertEqual(set(new), {0, 1})


class TestActionMatching(unittest.TestCase):
    def test_matches_correct_action_type_within_tolerance(self):
        m = _mk_mission(actions_required=[
            {"type": "place_attention", "x": 50, "y": 50, "r": 15},
            {"type": "remove_danger",   "x": 100, "y": 0},
        ])
        idx = m.try_match_action("place_attention", 55, 48)
        self.assertEqual(idx, 0)
        self.assertIn(0, m.actions_done)

    def test_does_not_match_wrong_type(self):
        m = _mk_mission(actions_required=[
            {"type": "place_attention", "x": 50, "y": 50},
        ])
        idx = m.try_match_action("remove_danger", 50, 50)
        self.assertIsNone(idx)
        self.assertNotIn(0, m.actions_done)

    def test_does_not_match_far_position(self):
        m = _mk_mission(actions_required=[
            {"type": "place_attention", "x": 50, "y": 50},
        ])
        # 100 см от цели — за пределами ACTION_TOLERANCE_CM (15)
        idx = m.try_match_action("place_attention", 150, 50)
        self.assertIsNone(idx)

    def test_does_not_match_already_done(self):
        m = _mk_mission(actions_required=[
            {"type": "place_attention", "x": 50, "y": 50},
            {"type": "place_attention", "x": 60, "y": 60},
        ])
        idx = m.try_match_action("place_attention", 50, 50)
        self.assertEqual(idx, 0)
        # Повторно зону НЕ матчим под уже выполненный action
        idx = m.try_match_action("place_attention", 50, 50)
        self.assertEqual(idx, 1, "должен сматчиться второй (ещё не done) action")


class TestCompletionAndStars(unittest.TestCase):
    def test_complete_when_all_waypoints_and_actions(self):
        m = _mk_mission(waypoints=[(100, 0)],
                        actions_required=[{"type": "place_attention", "x": 50, "y": 0}])
        self.assertFalse(m.is_complete())
        m.mark_waypoint_visits(100, 0)
        self.assertFalse(m.is_complete())
        m.try_match_action("place_attention", 50, 0)
        self.assertTrue(m.is_complete())

    def test_stars_zero_when_nothing_done(self):
        m = _mk_mission(waypoints=[(100, 0)])
        self.assertEqual(m.compute_stars(), 0)

    def test_stars_floor_of_base_times_coefficient(self):
        m = _mk_mission(waypoints=[(100, 0), (200, 0), (300, 0)])
        for i in range(3):
            m.waypoints_visited.add(i)
        m.coefficient = 0.7
        # base = 3, stars = floor(3 * 0.7) = 2
        self.assertEqual(m.compute_stars(), 2)

    def test_complete_mission_gets_at_least_1_star(self):
        m = _mk_mission(waypoints=[(100, 0)])
        m.waypoints_visited.add(0)
        m.coefficient = 0.001    # почти 0, но миссия завершена
        self.assertEqual(m.compute_stars(), 1)


class TestClientSerialization(unittest.TestCase):
    def test_to_client_dict_has_required_keys(self):
        m = _mk_mission()
        d = m.to_client_dict()
        for key in ("mission_id", "title", "waypoints", "danger_zones",
                    "actions", "safety_margin_cm", "progress"):
            self.assertIn(key, d)

    def test_progress_dict_reflects_state(self):
        m = _mk_mission(waypoints=[(100, 0), (200, 0)])
        m.waypoints_visited.add(0)
        m.coefficient = 0.85
        m.deviations = 3
        p = m.progress_dict()
        self.assertEqual(p["waypoints_visited"], [0])
        self.assertEqual(p["coefficient"], 0.85)
        self.assertEqual(p["deviations"], 3)
        self.assertFalse(p["complete"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
