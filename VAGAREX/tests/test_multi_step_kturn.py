"""Тесты для multi-step K-turn (разворот за N маленьких приёмов).

Покрывает:
  1. `_pick_kturn_step_count` — выбор N (cap=4) и расчёт отъезда.
  2. Дрейф позиции после N K-turn'ов: симулируем физику
     (forward-Euler как в `update_physics`) и проверяем, что робот
     возвращается в исходную точку с допустимой ошибкой.

Запуск:
    cd VAGAREX
    python -m unittest tests.test_multi_step_kturn -v
"""
import math
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from session import UserSession   # noqa: E402


# ── Радиус разворота в нашей калибровке (как в _multi_step_kturn).
# STEER_USED = cfg.turn_angle, по умолчанию 36° (см. models.UserSettings.turn_angle).
WHEEL_CIRC      = 28.30
HEADING_PER_ROT = 25.0
STEER_USED      = 36.0                                       # default cfg.turn_angle
R = (WHEEL_CIRC * 360.0) / (
    2.0 * math.pi * HEADING_PER_ROT * (STEER_USED / 45.0))    # ≈ 81 см

SAFETY = 12.5 + 12.0 / 2.0 + 5.0   # robot_width=12 → 23.5


class TestPickKturnStepCount(unittest.TestCase):
    """`_pick_kturn_step_count` (статический хелпер выбора N).

    Правило (от пользователя): шаг ≤ 45°, не более 4 приёмов.
        45° → 1 шаг, 90° → 2 шага, 135° → 3 шага, 180° → 4 шага.
    Если стандартное N не помещается в свободное место — N растёт до 4.
    Если и 4 не лезет — мини-отъезд назад."""

    def _pick(self, diff: float, forward: float, backward: float = 200.0):
        return UserSession._pick_kturn_step_count(
            diff, forward, backward, R, SAFETY, max_n=4, max_step_deg=45.0)

    def test_180_always_4_steps_in_open_space(self):
        # 180° → 4 шага по 45°, при любом достаточном пространстве.
        n, backup = self._pick(180.0, forward=200.0)
        self.assertEqual(n, 4)
        self.assertEqual(backup, 0.0)

    def test_90_uses_2_steps(self):
        # 90° → 2 шага по 45°.
        n, backup = self._pick(90.0, forward=200.0)
        self.assertEqual(n, 2)
        self.assertEqual(backup, 0.0)

    def test_45_uses_1_step(self):
        # 45° → 1 шаг.
        n, backup = self._pick(45.0, forward=200.0)
        self.assertEqual(n, 1)
        self.assertEqual(backup, 0.0)

    def test_30_uses_1_step(self):
        # 30° → 1 шаг (≤45°).
        n, backup = self._pick(30.0, forward=200.0)
        self.assertEqual(n, 1)
        self.assertEqual(backup, 0.0)

    def test_100_uses_3_steps(self):
        # 100° / 45 = 2.22 → ceil = 3.
        n, backup = self._pick(100.0, forward=200.0)
        self.assertEqual(n, 3)
        self.assertEqual(backup, 0.0)

    def test_135_uses_3_steps(self):
        # 135° / 45 = 3.
        n, backup = self._pick(135.0, forward=200.0)
        self.assertEqual(n, 3)
        self.assertEqual(backup, 0.0)

    def test_170_uses_4_steps(self):
        # 170° / 45 = 3.78 → ceil = 4.
        n, backup = self._pick(170.0, forward=200.0)
        self.assertEqual(n, 4)
        self.assertEqual(backup, 0.0)

    def test_n_capped_at_4_for_180(self):
        # 180° никогда не больше 4 шагов, даже в очень тесном пространстве.
        for forward in (200, 100, 60, 30, 10):
            n, _ = self._pick(180.0, forward=forward, backward=200.0)
            self.assertLessEqual(n, 4, f"N={n} превысил cap=4 для forward={forward}")

    def test_tight_space_180_triggers_backoff(self):
        # forward=10 см и при N=4 (шаг 45°) нужно ~40 → backup ≈ 30 см.
        n, backup = self._pick(180.0, forward=10.0, backward=200.0)
        self.assertEqual(n, 4)
        self.assertGreater(backup, 25.0)
        self.assertLess(backup, 35.0)

    def test_no_space_anywhere_falls_through(self):
        # Прижаты с двух сторон — backup<5 → (max_n, 0.0).
        n, backup = self._pick(180.0, forward=8.0, backward=3.0)
        self.assertEqual(n, 4)
        self.assertEqual(backup, 0.0)

    def test_negative_diff_treated_by_magnitude(self):
        n_pos, _ = self._pick(+180.0, forward=100.0)
        n_neg, _ = self._pick(-180.0, forward=100.0)
        self.assertEqual(n_pos, n_neg)

    def test_n_grows_for_small_angle_when_tight(self):
        # 90° обычно N=2 (per_step 45°). Если forward слишком мал,
        # N может вырасти до 3 или 4 (более мелкие шаги, меньше нужно
        # места). Проверяем, что N растёт, но не больше 4.
        n_open,  _ = self._pick(90.0, forward=200.0)
        n_tight, b = self._pick(90.0, forward=20.0, backward=200.0)
        self.assertEqual(n_open, 2)
        self.assertGreaterEqual(n_tight, n_open)
        self.assertLessEqual(n_tight, 4)


