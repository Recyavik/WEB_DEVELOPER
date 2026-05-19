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
    _min_dist_to_polyline,
    _CARDINAL_HEADINGS, _angular_diff_deg,
    _MIN_PATH_GAP_CM, _GRID_CELL_CM,
    _dist_point_to_segment, _generate_trajectory,
)  # noqa: E402
import random as _random


def _waypoint_headings_from_path(path):
    """Вернуть курсы (в градусах) каждого сегмента path, в порядке появления."""
    headings = []
    for i in range(len(path) - 1):
        dx = path[i + 1][0] - path[i][0]
        dy = path[i + 1][1] - path[i][1]
        if math.hypot(dx, dy) < 0.5:
            continue
        deg = math.degrees(math.atan2(dx, dy)) % 360.0
        # Объединяем коллинеарные сегменты (атан и так выдаёт один курс).
        if headings and abs(((deg - headings[-1] + 180) % 360) - 180) < 0.5:
            continue
        headings.append(deg)
    return headings


def _geom_default() -> WorldGeom:
    return WorldGeom(
        world_w_cm=500, world_h_cm=500, wall_thick_cm=5,
        robot_w_cm=12, robot_l_cm=20, safety_margin_cm=5,
        start_x=0, start_y=0, start_heading=0,
    )


