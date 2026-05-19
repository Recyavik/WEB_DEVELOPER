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
    WAYPOINT_TOLERANCE_CM, ACTION_TOLERANCE_CM, QUALITY_DRAIN_PER_TICK,
)  # noqa: E402


def _mk_mission(**overrides) -> ActiveMission:
    """Конструктор тестового ActiveMission с разумными дефолтами.
    track_tolerance_cm по умолчанию 20 см (габарит робота).
    path по умолчанию задан (старт→waypoints) — проверка отклонения
    активна; для миссий «без траектории» (уровень 1) передать path=[]."""
    defaults = dict(
        mission_id=1, run_id=None, user_id=1,
        waypoints=[(100.0, 0.0), (100.0, 100.0)],
        path=[(0.0, 0.0), (100.0, 0.0), (100.0, 100.0)],
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


class TestQualityUpdate(unittest.TestCase):
    """Аккуратность: стартует с 0, растёт за посещённые точки/действия,
    убывает вне коридора траектории. В коридоре по позиции не меняется."""

    def test_quality_zero_when_nothing_done(self):
        # Не делал ничего — аккуратность 0%, а не «100% точности».
        m = _mk_mission()
        self.assertEqual(m.quality, 0.0)

    def test_quality_grows_with_visits(self):
        # 2 точки, 0 действий → шаг 50%, каждое посещение +50%.
        m = _mk_mission(waypoints=[(100.0, 0.0), (200.0, 0.0)])
        self.assertEqual(m.quality_step, 0.5)
        m.mark_waypoint_visits(100.0, 0.0)
        self.assertAlmostEqual(m.quality, 0.5, places=4)
        m.mark_waypoint_visits(200.0, 0.0)
        self.assertAlmostEqual(m.quality, 1.0, places=4)

    def test_quality_grows_with_actions(self):
        # 1 точка + 1 действие → шаг 50%.
        m = _mk_mission(waypoints=[(100.0, 0.0)],
                        actions_required=[{"type": "place_attention",
                                           "x": 0.0, "y": 0.0}])
        self.assertEqual(m.quality_step, 0.5)
        m.try_match_action("place_attention", 0.0, 0.0)
        self.assertAlmostEqual(m.quality, 0.5, places=4)

    def test_quality_clamped_at_one(self):
        # Аккуратность не превышает 100%.
        m = _mk_mission(waypoints=[(100.0, 0.0)])
        m.mark_waypoint_visits(100.0, 0.0)
        self.assertAlmostEqual(m.quality, 1.0, places=4)

    def test_in_corridor_keeps_quality(self):
        m = _mk_mission()
        m.quality = 0.5
        # Робот ровно на траектории — ни аккуратность, ни track_penalty не меняются.
        m.update_quality(50, 0)
        self.assertEqual(m.quality, 0.5)
        for _ in range(1000):
            m.update_quality(50, 0)
        self.assertEqual(m.quality, 0.5)
        self.assertEqual(m.track_penalty, 0.0)

    def test_out_of_corridor_drains_effective_quality_to_zero(self):
        m = _mk_mission()
        m.quality = 1.0
        # 30 см от траектории — вне коридора (20 см): track_penalty копится,
        # сам quality не трогается, effective_quality уходит в 0.
        for _ in range(1000):
            m.update_quality(50, 30)
        self.assertEqual(m.quality, 1.0)
        self.assertEqual(m.effective_quality(), 0.0)

    def test_out_of_corridor_one_step_per_tick(self):
        # Один тик вне коридора добавляет ровно шаг к track_penalty.
        m = _mk_mission()
        m.quality = 1.0
        m.update_quality(50, 30)
        self.assertAlmostEqual(m.track_penalty, QUALITY_DRAIN_PER_TICK,
                               places=6)
        self.assertAlmostEqual(m.effective_quality(),
                               1.0 - QUALITY_DRAIN_PER_TICK, places=6)

    def test_track_penalty_counts_even_when_quality_is_zero(self):
        # Регрессия floor-absorption: отклонение в начале прогона
        # (quality ещё 0) раньше «съедалось» нижней границей 0.
        m = _mk_mission()
        m.quality = 0.0
        for _ in range(10):
            m.update_quality(50, 30)            # вне коридора
        self.assertAlmostEqual(m.track_penalty, 10 * QUALITY_DRAIN_PER_TICK,
                               places=6)
        # Позже игрок зарабатывает аккуратность — штраф остаётся вычтенным.
        m.quality = 0.50
        self.assertAlmostEqual(m.effective_quality(),
                               0.50 - 10 * QUALITY_DRAIN_PER_TICK, places=6)

    def test_no_path_means_no_deviation_check(self):
        # Миссия без эталонной траектории (path=[]) — уровень 1, «посети
        # точки любым путём»: отклонение не штрафует ни аккуратность, ни
        # track_penalty.
        m = _mk_mission(path=[])
        m.quality = 1.0
        for _ in range(1000):
            m.update_quality(50, 30)
        self.assertEqual(m.quality, 1.0)
        self.assertEqual(m.track_penalty, 0.0)
        self.assertEqual(m.deviations, 0)

    def test_visits_and_zone_hits_do_not_touch_track_penalty(self):
        # Посещение точки и наезд на зону влияют на quality/zone_penalty,
        # но track_penalty (точность траектории) не трогают.
        m = _mk_mission(waypoints=[(100.0, 0.0)],
                        danger_zones=[(0.0, 0.0, 15.0)])
        m.quality = 1.0
        m.mark_waypoint_visits(100.0, 0.0)        # +аккуратность
        m.update_quality(5, 5)                    # въезд в зону, в коридоре
        self.assertAlmostEqual(m.zone_penalty, 0.20, places=4)
        self.assertEqual(m.track_penalty, 0.0)    # траектория ни при чём

    def test_deviation_counted_once_per_excursion(self):
        m = _mk_mission()
        # Сначала в коридоре
        m.update_quality(50, 0)
        self.assertEqual(m.deviations, 0)
        # Уехали — один deviation
        m.update_quality(50, 30)
        self.assertEqual(m.deviations, 1)
        # Ещё раз вне коридора — deviation НЕ инкрементируется (тот же эпизод)
        m.update_quality(50, 30)
        self.assertEqual(m.deviations, 1)
        # Вернулись на траекторию
        m.update_quality(50, 0)
        self.assertEqual(m.deviations, 1)
        # Снова уехали — новый deviation
        m.update_quality(50, 30)
        self.assertEqual(m.deviations, 2)

    def test_hint_penalizes_quality(self):
        # Каждая подсказка — −20% к аккуратности; счётчик подсказок переживает
        # перезапуск прогона (reset_for_new_run сбрасывает quality, но не его).
        m = _mk_mission(waypoints=[(100.0, 0.0)])   # 1 точка → шаг 100%
        m.mark_waypoint_visits(100.0, 0.0)
        self.assertAlmostEqual(m.effective_quality(), 1.0, places=4)
        m.register_hint()
        self.assertAlmostEqual(m.effective_quality(), 0.80, places=4)
        m.register_hint()
        self.assertAlmostEqual(m.effective_quality(), 0.60, places=4)
        # Перезапуск: quality обнуляется, hints_used — нет.
        m.reset_for_new_run()
        self.assertEqual(m.hints_used, 2)
        m.mark_waypoint_visits(100.0, 0.0)
        self.assertAlmostEqual(m.effective_quality(), 0.60, places=4)


class TestDangerZoneHits(unittest.TestCase):
    """Наезд на опасную зону: −20% аккуратности начисляется СРАЗУ при ВЪЕЗДЕ,
    один раз за зону. Действие (place_attention / remove_danger),
    выполненное внутри зоны, возвращает штраф (forgive).
    Тесты стартуют с quality=1.0, чтобы изолировать влияние зон."""

    def _with_zones(self, danger_zones, actions=None):
        m = _mk_mission(danger_zones=danger_zones,
                        actions_required=actions or [])
        m.quality = 1.0
        return m

    def test_hit_fires_on_entry(self):
        # Заехал в зону → −20% СРАЗУ, не дожидаясь выезда.
        m = self._with_zones([(0.0, 0.0, 15.0)])
        m.update_quality(5, 5)       # въезд — штраф здесь же
        self.assertEqual(m.quality, 1.0)
        self.assertAlmostEqual(m.zone_penalty, 0.20, places=4)
        self.assertAlmostEqual(m.effective_quality(), 0.80, places=4)
        self.assertIn(0, m.danger_zones_finalized)

    def test_hit_counted_once_per_zone(self):
        # Повторные въезды/выезды в ту же зону больше не штрафуют.
        m = self._with_zones([(0.0, 0.0, 15.0)])
        m.update_quality(5, 5)       # въезд — штраф
        m.update_quality(50, 0)      # выезд
        m.update_quality(5, 5)       # повторный въезд — бесплатно
        self.assertAlmostEqual(m.zone_penalty, 0.20, places=4)

    def test_hit_penalizes_even_when_quality_is_zero(self):
        # Регрессия: наезд на зону в начале прогона (quality ещё 0) раньше
        # «съедался» нижней границей 0. Теперь штраф копится отдельно.
        m = self._with_zones([(0.0, 0.0, 15.0)])
        m.quality = 0.0
        m.update_quality(5, 5)       # въезд
        self.assertAlmostEqual(m.zone_penalty, 0.20, places=4)
        # Позже игрок зарабатывает аккуратность — штраф остаётся вычтенным.
        m.quality = 0.60
        self.assertAlmostEqual(m.effective_quality(), 0.40, places=4)

    def test_hit_refunded_when_action_performed_inside(self):
        # Заехал (−20%) → выполнил действие внутри → штраф возвращён.
        m = self._with_zones([(0.0, 0.0, 15.0)])
        m.update_quality(5, 5)            # въезд — штраф
        self.assertAlmostEqual(m.zone_penalty, 0.20, places=4)
        m.forgive_current_zone_hits(5, 5) # действие внутри → refund
        self.assertAlmostEqual(m.zone_penalty, 0.0, places=4)
        self.assertAlmostEqual(m.effective_quality(), 1.0, places=4)

    def test_forgive_before_entry_prevents_penalty(self):
        # Действие сработало раньше тика _mark_danger_zone_hits: зона
        # помечена forgiven → последующий въезд не штрафуется.
        m = self._with_zones([(0.0, 0.0, 15.0)])
        m.forgive_current_zone_hits(5, 5)  # робот уже у зоны, действие
        m.update_quality(5, 5)             # въезд — без штрафа
        self.assertAlmostEqual(m.zone_penalty, 0.0, places=4)

    def test_forgive_outside_zone_no_effect(self):
        # forgive вызван снаружи зоны → ничего не прощается.
        m = self._with_zones([(0.0, 0.0, 15.0)])
        m.forgive_current_zone_hits(100, 100)   # робот далеко от зоны
        m.update_quality(5, 5)                   # въезд — штраф
        self.assertAlmostEqual(m.effective_quality(), 0.80, places=4)

    def test_removable_zone_never_penalized(self):
        # Зона с парным remove_danger — задача «удалить»: заехать в неё
        # нужно по заданию, наезд НЕ штрафуется.
        m = self._with_zones(
            [(0.0, 0.0, 15.0)],
            actions=[{"type": "remove_danger", "x": 0.0, "y": 0.0}])
        self.assertIn(0, m.removable_zone_idx)
        m.update_quality(5, 5)                   # заехали внутрь
        m.update_quality(50, 0)                  # выехали
        self.assertEqual(m.zone_penalty, 0.0)

    def test_untouched_zone_still_penalized_with_removable_present(self):
        # Среди зон есть и «удалить» (0,0), и зона-препятствие (50,0).
        # Штрафуется только вторая — сразу при въезде.
        m = self._with_zones(
            [(0.0, 0.0, 15.0), (50.0, 0.0, 15.0)],
            actions=[{"type": "remove_danger", "x": 0.0, "y": 0.0}])
        self.assertEqual(m.removable_zone_idx, {0})
        m.update_quality(50, 5)                  # въезд во вторую → −20%
        self.assertAlmostEqual(m.effective_quality(), 0.80, places=4)


class TestActionCounts(unittest.TestCase):
    """Раздельный счёт действий: установлено зон внимания / удалено опасных."""

    def test_counts_split_by_type(self):
        m = _mk_mission(actions_required=[
            {"type": "place_attention", "x": 10, "y": 0},
            {"type": "place_attention", "x": 20, "y": 0},
            {"type": "remove_danger",   "x": 30, "y": 0},
        ])
        c = m.action_counts()
        self.assertEqual((c["place_done"], c["place_total"]), (0, 2))
        self.assertEqual((c["remove_done"], c["remove_total"]), (0, 1))
        m.try_match_action("place_attention", 10, 0)
        m.try_match_action("remove_danger", 30, 0)
        c = m.action_counts()
        self.assertEqual((c["place_done"], c["place_total"]), (1, 2))
        self.assertEqual((c["remove_done"], c["remove_total"]), (1, 1))


class TestWaypointVisits(unittest.TestCase):
    """Точное прохождение (допуск 5 см) с проверкой по отрезку движения."""

    def test_waypoint_marked_when_robot_on_point(self):
        # Допуск 5 см. Первый тик — fallback на точку.
        m = _mk_mission(waypoints=[(100.0, 0.0), (200.0, 0.0)])
        new = m.mark_waypoint_visits(100.5, 0.3)            # dist ≈ 0.58
        self.assertEqual(new, [0])
        self.assertIn(0, m.waypoints_visited)
        # Повторный заход в ту же точку — не считается заново
        new = m.mark_waypoint_visits(100.5, 0.3)
        self.assertEqual(new, [])

    def test_waypoint_not_marked_when_far(self):
        m = _mk_mission(waypoints=[(100.0, 0.0)])
        new = m.mark_waypoint_visits(70, 5)                 # dist ≈ 30 — за допуском 5
        self.assertEqual(new, [])
        self.assertNotIn(0, m.waypoints_visited)

    def test_multiple_waypoints_visited_in_one_tick(self):
        # На первом тике сравниваем с точкой. Близкие точки 100 и 100.5 —
        # обе попадают в 5 см от (100.2, 0).
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
        # Следующий кадр — робот уже за точкой на 10 см. Точечная проверка
        # бы дала dist=10 > 5 → точка пропущена. Сегментная — точка лежит
        # на отрезке (0,0)→(60,0) → dist=0 → засчитывается.
        m.mark_waypoint_visits(60.0, 0.0)
        self.assertIn(0, m.waypoints_visited)

    def test_segment_check_ignores_point_far_from_track(self):
        """Если точка в стороне от отрезка — не засчитывается, даже если
        робот проехал близко по оси."""
        m = _mk_mission(waypoints=[(50.0, 30.0)])
        m.mark_waypoint_visits(0.0, 0.0)
        m.mark_waypoint_visits(100.0, 0.0)
        # Точка (50, 30) — в 30 см над отрезком (0,0)→(100,0), за допуском 5.
        self.assertNotIn(0, m.waypoints_visited)

    def test_waypoint_tolerance_is_fixed_5cm(self):
        """Допуск точки — жёсткий WAYPOINT_TOLERANCE_CM (5 см): точка в 4 см
        засчитывается, точка в 8 см — нет. Не зависит от track_tolerance_cm."""
        self.assertEqual(WAYPOINT_TOLERANCE_CM, 5.0)
        # В пределах 5 см — засчитывается.
        m_ok = _mk_mission(waypoints=[(100.0, 0.0)])
        m_ok.mark_waypoint_visits(104.0, 0.0)              # dist 4
        self.assertIn(0, m_ok.waypoints_visited)
        # За пределами 5 см — НЕ засчитывается, даже при широком track-допуске.
        m_far = _mk_mission(waypoints=[(100.0, 0.0)], track_tolerance_cm=20.0)
        m_far.mark_waypoint_visits(108.0, 0.0)             # dist 8
        self.assertNotIn(0, m_far.waypoints_visited,
                         "Точку в 8 см засчитывать нельзя — допуск точки 5 см, "
                         "track_tolerance_cm на зачёт точек не влияет")


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

    def test_correct_radius_no_penalty(self):
        # Радиус в пределах допуска ±5 см — штрафа нет.
        m = _mk_mission(actions_required=[
            {"type": "place_attention", "x": 50, "y": 50, "radius": 10.0},
        ])
        idx = m.try_match_action("place_attention", 50, 50, radius=14.0)
        self.assertEqual(idx, 0)
        self.assertEqual(m.zone_penalty, 0.0)
        self.assertAlmostEqual(m.placed_attention[0]["r"], 14.0, places=4)

    def test_wrong_radius_penalizes_but_counts(self):
        # Радиус вне допуска — действие засчитывается, но −5% аккуратности.
        m = _mk_mission(actions_required=[
            {"type": "place_attention", "x": 50, "y": 50, "radius": 10.0},
        ])
        idx = m.try_match_action("place_attention", 50, 50, radius=20.0)
        self.assertEqual(idx, 0)
        self.assertIn(0, m.actions_done)            # действие засчитано
        self.assertAlmostEqual(m.zone_penalty, 0.05, places=4)

    def test_radius_at_tolerance_edge_no_penalty(self):
        # Ровно на границе допуска (|разница| == 5) — без штрафа.
        m = _mk_mission(actions_required=[
            {"type": "place_attention", "x": 50, "y": 50, "radius": 10.0},
        ])
        m.try_match_action("place_attention", 50, 50, radius=15.0)
        self.assertEqual(m.zone_penalty, 0.0)


class TestPlacedAttentionPenalty(unittest.TestCase):
    """Зона внимания, поставленная игроком: установка и первый выезд —
    без штрафа; повторный заезд+выезд → −20% (как опасная зона)."""

    def _mission(self):
        # path=[] — пропускаем проверку коридора, изолируем штраф зоны.
        return _mk_mission(path=[],
                           actions_required=[{"type": "place_attention",
                                              "x": 50, "y": 50,
                                              "radius": 15.0}])

    def test_place_records_zone_without_penalty(self):
        m = self._mission()
        m.try_match_action("place_attention", 50, 50)
        self.assertEqual(len(m.placed_attention), 1)
        z = m.placed_attention[0]
        self.assertEqual((z["armed"], z["inside"], z["finalized"]),
                         (False, True, False))

    def test_first_exit_arms_without_penalty(self):
        m = self._mission()
        m.try_match_action("place_attention", 50, 50)
        m.quality = 0.5
        m.update_quality(200, 200)            # выезд — взвели зону
        self.assertTrue(m.placed_attention[0]["armed"])
        self.assertEqual(m.zone_penalty, 0.0)  # штрафа нет

    def test_reentry_penalizes_once(self):
        m = self._mission()
        m.try_match_action("place_attention", 50, 50)
        m.quality = 0.5
        m.update_quality(200, 200)            # выезд — взвели
        m.update_quality(50, 50)              # повторный ВЪЕЗД → −20% сразу
        self.assertAlmostEqual(m.zone_penalty, 0.20, places=6)
        self.assertAlmostEqual(m.effective_quality(), 0.30, places=6)
        self.assertTrue(m.placed_attention[0]["finalized"])
        # Дальнейшие заезды/выезды больше не штрафуют.
        m.update_quality(200, 200)
        m.update_quality(50, 50)
        self.assertAlmostEqual(m.zone_penalty, 0.20, places=6)

    def test_reset_clears_placed_attention(self):
        m = self._mission()
        m.try_match_action("place_attention", 50, 50)
        m.reset_for_new_run()
        self.assertEqual(m.placed_attention, [])


class TestFinishAndEarlyRemove(unittest.TestCase):
    """Финиш (собраны все точки) и штраф за раннее снятие опасных зон (L5)."""

    def test_finish_reached_when_all_waypoints_visited(self):
        m = _mk_mission(waypoints=[(100.0, 0.0), (100.0, 100.0)])
        self.assertFalse(m.finish_reached)
        m.mark_waypoint_visits(100.0, 0.0)
        self.assertFalse(m.finish_reached)
        m.mark_waypoint_visits(100.0, 100.0)
        self.assertTrue(m.finish_reached)

    def test_track_penalty_frozen_after_finish(self):
        # После финиша точность траектории не проверяется.
        m = _mk_mission()
        m.finish_reached = True
        for _ in range(100):
            m.update_quality(50, 30)            # вне коридора
        self.assertEqual(m.track_penalty, 0.0)

    def test_remove_before_finish_penalized(self):
        # Миссия с траекторией (path≥2): снятие зоны до финиша → −20%.
        m = _mk_mission(actions_required=[
            {"type": "remove_danger", "x": 50, "y": 50}])
        self.assertFalse(m.finish_reached)
        idx = m.try_match_action("remove_danger", 50, 50)
        self.assertEqual(idx, 0)
        self.assertAlmostEqual(m.zone_penalty, 0.20, places=4)

    def test_remove_after_finish_no_penalty(self):
        m = _mk_mission(actions_required=[
            {"type": "remove_danger", "x": 50, "y": 50}])
        m.finish_reached = True
        idx = m.try_match_action("remove_danger", 50, 50)
        self.assertEqual(idx, 0)
        self.assertEqual(m.zone_penalty, 0.0)

    def test_remove_no_trajectory_no_penalty(self):
        # Миссия без эталонной траектории (L2, path=[]) — гейт «после
        # финиша» не применяется, раннее снятие не штрафуется.
        m = _mk_mission(path=[], actions_required=[
            {"type": "remove_danger", "x": 50, "y": 50}])
        idx = m.try_match_action("remove_danger", 50, 50)
        self.assertEqual(idx, 0)
        self.assertEqual(m.zone_penalty, 0.0)


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
        Не зависит от аккуратности."""
        m = _mk_mission(waypoints=[(100, 0), (200, 0), (300, 0)],
                        actions_required=[{"type": "place_attention", "x": 0, "y": 0}])
        for i in range(3):
            m.waypoints_visited.add(i)
        m.actions_done.add(0)
        m.quality = 0.0              # аккуратность 0
        self.assertEqual(m.fact_stars(), 4,
                         "Все 3 точки + 1 действие = 4 факт-звезды, "
                         "независимо от аккуратности")

    def test_track_bonus_proportional_to_quality(self):
        """Бонус = floor(база × аккуратность). При 100% — удваивает базу."""
        m = _mk_mission(waypoints=[(100, 0), (200, 0), (300, 0)])
        for i in range(3):
            m.waypoints_visited.add(i)
        # аккуратность 100% → бонус = 3, всего 6
        m.quality = 1.0
        self.assertEqual(m.fact_stars(), 3)
        self.assertEqual(m.track_bonus_stars(), 3)
        self.assertEqual(m.compute_stars(), 6)
        # аккуратность 70% → бонус = floor(3 * 0.7) = 2, всего 5
        m.quality = 0.7
        self.assertEqual(m.track_bonus_stars(), 2)
        self.assertEqual(m.compute_stars(), 5)
        # аккуратность 22% → бонус 0, всего только факт-звёзды
        m.quality = 0.22
        self.assertEqual(m.track_bonus_stars(), 0)
        self.assertEqual(m.compute_stars(), 3)

    def test_partial_completion_still_gives_fact_stars(self):
        """Регрессия: звёзды за посещённые точки гарантированы даже при
        низком аккуратности — факт-звёзды не режутся."""
        m = _mk_mission(waypoints=[(100, 0), (200, 0), (300, 0)])
        m.waypoints_visited.add(0)
        m.waypoints_visited.add(1)
        m.quality = 0.22
        # 2 факт + floor(2 * 0.22)=0 бонус = 2 звезды
        self.assertEqual(m.compute_stars(), 2,
                         "При частичном прохождении звёзды за точки "
                         "должны сохраняться, даже при низком аккуратности")

    def test_complete_mission_at_low_quality(self):
        """Полное прохождение с минимальным аккуратностью даёт ровно factual
        количество звёзд (бонус ~0). 'Хотя бы 1' больше не нужно —
        фактом это покрыто."""
        m = _mk_mission(waypoints=[(100, 0)])
        m.waypoints_visited.add(0)
        m.quality = 0.001
        self.assertEqual(m.compute_stars(), 1)
        self.assertEqual(m.fact_stars(), 1)
        self.assertEqual(m.track_bonus_stars(), 0)

    def test_time_bonus_fast_run_two_stars(self):
        """Скорость ≤ половины target — +2 звезды.
        Target = 90 сек × N_waypoints. Для 3 точек target=270 сек, /2 = 135 сек."""
        m = _mk_mission(waypoints=[(100, 0), (200, 0), (300, 0)])
        for i in range(3): m.waypoints_visited.add(i)
        self.assertEqual(m.time_bonus_stars(60), 2)
        self.assertEqual(m.time_bonus_stars(135), 2)  # ровно на границе

    def test_time_bonus_normal_run_one_star(self):
        """Скорость ≤ target — +1 звезда. Для 3 точек target=270 сек."""
        m = _mk_mission(waypoints=[(100, 0), (200, 0), (300, 0)])
        for i in range(3): m.waypoints_visited.add(i)
        self.assertEqual(m.time_bonus_stars(200), 1)
        self.assertEqual(m.time_bonus_stars(270), 1)  # ровно на границе

    def test_time_bonus_slow_run_zero(self):
        """Превышение target — 0 звёзд."""
        m = _mk_mission(waypoints=[(100, 0)])
        m.waypoints_visited.add(0)
        # target = 90 сек × 1 = 90. 120 сек > 90 → 0
        self.assertEqual(m.time_bonus_stars(120), 0)

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
        """compute_stars(duration) суммирует факт + аккуратность + скорость.
        Для 3 точек target=270 с, половина=135 с."""
        m = _mk_mission(waypoints=[(100, 0), (200, 0), (300, 0)])
        for i in range(3): m.waypoints_visited.add(i)
        m.quality = 1.0
        # факт 3 + аккуратность 3 + скорость 2 (быстро, ≤135 с) = 8
        self.assertEqual(m.compute_stars(10), 8)
        # факт 3 + аккуратность 3 + скорость 0 (медленно, >270 с) = 6
        self.assertEqual(m.compute_stars(300), 6)
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
        m.quality = 0.85
        p = m.progress_dict()
        self.assertEqual(p["waypoints_visited"], [0])
        self.assertEqual(p["quality"], 0.85)
        self.assertFalse(p["complete"])

    def test_progress_dict_quality_reflects_zone_penalty(self):
        # progress_dict.quality = effective_quality() — с вычетом штрафов.
        m = _mk_mission()
        m.quality = 0.80
        m.zone_penalty = 0.10
        self.assertAlmostEqual(m.progress_dict()["quality"], 0.70, places=4)


if __name__ == "__main__":
    unittest.main(verbosity=2)