class TestKturnDriftSimulation(unittest.TestCase):
    """Симулирует физику симулятора и измеряет накопленный дрейф позиции
    после N K-turn'ов. Цель — убедиться, что точка начала и конца
    разворота близки (как заявлено: «каждый K-turn возвращается в точку»).

    Воспроизводит логику `update_physics`:
        heading += (dist/wheel_circ) * heading_per_rot * (steer/45) * trf * sign
        x += sin(new_heading) * dist
        y += cos(new_heading) * dist
    с шагом dt=0.05 как в _wait_movement.
    """

    DT          = 0.05
    SPEED_PCT   = 40
    SPEED_AT_100 = 80.0          # см/с
    TURN_REF    = 40             # turn_speed_ref %

    def _trf(self, spd_pct: int) -> float:
        ref = self.TURN_REF / 100.0
        spd = max(0.05, abs(spd_pct) / 100.0)
        return max(0.25, min(3.0, ref / spd))

    def _simulate_arc(self, x, y, heading, steer_deg, sweep_deg, backward):
        """Симулирует одну дугу как _arc_at_steer + update_physics.
        Возвращает (x, y, heading) после дуги."""
        steer_ratio = abs(steer_deg) / 45.0
        trf         = self._trf(self.SPEED_PCT)
        # Полная длина дуги для заданного sweep_deg курса
        arc = abs(sweep_deg) * WHEEL_CIRC / (HEADING_PER_ROT * steer_ratio * trf)

        drive_spd = -self.SPEED_PCT if backward else self.SPEED_PCT
        sign      = 1 if drive_spd >= 0 else -1
        cm_per_s  = self.SPEED_AT_100 * abs(drive_spd) / 100.0
        dist_left = arc
        h         = heading

        while dist_left > 1e-6:
            step_dist = min(cm_per_s * self.DT, dist_left)
            # Шаг как в update_physics: сначала heading, потом позиция.
            d_head = (step_dist / WHEEL_CIRC) * HEADING_PER_ROT * (
                steer_deg / 45.0) * trf * sign
            h = (h + d_head) % 360.0
            h_rad = math.radians(h)
            x += sign * math.sin(h_rad) * step_dist
            y += sign * math.cos(h_rad) * step_dist
            dist_left -= step_dist
        return x, y, h

    def _simulate_one_kturn(self, x, y, heading, target_deg):
        """Симулирует 3-дуговой K-turn от heading до target_deg.
        Финальный heading НЕ snap'ится — возвращаем как накопилось.
        (В реальном коде snap происходит, но позиция остаётся как
        даёт физика — её мы и проверяем.)"""
        diff = (target_deg - heading + 540.0) % 360.0 - 180.0
        if abs(diff) < 1e-6:
            return x, y, heading
        direction = 1 if diff > 0 else -1
        STEER = STEER_USED   # 36
        half = math.radians(abs(diff) / 2.0)
        beta = 2.0 * math.degrees(math.asin(math.sin(half) / 2.0))
        alpha = (abs(diff) - beta) / 2.0
        # Дуга 1: forward, +direction·STEER
        x, y, h = self._simulate_arc(x, y, heading, direction * STEER, alpha, backward=False)
        # Дуга 2: backward, -direction·STEER
        x, y, h = self._simulate_arc(x, y, h, -direction * STEER, beta, backward=True)
        # Дуга 3: forward, +direction·STEER
        x, y, h = self._simulate_arc(x, y, h, direction * STEER, alpha, backward=False)
        return x, y, h

    def test_single_kturn_180_returns_to_start(self):
        """Один K-turn на 180° — геометрия Reeds-Shepp возвращает в точку
        с точностью симулятора (forward-Euler при dt=0.05 даёт <1 см)."""
        x0, y0, h0 = 50.0, 50.0, 0.0
        x1, y1, _ = self._simulate_one_kturn(x0, y0, h0, 180.0)
        drift = math.hypot(x1 - x0, y1 - y0)
        self.assertLess(drift, 1.0,
            f"180° K-turn дрейфнул на {drift:.3f} см "
            f"(старт ({x0},{y0}), финиш ({x1:.3f},{y1:.3f}))")

    def test_multistep_4x45_returns_to_start(self):
        """4×45° multi-step (как теперь делает алгоритм для 180°) —
        каждый шаг возвращает в свою точку → суммарный дрейф < 1 см."""
        x0, y0, h0 = 50.0, 50.0, 0.0
        x, y, h = x0, y0, h0
        for _ in range(4):
            target = (h + 45.0) % 360.0
            x, y, h = self._simulate_one_kturn(x, y, h, target)
            h = target  # snap, как делает _k_turn_to_heading
        drift = math.hypot(x - x0, y - y0)
        self.assertLess(drift, 1.0,
            f"4×45° K-turn дрейфнул на {drift:.3f} см "
            f"(старт ({x0},{y0}), финиш ({x:.3f},{y:.3f}))")

    def test_multistep_3x60_returns_to_start(self):
        """3×60° multi-step (180°). Тоже должен возвращать в точку."""
        x0, y0, h0 = 50.0, 50.0, 0.0
        x, y, h = x0, y0, h0
        for _ in range(3):
            target = (h + 60.0) % 360.0
            x, y, h = self._simulate_one_kturn(x, y, h, target)
            h = target
        drift = math.hypot(x - x0, y - y0)
        self.assertLess(drift, 1.0,
            f"3×60° K-turn дрейфнул на {drift:.3f} см "
            f"(старт ({x0},{y0}), финиш ({x:.3f},{y:.3f}))")

    def test_multistep_2x90_returns_to_start(self):
        """2×90° multi-step (180°)."""
        x0, y0, h0 = 50.0, 50.0, 0.0
        x, y, h = x0, y0, h0
        for _ in range(2):
            target = (h + 90.0) % 360.0
            x, y, h = self._simulate_one_kturn(x, y, h, target)
            h = target
        drift = math.hypot(x - x0, y - y0)
        self.assertLess(drift, 1.0,
            f"2×90° K-turn дрейфнул на {drift:.3f} см "
            f"(старт ({x0},{y0}), финиш ({x:.3f},{y:.3f}))")

    def test_starting_heading_nonzero(self):
        """Из произвольного курса (например, 45°) тоже возвращаемся в точку."""
        x0, y0, h0 = 50.0, 50.0, 45.0
        target_deg = (h0 + 180.0) % 360.0
        x, y, h = x0, y0, h0
        for _ in range(4):
            target = (h + 45.0) % 360.0
            x, y, h = self._simulate_one_kturn(x, y, h, target)
            h = target
        drift = math.hypot(x - x0, y - y0)
        self.assertLess(drift, 1.0,
            f"Multi-step из курса {h0}° дрейфнул на {drift:.3f} см")


if __name__ == "__main__":
    unittest.main()