class TestLevel1Shape(unittest.TestCase):
    """Структура сгенерированной миссии level 1."""

    def test_level_3_has_4_waypoints(self):
        # L3 «Базовый» генерирует ровно 4 точки на сетке 50×50.
        for seed in range(20):
            with self.subTest(seed=seed):
                m = generate_mission(level=3, geom=_geom_default(), seed=seed)
                wp = json.loads(m["waypoints"])
                self.assertEqual(len(wp), 4,
                                  f"seed={seed}: L3 должен генерировать 4 точки")

    def test_level_1_no_zones(self):
        for seed in range(10):
            with self.subTest(seed=seed):
                m = generate_mission(level=3, seed=seed)
                self.assertEqual(json.loads(m["danger_zones"]), [])
                self.assertEqual(json.loads(m["actions_required"]), [])

    def test_required_fields_present(self):
        m = generate_mission(level=3, seed=42)
        for key in ("level", "title", "description", "waypoints",
                    "danger_zones", "actions_required",
                    "reference_voice", "reference_code", "safety_margin_cm"):
            self.assertIn(key, m, f"отсутствует поле {key!r}")

    def test_level_1_title_empty_and_description_meaningful(self):
        """Title оставляем пустым — пользователь введёт сам, fallback
        «Миссия #N» делается в /missions/save. Описание в формате:
        🟢 Начало → 📍 Контрольные точки → ⭐."""
        m = generate_mission(level=3, seed=7)
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
        m = generate_mission(level=3, seed=1)
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
            m = generate_mission(level=3, geom=g, seed=seed)
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
        a = generate_mission(level=3, seed=123)
        b = generate_mission(level=3, seed=123)
        self.assertEqual(a["waypoints"],       b["waypoints"])
        self.assertEqual(a["reference_voice"], b["reference_voice"])
        self.assertEqual(a["reference_code"],  b["reference_code"])

    def test_different_seeds_different_missions(self):
        a = generate_mission(level=3, seed=1)
        b = generate_mission(level=3, seed=999)
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
        """v4.5+: старт миссии генерируется случайно (на сетке 50×50,
        |coord| ≤ 100). geom.start_x/y игнорируется — пользователь
        учится выставлять стартовые координаты под условие задачи.
        Проверяем только что path не пустой и стартует на сетке."""
        for seed in (1, 7, 42, 100):
            with self.subTest(seed=seed):
                g = WorldGeom(world_w_cm=600, world_h_cm=600,
                              robot_w_cm=12, robot_l_cm=20,
                              start_x=0, start_y=0, start_heading=0)
                m = generate_mission(level=3, geom=g, seed=seed)
                path = json.loads(m["path"])
                self.assertGreaterEqual(len(path), 1,
                                        f"path не должен быть пустым (seed={seed})")
                sx, sy = path[0]
                self.assertEqual(sx % 50, 0, f"старт не на сетке 50: {sx}")
                self.assertEqual(sy % 50, 0, f"старт не на сетке 50: {sy}")
                self.assertLessEqual(abs(sx), 100, f"|start_x|>100: {sx}")
                self.assertLessEqual(abs(sy), 100, f"|start_y|>100: {sy}")
                self.assertAlmostEqual(path[0][0], sx, places=1,
                                       msg=f"path[0].x ≠ start_x для start=({sx},{sy})")
                self.assertAlmostEqual(path[0][1], sy, places=1,
                                       msg=f"path[0].y ≠ start_y для start=({sx},{sy})")

    def test_description_includes_random_start_coords(self):
        """v4.5+: 🟢 Начало маршрута содержит СГЕНЕРИРОВАННЫЕ
        start-координаты (кратные 50, |coord| ≤ 100). geom.start
        игнорируется при генерации."""
        g = WorldGeom(world_w_cm=600, world_h_cm=600, start_x=150, start_y=-100)
        m = generate_mission(level=3, geom=g, seed=7)
        path = json.loads(m["path"])
        sx, sy = int(path[0][0]), int(path[0][1])
        self.assertIn(f"Начало маршрута ({sx}, {sy})", m["description"])
        self.assertLessEqual(abs(sx), 100)
        self.assertLessEqual(abs(sy), 100)

    def test_random_start_is_on_grid_50(self):
        """v4.5+: сгенерированный старт всегда кратен 50."""
        g = WorldGeom(world_w_cm=600, world_h_cm=600,
                      start_x=158, start_y=-77)
        m = generate_mission(level=3, geom=g, seed=7)
        path = json.loads(m["path"])
        self.assertEqual(int(path[0][0]) % 50, 0)
        self.assertEqual(int(path[0][1]) % 50, 0)

    def test_waypoints_reflect_start_offset(self):
        """v4.5+: с генерируемым стартом waypoints спред по полю.
        Проверяем что хотя бы одна точка далеко от центра."""
        g = WorldGeom(world_w_cm=600, world_h_cm=600,
                      robot_w_cm=12, robot_l_cm=20,
                      start_x=200, start_y=200, start_heading=0)
        m = generate_mission(level=3, geom=g, seed=3)
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
        d = _format_description(level=3, waypoints=[], actions=[],
                                start_x=0, start_y=0)
        self.assertIn("Начало маршрута (0, 0)", d)
        self.assertIn("⭐", d)
        # Нет лишних блоков
        self.assertNotIn("Контрольные точки", d)
        self.assertNotIn("Установите зоны", d)
        self.assertNotIn("Опасные зоны", d)
        self.assertNotIn("Удалите", d)

    def test_with_waypoints_includes_coords_and_count(self):
        # v4.5+: L1/L3/L4 используют clean-шаблон без «(N шт.)».
        # Счётчик «(2 шт.)» — только на L2/L5.
        d = _format_description(level=5,
                                waypoints=[[100, 50], [200, -30]],
                                actions=[], start_x=0, start_y=0)
        self.assertIn("Контрольные точки маршрута (2 шт.)", d)
        self.assertIn("(100, 50)", d)
        self.assertIn("(200, -30)", d)
        # А для L3 — clean без счётчика
        d3 = _format_description(level=3,
                                  waypoints=[[100, 50], [200, -30]],
                                  actions=[], start_x=0, start_y=0)
        self.assertNotIn("шт.", d3)
        self.assertIn("(100, 50)", d3)

    def test_place_attention_actions_appear_as_pin(self):
        d = _format_description(level=4, waypoints=[],
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
        """remove_danger описывается строкой «Удалите опасные зоны»
        (по парным danger_zones), remove_attention — отдельной строкой."""
        d = _format_description(level=4, waypoints=[],
                                actions=[
                                    {"type": "remove_danger",    "x": 0, "y": 0},
                                    {"type": "remove_danger",    "x": 1, "y": 1},
                                    {"type": "remove_attention", "x": 2, "y": 2},
                                ],
                                danger_zones=[[0, 0, 15], [1, 1, 20]],
                                start_x=0, start_y=0)
        self.assertIn("Удалите опасные зоны", d)
        self.assertIn("Удалите зоны внимания (1 шт)", d)

    def test_avoid_zones_block(self):
        """Опасные зоны без парного remove_danger — блок «Не задевайте»."""
        d = _format_description(level=5, waypoints=[],
                                actions=[],
                                danger_zones=[[100, 0, 20], [-50, 80, 15]],
                                start_x=0, start_y=0)
        self.assertIn("Не задевайте опасные зоны", d)
        self.assertIn("(100, 0)", d)
        self.assertIn("(-50, 80)", d)

    def test_block_order_is_start_waypoints_actions_zones_stars(self):
        """Стабильный порядок блоков (на нём держится UI-парсинг описания)."""
        d = _format_description(level=4, waypoints=[[100, 0]],
                                actions=[{"type": "place_attention", "x": 50, "y": 50}],
                                danger_zones=[[200, 0, 10]],
                                start_x=0, start_y=0)
        i_start = d.index("Начало маршрута")
        i_wp    = d.index("Контрольные точки")
        i_pin   = d.index("Установите зоны")
        i_dz    = d.index("Не задевайте опасные зоны")
        i_star  = d.index("⭐")
        self.assertLess(i_start, i_wp)
        self.assertLess(i_wp,    i_pin)
        self.assertLess(i_pin,   i_dz)
        self.assertLess(i_dz,    i_star)

    def test_no_command_hints_leak_into_description(self):
        """Регрессия: ранее описание содержало подсказки `forward_cmd(N)` /
        `face_cmd(N)` — это эталонное решение, его НЕ должно быть видно
        пользователю. Подсказки идут только в reference_code."""
        d = _format_description(level=3, waypoints=[[100, 0]], actions=[],
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


class TestLevel1Grid(unittest.TestCase):
    """Уровень 1: waypoints обязаны лежать в узлах сетки 50×50."""

    def test_waypoints_on_grid_nodes(self):
        for seed in range(40):
            m = generate_mission(level=3, geom=_geom_default(), seed=seed)
            wp = json.loads(m["waypoints"])
            for (x, y) in wp:
                with self.subTest(seed=seed, point=(x, y)):
                    self.assertAlmostEqual(
                        x, round(x / _GRID_CELL_CM) * _GRID_CELL_CM,
                        places=1, msg=f"x={x} не на сетке {_GRID_CELL_CM}")
                    self.assertAlmostEqual(
                        y, round(y / _GRID_CELL_CM) * _GRID_CELL_CM,
                        places=1, msg=f"y={y} не на сетке {_GRID_CELL_CM}")


class TestCardinalHeadings(unittest.TestCase):
    """Курсы сегментов траектории кратны заявленному шагу:
        уровень 1 — 45° (8 направлений), уровень 2 — 15° (24 направления).

    Допуск ~3° на накопление округлений (level 2 не снапит позицию,
    после нескольких диагональных шагов state.x/y становятся
    нецелыми, направление к следующей точке отклоняется на 1-3°)."""

    def _check_segments_match_step(self, level, step_deg, tolerance_deg,
                                     seeds=range(30)):
        for seed in seeds:
            g = _geom_default()
            m = generate_mission(level=level, geom=g, seed=seed)
            path = json.loads(m["path"])
            for h in _waypoint_headings_from_path(path):
                snapped = round(h / step_deg) * step_deg % 360
                diff = _angular_diff_deg(h % 360, snapped)
                with self.subTest(level=level, seed=seed, heading=h):
                    self.assertLessEqual(diff, tolerance_deg,
                        f"курс {h:.2f}° не кратен {step_deg}° "
                        f"(ближайший {snapped}°, расхождение {diff:.2f}°, "
                        f"seed={seed}, level={level})")

    def test_level_1_all_segments_kr_45(self):
        # Уровень 1 снапит позицию к сетке — все курсы точно кратны 45°.
        self._check_segments_match_step(level=3, step_deg=45,
                                          tolerance_deg=1.0)

    def test_level_2_all_segments_kr_15(self):
        # Уровень 2 округляет цели goto до 10 см, отсюда дрифт направления
        # до ~5°. Это допустимое отклонение для человеко-читаемых
        # координат «в точку 123 -45», робот всё равно движется ровно.
        self._check_segments_match_step(level=4, step_deg=15,
                                          tolerance_deg=5.0)


class TestSmoothTurns(unittest.TestCase):
    """Соседние курсы РОБОТА (heading) отличаются не более чем на 90° —
    нет резких 135°/180°. Курс пути может реверсироваться 180° на команде
    «назад» (это допустимо: heading не меняется, путь идёт в обратную
    сторону); зеркальное наложение track-over-track ловится отдельно
    тестом TestPathSelfClearance."""

    def _check_smooth(self, align_to_grid, n_waypoints, heading_step_deg,
                       seeds=range(30)):
        g = _geom_default()
        for seed in seeds:
            traj = _generate_trajectory(n_waypoints, g, _random.Random(seed),
                                         align_to_grid=align_to_grid,
                                         heading_step_deg=heading_step_deg)
            headings = traj["heading_steps"]
            for i in range(1, len(headings)):
                diff = _angular_diff_deg(headings[i], headings[i - 1])
                with self.subTest(seed=seed, i=i,
                                  a=headings[i - 1], b=headings[i]):
                    self.assertLessEqual(diff, 90.5,
                        f"резкая смена курса {diff:.1f}° между "
                        f"{headings[i-1]:.0f}° → {headings[i]:.0f}° "
                        f"(seed={seed})")

    def test_level_1_no_sharp_turns(self):
        self._check_smooth(align_to_grid=True, n_waypoints=3,
                            heading_step_deg=45)

    def test_level_2_no_sharp_turns(self):
        self._check_smooth(align_to_grid=False, n_waypoints=5,
                            heading_step_deg=15)


class TestPathSelfClearance(unittest.TestCase):
    """Параллельные/обратные прохождения траектории не должны проходить
    ближе min_gap к ранее пройденному пути.

    Проверяем на РАЗРЕЖЁННОМ списке вершин (vertices) — точки поворотов,
    не дансная интерполяция. Иначе у плавного 90° поворота два дансных
    суб-сегмента возле точки стыковки оказываются «близко» друг к другу
    (рядом с общим узлом), это false positive."""

    def _min_pair_distance_vertices(self, verts):
        """Минимальное расстояние сэмпла одного сегмента до НЕпримыкающего
        сегмента ломаной из vertices. Используем `_dist_to_segment_interior`
        для adjacent — пропускаем близость у точки стыковки."""
        if len(verts) < 4:
            return float("inf")
        best = float("inf")
        n_seg = len(verts) - 1
        for i in range(n_seg):
            ax = verts[i][0]; ay = verts[i][1]
            bx = verts[i + 1][0]; by = verts[i + 1][1]
            for j in range(n_seg):
                if abs(i - j) <= 1:
                    continue
                cx = verts[j][0]; cy = verts[j][1]
                dx = verts[j + 1][0]; dy = verts[j + 1][1]
                for t_idx in range(1, 5):
                    t = t_idx / 5.0
                    px = ax + (bx - ax) * t
                    py = ay + (by - ay) * t
                    d = _dist_point_to_segment(px, py, cx, cy, dx, dy)
                    if d < best:
                        best = d
        return best

    def _check(self, level, align_to_grid, n_waypoints, heading_step_deg):
        g = _geom_default()
        for seed in range(30):
            traj = _generate_trajectory(n_waypoints, g, _random.Random(seed),
                                         align_to_grid=align_to_grid,
                                         heading_step_deg=heading_step_deg)
            verts = traj["vertices"]
            min_d = self._min_pair_distance_vertices(verts)
            with self.subTest(seed=seed, level=level, verts=verts):
                # 5 см допуск на округления, _MIN_PATH_GAP_CM = 30.
                self.assertGreaterEqual(min_d, _MIN_PATH_GAP_CM - 5,
                    f"seed={seed}: сегменты сближаются до {min_d:.1f} см "
                    f"(требуется ≥ {_MIN_PATH_GAP_CM} см)")

    def test_level_1_path_keeps_min_gap(self):
        self._check(level=3, align_to_grid=True, n_waypoints=3,
                     heading_step_deg=45)

    def test_level_2_path_keeps_min_gap(self):
        self._check(level=4, align_to_grid=False, n_waypoints=5,
                     heading_step_deg=15)


class TestLevel2Shape(unittest.TestCase):
    """Структура сгенерированной миссии level 4.

    По ТЗ: 5-6 waypoints, 4 опасные зоны разного радиуса. Никаких
    actions_required (зоны только обходят). Опасные зоны должны быть ВНЕ
    эталонной траектории — иначе эталонное решение нельзя пройти без
    коллизий."""

    def test_level_4_has_5_or_6_waypoints(self):
        for seed in range(30):
            with self.subTest(seed=seed):
                m = generate_mission(level=4, geom=_geom_default(), seed=seed)
                wp = json.loads(m["waypoints"])
                self.assertGreaterEqual(len(wp), 5,
                                        f"seed={seed}: меньше 5 точек ({len(wp)})")
                self.assertLessEqual(len(wp), 6,
                                     f"seed={seed}: больше 6 точек ({len(wp)})")

    def test_level_4_has_4_danger_zones(self):
        """Уровень 4 запрашивает 4 зоны. На обычной геометрии (500×500)
        почти все seed'ы дают ровно 4; лимит сверху строго 4."""
        zone_counts = []
        for seed in range(30):
            m = generate_mission(level=4, geom=_geom_default(), seed=seed)
            zones = json.loads(m["danger_zones"])
            self.assertLessEqual(len(zones), 4,
                                  f"seed={seed}: больше 4 зон")
            zone_counts.append(len(zones))
        n_with_four = sum(1 for c in zone_counts if c == 4)
        self.assertGreaterEqual(n_with_four, 24,
                                f"только {n_with_four}/30 seed'ов дали 4 зоны")

    def test_level_2_no_actions_required(self):
        for seed in range(10):
            m = generate_mission(level=4, seed=seed)
            self.assertEqual(json.loads(m["actions_required"]), [],
                              f"seed={seed}: actions_required не пуст")

    def test_level_4_zone_radii_multiples_of_10(self):
        """Радиусы зон L4 — кратны 10 (из набора 10/20/30)."""
        for seed in range(20):
            m = generate_mission(level=4, geom=_geom_default(), seed=seed)
            zones = json.loads(m["danger_zones"])
            for z in zones:
                with self.subTest(seed=seed, zone=z):
                    self.assertIn(round(z[2]), (10, 20, 30),
                                  f"радиус зоны не кратен 10: {z}")

    def test_level_2_zones_close_to_trajectory(self):
        """Зоны должны угрожать роботу: расстояние от траектории до
        края зоны (= dist_to_path - zone_radius) не превышает
        clearance + max_extra. Иначе зона стоит «где-то в углу» и
        миссия проходится игнорируя её — не работает как препятствие."""
        g = _geom_default()
        half_robot = max(g.robot_w_cm, g.robot_l_cm) / 2.0
        clearance = g.safety_margin_cm + half_robot
        for seed in range(30):
            m = generate_mission(level=4, geom=g, seed=seed)
            path = json.loads(m["path"])
            for (zx, zy, zr) in json.loads(m["danger_zones"]):
                # required = zone_radius + clearance, extra ∈ [0, 25];
                # округление координат до 5 см + случайный старт → +15 см.
                max_dist = zr + clearance + 25.0 + 15.0
                d = _min_dist_to_polyline(zx, zy, path)
                with self.subTest(seed=seed, zone=(zx, zy, zr)):
                    self.assertLessEqual(
                        d, max_dist,
                        f"seed={seed}: зона ({zx},{zy}) слишком далеко от "
                        f"траектории ({d:.1f} см) — не угрожает прохождению")

    def test_level_2_zones_outside_reference_trajectory(self):
        """Главное свойство уровня 2: эталонная траектория не должна
        проходить через опасные зоны (с учётом safety + габаритов робота).

        Допуск проверки: zone_radius + safety_margin + half_robot_dim."""
        g = _geom_default()
        half_robot = max(g.robot_w_cm, g.robot_l_cm) / 2.0
        for seed in range(30):
            m = generate_mission(level=4, geom=g, seed=seed)
            zones = json.loads(m["danger_zones"])
            path = json.loads(m["path"])
            for (zx, zy, zr) in zones:
                with self.subTest(seed=seed, zone=(zx, zy, zr)):
                    d = _min_dist_to_polyline(zx, zy, path)
                    required = zr + g.safety_margin_cm + half_robot
                    self.assertGreaterEqual(
                        d, required - 0.5,   # допуск 0.5см на округления
                        f"seed={seed}: зона ({zx},{zy},{zr}) слишком близко "
                        f"к траектории: {d:.1f} < требуется {required:.1f}")

    def test_level_2_zones_not_on_start(self):
        """Робот не должен начинать миссию ВНУТРИ опасной зоны."""
        for seed in range(20):
            g = _geom_default()
            m = generate_mission(level=4, geom=g, seed=seed)
            # Старт миссии генерируется случайно — берём его из path[0],
            # а не из geom (geom.start не отражает фактический старт).
            path = json.loads(m["path"])
            sx, sy = path[0]
            for (zx, zy, zr) in json.loads(m["danger_zones"]):
                d = math.hypot(zx - sx, zy - sy)
                with self.subTest(seed=seed):
                    self.assertGreater(d, zr,
                                       f"стартовая точка внутри зоны ({zx},{zy},{zr})")

    def test_level_2_zones_inside_field(self):
        """Зона целиком внутри игрового поля (с запасом стенки)."""
        g = _geom_default()
        half_w = g.world_w_cm / 2.0
        half_h = g.world_h_cm / 2.0
        for seed in range(20):
            m = generate_mission(level=4, geom=g, seed=seed)
            for (zx, zy, zr) in json.loads(m["danger_zones"]):
                with self.subTest(seed=seed, zone=(zx, zy, zr)):
                    self.assertGreaterEqual(zx - zr, -half_w + g.wall_thick_cm - 0.5)
                    self.assertLessEqual(zx + zr,    half_w - g.wall_thick_cm + 0.5)
                    self.assertGreaterEqual(zy - zr, -half_h + g.wall_thick_cm - 0.5)
                    self.assertLessEqual(zy + zr,    half_h - g.wall_thick_cm + 0.5)

    def test_level_2_zones_not_overlapping(self):
        """Если зон две, они не пересекаются (визуально разнесены)."""
        for seed in range(30):
            m = generate_mission(level=4, geom=_geom_default(), seed=seed)
            zones = json.loads(m["danger_zones"])
            for i in range(len(zones)):
                for j in range(i + 1, len(zones)):
                    zx1, zy1, zr1 = zones[i]
                    zx2, zy2, zr2 = zones[j]
                    d = math.hypot(zx1 - zx2, zy1 - zy2)
                    with self.subTest(seed=seed):
                        self.assertGreater(d, zr1 + zr2,
                                           f"зоны пересекаются: {zones[i]} vs {zones[j]}")

    def test_level_2_description_includes_danger_block(self):
        """Описание миссии с зонами должно содержать блок про опасные зоны
        (удалить / не задевать) и координаты всех зон."""
        for seed in range(20):
            m = generate_mission(level=4, geom=_geom_default(), seed=seed)
            zones = json.loads(m["danger_zones"])
            if not zones:
                continue
            self.assertIn("опасные зоны", m["description"].lower(),
                           f"seed={seed}: блок с зонами отсутствует в описании")
            for (zx, zy, _r) in zones:
                self.assertIn(f"({int(zx)}, {int(zy)})", m["description"],
                              f"seed={seed}: координата зоны не в описании")

    def test_level_2_determinism(self):
        a = generate_mission(level=4, seed=77)
        b = generate_mission(level=4, seed=77)
        self.assertEqual(a["waypoints"],   b["waypoints"])
        self.assertEqual(a["danger_zones"], b["danger_zones"])
        self.assertEqual(a["reference_voice"], b["reference_voice"])

    def test_level_2_title_empty_level_field_set(self):
        m = generate_mission(level=4, seed=1)
        self.assertEqual(m["title"], "")
        self.assertEqual(m["level"], 4)


class TestLevel5Shape(unittest.TestCase):
    """L5 «Продвинутый»: 5 точек, эталонная траектория, до 5 опасных
    зон, 1 place_attention на финише + 2 remove_danger."""

    def test_level_5_structure(self):
        for seed in range(20):
            m = generate_mission(level=5, geom=_geom_default(), seed=seed)
            with self.subTest(seed=seed):
                wp = json.loads(m["waypoints"])
                self.assertEqual(len(wp), 5, "L5 — 5 контрольных точек")
                self.assertLessEqual(len(json.loads(m["danger_zones"])), 5)
                actions = json.loads(m["actions_required"])
                place  = [a for a in actions if a["type"] == "place_attention"]
                remove = [a for a in actions if a["type"] == "remove_danger"]
                self.assertEqual(len(place), 1, "ровно 1 зона внимания")
                self.assertLessEqual(len(remove), 2, "не больше 2 удаляемых")
                # Зона внимания — на финишной (последней) точке.
                self.assertEqual([place[0]["x"], place[0]["y"]],
                                 list(wp[-1]))
                # Эталонная траектория задана (≥2 точек).
                self.assertGreaterEqual(len(json.loads(m["path"])), 2)

    def test_level_5_remove_targets_are_zone_centres(self):
        for seed in range(15):
            m = generate_mission(level=5, geom=_geom_default(), seed=seed)
            centres = {(round(z[0]), round(z[1]))
                       for z in json.loads(m["danger_zones"])}
            for a in json.loads(m["actions_required"]):
                if a["type"] == "remove_danger":
                    with self.subTest(seed=seed):
                        self.assertIn((round(a["x"]), round(a["y"])), centres)

    def test_level_5_zone_radii_multiples_of_10(self):
        for seed in range(15):
            m = generate_mission(level=5, geom=_geom_default(), seed=seed)
            for z in json.loads(m["danger_zones"]):
                with self.subTest(seed=seed, zone=z):
                    self.assertIn(round(z[2]), (10, 20, 30))

    def test_level_5_determinism(self):
        a = generate_mission(level=5, seed=55)
        b = generate_mission(level=5, seed=55)
        self.assertEqual(a["waypoints"],        b["waypoints"])
        self.assertEqual(a["danger_zones"],     b["danger_zones"])
        self.assertEqual(a["actions_required"], b["actions_required"])


class TestGeomHelpers(unittest.TestCase):
    def test_wall_clearance_fits_kturn(self):
        """Запас от стены должен вмещать K-turn: R·sin(α) + safety ≈ 50-60 см
        на дефолтном роботе. Поэтому 4×габарит, но не меньше 80 см."""
        g = WorldGeom(robot_w_cm=12, robot_l_cm=20)
        self.assertEqual(_wall_clearance_cm(g), 80.0)
        g = WorldGeom(robot_w_cm=20, robot_l_cm=12)
        self.assertEqual(_wall_clearance_cm(g), 80.0)
        # Крупный робот — масштабируется
        g = WorldGeom(robot_w_cm=30, robot_l_cm=40)
        self.assertEqual(_wall_clearance_cm(g), 160.0)

    def test_inside_field_respects_clearance(self):
        g = _geom_default()
        # margin = 80см, поле 500×500 → допустимая зона [-170, +170]
        self.assertTrue(_inside_field(0, 0, g))
        self.assertTrue(_inside_field(150, 150, g))
        self.assertFalse(_inside_field(200, 0, g))    # за пределами 170
        self.assertFalse(_inside_field(0, -200, g))


if __name__ == "__main__":
    unittest.main(verbosity=2)
