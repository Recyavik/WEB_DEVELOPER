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
    WAYPOINT_FALLBACK_TOLERANCE_CM, ACTION_TOLERANCE_CM, COEFF_STEP_PER_TICK,
)  # noqa: E402


def _mk_mission(**overrides) -> ActiveMission:
    """Конструктор тестового ActiveMission с разумными дефолтами.
    level=3 (с трекингом траектории) — иначе включится инспектор-режим
    L1/L2 (см. ActiveMission.is_inspector), и тесты update_coefficient
    провалятся (там не отслеживается отклонение)."""
    defaults = dict(
        mission_id=1, run_id=None, user_id=1,
        waypoints=[(100.0, 0.0), (100.0, 100.0)],
        danger_zones=[],
        actions_required=[],
        safety_margin_cm=5.0,
        start_x=0.0, start_y=0.0,
        level=3,
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
    """Точное прохождение (1 см) с проверкой по отрезку движения."""

    def test_waypoint_marked_when_robot_on_point(self):
        # Tolerance 1 см. Первый тик — fallback на точку.
        m = _mk_mission(waypoints=[(100.0, 0.0), (200.0, 0.0)])
        new = m.mark_waypoint_visits(100.5, 0.3)            # dist ≈ 0.58
        self.assertEqual(new, [0])
        self.assertIn(0, m.waypoints_visited)
        # Повторный заход в ту же точку — не считается заново
        new = m.mark_waypoint_visits(100.5, 0.3)
        self.assertEqual(new, [])

    def test_waypoint_not_marked_when_far(self):
        m = _mk_mission(waypoints=[(100.0, 0.0)])
        new = m.mark_waypoint_visits(95, 5)                 # dist ≈ 7.07 — далеко
        self.assertEqual(new, [])
        self.assertNotIn(0, m.waypoints_visited)

    def test_multiple_waypoints_visited_in_one_tick(self):
        # На первом тике сравниваем с точкой. Близкие точки 100 и 100.5 —
        # обе попадают в 1 см от (100, 0).
        m = _mk_mission(waypoints=[(100.0, 0.0), (100.5, 0.0)])
        new = m.mark_waypoint_visits(100.2, 0.0)
        self.assertEqual(set(new), {0, 1})

    def test_segment_check_catches_fast_pass_through(self):
        """Регрессия: на быстрой скорости робот мог 'проскочить' точку.
        Сейчас точка засчитывается по отрезку движения, не по точке."""
        m = _mk_mission(waypoints=[(50.0, 0.0)])
        # Первый кадр — робот далеко перед точкой.
        m.mark_waypoint_visits(0.0, 0.0)
        self.assertNotIn(0, m.waypoints_visited)
        # Следующий кадр — робот уже за точкой на 5 см. Точечная проверка
        # бы дала dist=5 > 1 → точка пропущена. Сегментная — точка лежит
        # на отрезке (0,0)→(55,0) → dist=0 → засчитывается.
        m.mark_waypoint_visits(55.0, 0.0)
        self.assertIn(0, m.waypoints_visited)

    def test_segment_check_ignores_point_far_from_track(self):
        """Если точка в стороне от отрезка — не засчитывается, даже если
        робот проехал близко по оси."""
        m = _mk_mission(waypoints=[(50.0, 10.0)])
        m.mark_waypoint_visits(0.0, 0.0)
        m.mark_waypoint_visits(100.0, 0.0)
        # Точка (50, 10) — в 10 см над отрезком (0,0)→(100,0).
        self.assertNotIn(0, m.waypoints_visited)

    def test_tolerance_follows_safety_margin(self):
        """Допуск waypoint = safety_margin_cm миссии. С margin=2 точка
        в 5 см не засчитывается, с margin=10 — засчитывается."""
        # margin=2 — допуск тесный
        m_tight = _mk_mission(waypoints=[(100.0, 0.0)], safety_margin_cm=2.0)
        m_tight.mark_waypoint_visits(105.0, 0.0)
        self.assertNotIn(0, m_tight.waypoints_visited,
                         "При margin=2 точка в 5 см не должна засчитываться")
        # margin=10 — допуск шире, та же позиция засчитывается
        m_wide = _mk_mission(waypoints=[(100.0, 0.0)], safety_margin_cm=10.0)
        m_wide.mark_waypoint_visits(105.0, 0.0)
        self.assertIn(0, m_wide.waypoints_visited,
                      "При margin=10 точка в 5 см должна засчитываться")

    def test_fallback_tolerance_when_safety_margin_zero(self):
        """Если у миссии margin=0 (вырожденный случай) — используем
        WAYPOINT_FALLBACK_TOLERANCE_CM = 1 см, чтобы трекинг работал."""
        m = _mk_mission(waypoints=[(100.0, 0.0)], safety_margin_cm=0.0)
        m.mark_waypoint_visits(100.5, 0.0)                 # в 0.5 см
        self.assertIn(0, m.waypoints_visited)


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
        self.assertEqual(m.fact_stars(), 0)
        self.assertEqual(m.track_bonus_stars(), 0)

    def test_fact_stars_one_per_visit_and_action(self):
        """1 звезда за каждую посещённую точку + 1 за каждое действие.
        Не зависит от коэффициента."""
        m = _mk_mission(waypoints=[(100, 0), (200, 0), (300, 0)],
                        actions_required=[{"type": "place_attention", "x": 0, "y": 0}])
        for i in range(3):
            m.waypoints_visited.add(i)
        m.actions_done.add(0)
        m.coefficient = 0.0          # точность 0
        self.assertEqual(m.fact_stars(), 4,
                         "Все 3 точки + 1 действие = 4 факт-звезды, "
                         "независимо от точности")

    def test_track_bonus_proportional_to_coefficient(self):
        """Бонус = floor(база × точность). При 100% — удваивает базу."""
        m = _mk_mission(waypoints=[(100, 0), (200, 0), (300, 0)])
        for i in range(3):
            m.waypoints_visited.add(i)
        # точность 100% → бонус = 3, всего 6
        m.coefficient = 1.0
        self.assertEqual(m.fact_stars(), 3)
        self.assertEqual(m.track_bonus_stars(), 3)
        self.assertEqual(m.compute_stars(), 6)
        # точность 70% → бонус = floor(3 * 0.7) = 2, всего 5
        m.coefficient = 0.7
        self.assertEqual(m.track_bonus_stars(), 2)
        self.assertEqual(m.compute_stars(), 5)
        # точность 22% → бонус 0, всего только факт-звёзды
        m.coefficient = 0.22
        self.assertEqual(m.track_bonus_stars(), 0)
        self.assertEqual(m.compute_stars(), 3)

    def test_partial_completion_still_gives_fact_stars(self):
        """Регрессия: раньше при coef=0.2 звёзды режились в 0 даже если
        робот посетил часть точек. Сейчас факт-звёзды гарантированы."""
        m = _mk_mission(waypoints=[(100, 0), (200, 0), (300, 0)])
        m.waypoints_visited.add(0)
        m.waypoints_visited.add(1)
        m.coefficient = 0.22
        # 2 факт + floor(2 * 0.22)=0 бонус = 2 звезды
        self.assertEqual(m.compute_stars(), 2,
                         "При частичном прохождении звёзды за точки "
                         "должны сохраняться, даже при низкой точности")

    def test_complete_mission_at_low_coefficient(self):
        """Полное прохождение с минимальной точностью даёт ровно factual
        количество звёзд (бонус ~0). 'Хотя бы 1' больше не нужно —
        фактом это покрыто."""
        m = _mk_mission(waypoints=[(100, 0)])
        m.waypoints_visited.add(0)
        m.coefficient = 0.001
        self.assertEqual(m.compute_stars(), 1)
        self.assertEqual(m.fact_stars(), 1)
        self.assertEqual(m.track_bonus_stars(), 0)

    def test_time_bonus_fast_run_two_stars(self):
        """Скорость ≤ половины target — +2 звезды.
        Target = 10 сек × N_waypoints. Для 3 точек target=30 сек, /2 = 15 сек."""
        m = _mk_mission(waypoints=[(100, 0), (200, 0), (300, 0)])
        for i in range(3): m.waypoints_visited.add(i)
        self.assertEqual(m.time_bonus_stars(10), 2)
        self.assertEqual(m.time_bonus_stars(15), 2)  # ровно на границе

    def test_time_bonus_normal_run_one_star(self):
        """Скорость ≤ target — +1 звезда."""
        m = _mk_mission(waypoints=[(100, 0), (200, 0), (300, 0)])
        for i in range(3): m.waypoints_visited.add(i)
        self.assertEqual(m.time_bonus_stars(20), 1)
        self.assertEqual(m.time_bonus_stars(30), 1)  # ровно на границе

    def test_time_bonus_slow_run_zero(self):
        """Превышение target — 0 звёзд."""
        m = _mk_mission(waypoints=[(100, 0)])
        m.waypoints_visited.add(0)
        # target = 10 сек × 1 = 10. 20 сек > 10 → 0
        self.assertEqual(m.time_bonus_stars(20), 0)

    def test_time_bonus_zero_when_no_fact_stars(self):
        """Если ничего не пройдено — бонус 0, даже если робот был быстрый."""
        m = _mk_mission(waypoints=[(100, 0)])
        self.assertEqual(m.time_bonus_stars(1), 0)

    def test_time_bonus_zero_when_duration_none_or_negative(self):
        m = _mk_mission(waypoints=[(100, 0)])
        m.waypoints_visited.add(0)
        self.assertEqual(m.time_bonus_stars(None), 0)
        self.assertEqual(m.time_bonus_stars(0), 0)
        self.assertEqual(m.time_bonus_stars(-5), 0)

    def test_compute_stars_includes_time_bonus(self):
        """compute_stars(duration) суммирует факт + точность + скорость."""
        m = _mk_mission(waypoints=[(100, 0), (200, 0), (300, 0)])
        for i in range(3): m.waypoints_visited.add(i)
        m.coefficient = 1.0
        # факт 3 + точность 3 + скорость 2 (быстро) = 8
        self.assertEqual(m.compute_stars(10), 8)
        # факт 3 + точность 3 + скорость 0 (медленно) = 6
        self.assertEqual(m.compute_stars(60), 6)
        # без duration — без бонуса
        self.assertEqual(m.compute_stars(), 6)


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
