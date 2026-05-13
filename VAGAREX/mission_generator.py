"""mission_generator.py — генерация миссий для робота.

Уровни сложности (1..5):
  1. Ознакомительный: 2-3 точки, без опасных зон.
  2. Начальный:       4-5 точек, 1-2 опасные зоны.
  3. Базовый:         6-7 точек, 2 опасные (10см) + 3 зоны внимания.
  4. Углублённый:     8-9 точек, 3-4 опасные (10-20см) + 3-4 зоны
                       внимания + удаление всех опасных.
  5. Продвинутый:     10 точек, 5 опасных (10-20см) + 4 зоны внимания
                       + удаление всех опасных.

На текущем этапе реализован только level 1 (vertical slice). Остальные
уровни — следующим этапом.

API:
    generate_mission(level=1, cfg=...) -> dict
        возвращает словарь, готовый к сохранению в Mission(...)

Чистая библиотека: без зависимостей от FastAPI/БД, только cfg-объект
с физикой (для проверки коллизий со стенами).
"""
from __future__ import annotations

import json
import math
import random
from dataclasses import dataclass
from typing import Optional


# ── Параметры физики ───────────────────────────────────────────────────────

@dataclass
class WorldGeom:
    """Минимум, что нужно генератору: размеры поля и габариты робота.
    Заполняется из UserCfg либо из глобальных дефолтов."""
    world_w_cm:     float = 500.0
    world_h_cm:     float = 500.0
    wall_thick_cm:  float =   5.0
    robot_w_cm:     float =  12.0
    robot_l_cm:     float =  20.0
    safety_margin_cm: float = 5.0
    start_x:        float =   0.0
    start_y:        float =   0.0
    start_heading:  float =   0.0
    danger_zone_radius_cm: float = 10.0


def _wall_clearance_cm(g: WorldGeom) -> float:
    """Минимальное допустимое расстояние от центра робота до стены.

    Должно вмещать манёвр K-turn (3-дуговой разворот): при минимальном
    радиусе поворота R ≈ 80 см робот сметает вперёд до R·sin(α) ≈ 33 см
    плюс 20 см safety. Поэтому 2× габариты (= 40 см) тесно — точки
    у стены не дают развернуться. 4× габариты (= 80 см на дефолтном
    роботе 12×20) гарантируют пространство для разворота с любого
    подхода. Минимум 80 см на случай очень маленьких роботов."""
    return max(80.0, 4.0 * max(g.robot_w_cm, g.robot_l_cm))


# ── Случайный выбор команды и симуляция ────────────────────────────────────

# Пул команд для уровня 1 — простые и предсказуемые. Никаких микрокоманд
# (steer, set_speed). Никаких циклических (circle/spiral/eight) — они
# возвращают робота в исходную точку, новых waypoints не дают.
#
# Каждая запись: (intent, voice_template, code_template, simulator_fn).
# simulator_fn(state, geom) -> (new_state, was_collision)
#
# state: dict {"x":, "y":, "heading":}.

def _step_forward(state: dict, dist_cm: float) -> dict:
    h = math.radians(state["heading"])
    return {
        "x":       state["x"] + math.sin(h) * dist_cm,
        "y":       state["y"] + math.cos(h) * dist_cm,
        "heading": state["heading"],
    }


def _step_back(state: dict, dist_cm: float) -> dict:
    return _step_forward(state, -dist_cm)


def _step_face(state: dict, target_deg: float) -> dict:
    """K-turn возвращает робота в исходную позицию (по геометрии Reeds-
    Shepp), меняется только heading. Поэтому позиция не двигается."""
    return {
        "x":       state["x"],
        "y":       state["y"],
        "heading": float(target_deg) % 360.0,
    }


def _inside_field(x: float, y: float, g: WorldGeom) -> bool:
    """Точка внутри игрового поля с запасом wall_clearance."""
    margin = _wall_clearance_cm(g)
    half_w = g.world_w_cm / 2.0
    half_h = g.world_h_cm / 2.0
    return (
        -half_w + margin <= x <= half_w - margin
        and -half_h + margin <= y <= half_h - margin
    )


# ── Кардинальные курсы, сетка 50×50, плавность поворотов ───────────────────

# Уровень 1 ограничен 8 кардинальными курсами (шаг 45°) — «школьно-чистая»
# траектория: только стороны света и диагонали. Уровень 2 — 24 курса
# (шаг 15°), более плавная геометрия. На уровнях 3+ предполагается ещё
# мельче либо произвольный угол.
_CARDINAL_HEADINGS = (0, 45, 90, 135, 180, 225, 270, 315)
_HEADINGS_15_DEG   = tuple(range(0, 360, 15))   # 0, 15, 30, ..., 345

# Шаги по уровням (передаются в _generate_trajectory).
_LEVEL_HEADING_STEP_DEG = {1: 45, 2: 15}

# Русские названия + helper-команды для каждого из 8 курсов. Используется
# в face-кандидате (вместо литеральных списков в коде).
_CARDINAL_NAMES = {
    0:   ("север",         "face_n_cmd()"),
    45:  ("северо-восток", "face_ne_cmd()"),
    90:  ("восток",        "face_e_cmd()"),
    135: ("юго-восток",    "face_se_cmd()"),
    180: ("юг",            "face_s_cmd()"),
    225: ("юго-запад",     "face_sw_cmd()"),
    270: ("запад",         "face_w_cmd()"),
    315: ("северо-запад",  "face_nw_cmd()"),
}

# Максимальный сдвиг курса за один поворотный шаг: 90°. 135° и 180°
# отбрасываются как «резкие смены курса» — робот разворачивается на месте
# и идёт почти обратно, что (а) ухудшает восприятие траектории, (б) часто
# провоцирует прохождение нового сегмента вплотную к старому.
_MAX_TURN_PER_STEP_DEG = 90.0

# Минимальный зазор между непримыкающими сегментами траектории. Если новый
# сегмент подходит ближе — генератор отбрасывает его и пробует другой
# вариант. На дефолтной геометрии (safety=5, robot=12×20) реальный зазор
# будет 2×(safety+half_robot) = 30 см, иначе минимум — этот же.
_MIN_PATH_GAP_CM = 30.0

# Сетка узлов 50×50 см (только для уровня 1 — align_to_grid=True).
# Дистанции на ортогональных курсах кратны клетке; на диагональных —
# кратны клетке × √2 ≈ 70 см (50.20 см ортогонального смещения, дрифт
# до ~0.2 см от узла при каждом диагональном шаге).
_GRID_CELL_CM = 50.0
_GRID_DIAG_CM = 70.0          # ≈ round(50 × √2) = 71, округлено до десятка
_ORTH_DISTANCES      = (50, 100, 150, 200)
_DIAG_DISTANCES      = (70, 140, 210)
_ORTH_BACK_DISTANCES = (50, 100)
_DIAG_BACK_DISTANCES = (70, 140)


def _snap_to_step(deg: float, step_deg: int) -> float:
    """Привязать угол к ближайшему кратному step_deg в [0, 360)."""
    return float(round(deg / step_deg) * step_deg % 360)


def _snap_to_45(deg: float) -> float:
    """Совместимость: некоторые места кода исторически снапают к 45°."""
    return _snap_to_step(deg, 45)


def _angular_diff_deg(a: float, b: float) -> float:
    """Минимальный угловой сдвиг между двумя курсами ∈ [0, 180]."""
    return abs(((a - b + 180) % 360) - 180)


def _allowed_headings_from(heading: float, step_deg: int,
                             max_turn_deg: float = _MAX_TURN_PER_STEP_DEG
                             ) -> list[float]:
    """Какие курсы кратные step_deg разрешены, при ограничении угла
    поворота max_turn_deg? Исключает текущий курс (no-op) и резкие
    повороты > max_turn_deg. step_deg=45 → 8 кардиналов; step_deg=15
    → 24 направления."""
    cur = _snap_to_step(heading, step_deg)
    out = []
    for h in range(0, 360, step_deg):
        if h == int(cur):
            continue
        if _angular_diff_deg(h, cur) <= max_turn_deg:
            out.append(float(h))
    return out


def _allowed_cardinals_from(heading: float,
                             max_turn_deg: float = _MAX_TURN_PER_STEP_DEG
                             ) -> list[float]:
    """Совместимость: 8 кардиналов (step=45)."""
    return _allowed_headings_from(heading, 45, max_turn_deg)


def _is_diagonal_heading(deg: float) -> bool:
    """45/135/225/315 — диагональные курсы (dx и dy не нули)."""
    return int(round(deg)) % 90 == 45


def _snap_to_grid(value: float) -> float:
    return float(round(value / _GRID_CELL_CM) * _GRID_CELL_CM)


def _snap_state_to_grid(state: dict) -> dict:
    """Снэп позиции состояния к ближайшему узлу сетки 50×50.
    Применяется после каждого forward/back/goto на уровне 1 — чтобы
    waypoint оказывался ровно в узле сетки. Реальный дрифт робота
    относительно узла (≤ 1 см за диагональ) поглощается safety_margin."""
    return {"x": _snap_to_grid(state["x"]),
            "y": _snap_to_grid(state["y"]),
            "heading": state["heading"]}


# ── Геометрический helper: зазор между новой и прошлой траекториями ────────

def _path_seg_clears_prior(path_seg: list[list[float]],
                            full_path_before: list[list[float]],
                            min_gap_cm: float,
                            sample_step_cm: float = 15.0) -> bool:
    """True если новый кусок path_seg не приближается ближе min_gap_cm к
    РАНЕЕ пройденному пути.

    Тонкость стыковки решается через `_dist_to_segment_interior`: для
    последнего (adjacent) prior-сегмента считаем расстояние только тогда,
    когда подножие перпендикуляра падает СТРОГО внутрь prior. Это
    отсеивает естественную близость новой команды к точке стыковки и
    при этом ловит обратное наложение (back с разворотом > 90° по пути).
    Для непримыкающих prior — обычная дистанция до отрезка."""
    if len(path_seg) < 2 or len(full_path_before) < 2:
        return True
    n_prior = len(full_path_before) - 1
    if n_prior < 1:
        return True
    adjacent_idx = n_prior - 1
    for k in range(1, len(path_seg)):
        a = path_seg[k - 1]
        b = path_seg[k]
        seg_L = math.hypot(b[0] - a[0], b[1] - a[1])
        if seg_L < 1.0:
            continue
        n_samples = max(2, int(seg_L / sample_step_cm))
        for i in range(1, n_samples + 1):
            t = i / n_samples
            px = a[0] + (b[0] - a[0]) * t
            py = a[1] + (b[1] - a[1]) * t
            for seg_idx in range(n_prior):
                sa = full_path_before[seg_idx]
                sb = full_path_before[seg_idx + 1]
                if seg_idx == adjacent_idx:
                    d = _dist_to_segment_interior(
                        px, py, sa[0], sa[1], sb[0], sb[1])
                else:
                    d = _dist_point_to_segment(
                        px, py, sa[0], sa[1], sb[0], sb[1])
                if d < min_gap_cm:
                    return False
    return True


# ── Кандидаты команд ───────────────────────────────────────────────────────

# Каждый кандидат: функция(state, geom, rng, **kwargs) -> tuple | None.
# Возвращает (new_state, voice, code, path_seg). None — «команду нельзя
# использовать сейчас» (например, не хватает места впереди).
#
# Опциональные kwargs:
#   align_to_grid — снэпать позицию к узлам сетки 50×50 (только уровень 1).
#   prior_path    — уже пройденный full_path для проверки зазора.
#   min_gap_cm    — минимальное расстояние между новым и прошлым путём.

def _interpolate_straight(a: dict, b: dict, step_cm: float = 5.0) -> list[list[float]]:
    """Список точек вдоль прямой [a..b] с шагом step_cm.
    Включает обе крайние точки. Если a≈b — возвращает только [a]."""
    dx = b["x"] - a["x"]; dy = b["y"] - a["y"]
    dist = math.hypot(dx, dy)
    if dist < 0.5:
        return [[round(a["x"], 1), round(a["y"], 1)]]
    n = max(1, int(dist / step_cm))
    pts = []
    for i in range(n + 1):
        t = i / n
        pts.append([round(a["x"] + dx * t, 1), round(a["y"] + dy * t, 1)])
    return pts


def _legacy_forward_distances(g: WorldGeom, rng: random.Random) -> list[int]:
    """Старая логика дистанций — пропорционально полю, кратно 10 см.
    Используется когда align_to_grid=False (уровень 2+)."""
    half_min = min(g.world_w_cm, g.world_h_cm) / 2.0
    base = max(80.0, half_min - _wall_clearance_cm(g))
    dists = []
    for shrink in (1.0, 0.7, 0.5, 0.35):
        d = int(round(rng.uniform(0.35, 0.85) * base * shrink / 10) * 10)
        if d >= 30:
            dists.append(d)
    return dists


def _legacy_back_distances(g: WorldGeom, rng: random.Random) -> list[int]:
    half_min = min(g.world_w_cm, g.world_h_cm) / 2.0
    base = max(60.0, half_min - _wall_clearance_cm(g))
    dists = []
    for shrink in (1.0, 0.6, 0.4):
        d = int(round(rng.uniform(0.2, 0.45) * base * shrink / 10) * 10)
        if d >= 20:
            dists.append(d)
    return dists


def _candidate_forward(state: dict, g: WorldGeom, rng: random.Random, *,
                        align_to_grid: bool = False,
                        prior_path: Optional[list] = None,
                        min_gap_cm: Optional[float] = None,
                        heading_step_deg: int = 45):  # noqa: ARG001 — единый kwargs
    """Forward на текущий курс. На уровне 1 (align_to_grid=True) дистанция
    выбирается из набора, соответствующего ортогональному/диагональному
    курсу (50/100/150/200 или 70/140/210 см) — endpoint попадает в узел
    сетки. На уровне 2 — старая логика «пропорционально полю»."""
    if align_to_grid:
        if _is_diagonal_heading(state["heading"]):
            dists = list(_DIAG_DISTANCES)
        else:
            dists = list(_ORTH_DISTANCES)
    else:
        dists = _legacy_forward_distances(g, rng)
    rng.shuffle(dists)
    for dist in dists:
        new = _step_forward(state, dist)
        if not _inside_field(new["x"], new["y"], g):
            continue
        if align_to_grid:
            new = _snap_state_to_grid(new)
        path_seg = _interpolate_straight(state, new)
        if (prior_path is not None and min_gap_cm is not None
                and not _path_seg_clears_prior(path_seg, prior_path, min_gap_cm)):
            continue
        return (new, f"Вега вперед {int(dist)} см",
                f"forward_cmd({int(dist)})", path_seg)
    return None


def _candidate_back(state: dict, g: WorldGeom, rng: random.Random, *,
                     align_to_grid: bool = False,
                     prior_path: Optional[list] = None,
                     min_gap_cm: Optional[float] = None,
                     heading_step_deg: int = 45):  # noqa: ARG001 — единый kwargs
    """Back-движение. Дистанции короче, чем у forward — иначе после
    «вперёд 200, назад 200» робот вернётся в старт. Сетка 50×50 — те же
    клеточные дистанции, но укороченный набор."""
    if align_to_grid:
        if _is_diagonal_heading(state["heading"]):
            dists = list(_DIAG_BACK_DISTANCES)
        else:
            dists = list(_ORTH_BACK_DISTANCES)
    else:
        dists = _legacy_back_distances(g, rng)
    rng.shuffle(dists)
    for dist in dists:
        new = _step_back(state, dist)
        if not _inside_field(new["x"], new["y"], g):
            continue
        if align_to_grid:
            new = _snap_state_to_grid(new)
        path_seg = _interpolate_straight(state, new)
        if (prior_path is not None and min_gap_cm is not None
                and not _path_seg_clears_prior(path_seg, prior_path, min_gap_cm)):
            continue
        return (new, f"Вега назад {int(dist)} см",
                f"back_cmd({int(dist)})", path_seg)
    return None


def _candidate_face_cardinal(state: dict, g: WorldGeom, rng: random.Random, *,
                               align_to_grid: bool = False,
                               prior_path: Optional[list] = None,
                               min_gap_cm: Optional[float] = None,
                               heading_step_deg: int = 45):
    """Поворот на месте к одному из разрешённых курсов кратных
    heading_step_deg. Уровень 1: step=45° (8 кардиналов с русскими
    названиями). Уровень 2: step=15° (24 направления, для не-кардиналов
    используется голосовая фраза «Вега поверни на N»).

    Разрешены только направления в пределах ±90° от текущего курса —
    резкие повороты (> 90°) исключены."""
    allowed = _allowed_headings_from(state["heading"], heading_step_deg)
    if not allowed:
        return None
    deg = int(rng.choice(allowed))
    if deg in _CARDINAL_NAMES:
        ru, code = _CARDINAL_NAMES[deg]
        voice = f"Вега {ru}"
    else:
        voice = f"Вега поверни на {deg}"
        code  = f"face_cmd({deg})"
    new = _step_face(state, deg)
    path_seg = [[round(state["x"], 1), round(state["y"], 1)]]
    return new, voice, code, path_seg


def _candidate_goto(state: dict, g: WorldGeom, rng: random.Random, *,
                     align_to_grid: bool = False,
                     prior_path: Optional[list] = None,
                     min_gap_cm: Optional[float] = None,
                     heading_step_deg: int = 45):
    """«Вега в точку X Y» — цель строго на одном из разрешённых лучей
    из текущей позиции (направление кратно heading_step_deg) и не дальше
    ±90° от текущего курса (плавный поворот внутри goto).

    align_to_grid=True: цель — узел сетки 50×50, дистанции из набора
    50/100/150/200 (ортогональ) или 70/140/210 (диагональ).
    align_to_grid=False: дистанция произвольная, кратна 10 см."""
    allowed = _allowed_headings_from(state["heading"], heading_step_deg)
    if not allowed:
        return None
    if align_to_grid:
        # На уровне 1 — кардинал-зависимые дистанции из сетки 50×50.
        # На ортогональных = N×50, на диагональных = N×70.
        candidates = list(allowed)
        rng.shuffle(candidates)
        for deg in candidates:
            dists = (list(_DIAG_DISTANCES) if _is_diagonal_heading(deg)
                     else list(_ORTH_DISTANCES))
            rng.shuffle(dists)
            for dist in dists:
                h = math.radians(deg)
                tx = state["x"] + math.sin(h) * dist
                ty = state["y"] + math.cos(h) * dist
                new = _snap_state_to_grid({"x": tx, "y": ty, "heading": float(deg)})
                if not _inside_field(new["x"], new["y"], g):
                    continue
                if math.hypot(new["x"] - state["x"],
                              new["y"] - state["y"]) < 50.0:
                    continue
                path_seg = _interpolate_straight(state, new)
                if (prior_path is not None and min_gap_cm is not None
                        and not _path_seg_clears_prior(path_seg, prior_path, min_gap_cm)):
                    continue
                return (new, f"Вега в точку {new['x']:g} {new['y']:g}",
                        f"goto_cmd({new['x']:g}, {new['y']:g})", path_seg)
        return None
    # Легаси (уровень 2): целевая дистанция произвольная, кратна 10 см,
    # направление — один из allowed кардиналов.
    candidates = list(allowed)
    rng.shuffle(candidates)
    half_w = g.world_w_cm / 2.0
    half_h = g.world_h_cm / 2.0
    margin = _wall_clearance_cm(g)
    for deg in candidates:
        h = math.radians(deg)
        ux = math.sin(h); uy = math.cos(h)
        # Пробуем разные дистанции от больших к маленьким.
        for base in (200, 160, 120, 80, 60):
            dist = round(rng.uniform(0.6, 1.0) * base / 10) * 10
            if dist < 60:
                continue
            tx = round((state["x"] + ux * dist) / 10) * 10
            ty = round((state["y"] + uy * dist) / 10) * 10
            if not (-half_w + margin <= tx <= half_w - margin and
                    -half_h + margin <= ty <= half_h - margin):
                continue
            if math.hypot(tx - state["x"], ty - state["y"]) < 60:
                continue
            new = {"x": float(tx), "y": float(ty), "heading": float(deg)}
            path_seg = _interpolate_straight(state, new)
            if (prior_path is not None and min_gap_cm is not None
                    and not _path_seg_clears_prior(path_seg, prior_path, min_gap_cm)):
                continue
            return (new, f"Вега в точку {tx:g} {ty:g}",
                    f"goto_cmd({tx:g}, {ty:g})", path_seg)
    return None


# ── Общий движок генерации траектории ──────────────────────────────────────

def _generate_trajectory(n_waypoints: int, geom: WorldGeom,
                          rng: random.Random, *,
                          align_to_grid: bool = False,
                          heading_step_deg: int = 45) -> dict:
    """Сгенерировать траекторию из n_waypoints чекпоинтов чередованием
    «turn + forward/back» и «goto».

    Ограничения уровней 1-2 (применяются всегда):
      • курсы — только 8 кардиналов (0, 45, 90, 135, 180, 225, 270, 315);
      • смена курса за один поворот ≤ 90° (нет резких 135°/180°);
      • новый сегмент пути не приближается ближе min_gap_cm к ранее
        пройденным (без касания самого себя).

    align_to_grid=True (уровень 1): waypoints обязаны попадать в узлы
    сетки 50×50 см — старт снапится к ближайшему узлу, дистанции
    форвардов берутся из набора 50/100/150/200 (ортогональ) и 70/140/210
    (диагональ ≈ √2 × клетка).

    Возвращает dict с полями state, waypoints, full_path, voice_steps,
    code_steps. Безопасный лимит итераций защищает от зацикливания —
    если кандидаты не находят валидной позиции в тесном поле, вернёт
    меньше точек чем запрашивалось."""
    start_x = geom.start_x
    start_y = geom.start_y
    if align_to_grid:
        start_x = _snap_to_grid(start_x)
        start_y = _snap_to_grid(start_y)
    state = {"x": float(start_x), "y": float(start_y),
             "heading": _snap_to_step(geom.start_heading, heading_step_deg)}
    waypoints   = []
    voice_steps = []
    code_steps  = []
    # Последовательность курсов робота после каждой команды (для тестов
    # и аудита плавности). На уровне 1-2 соседние значения отличаются
    # ≤ 90°. Не сериализуется в Mission — только debug-инструмент.
    heading_steps = [state["heading"]]
    full_path   = [[round(state["x"], 1), round(state["y"], 1)]]
    # Разрежённый список вершин (только endpoints движений) — отдельно
    # от full_path. Используется для проверки зазора: дансная интерполяция
    # full_path даёт ложные «коллизии» у стыковочной точки (много мелких
    # сегментов вблизи state, каждый сэмпл новой команды попадает на их
    # хвост). Вершины — топологически чистый путь.
    vertices    = [[round(state["x"], 1), round(state["y"], 1)]]

    # Зазор: 2 × (safety + half_robot), но не меньше абсолютного минимума.
    # На дефолтной геометрии = 2 × (5 + 10) = 30 см, что совпадает с
    # _MIN_PATH_GAP_CM. На крупных роботах зазор шире — пропорционально.
    min_gap = max(_MIN_PATH_GAP_CM,
                   2.0 * (geom.safety_margin_cm
                          + max(geom.robot_w_cm, geom.robot_l_cm) / 2.0))

    def _extend_path(seg):
        if not seg: return
        if (full_path and len(seg) > 0
                and full_path[-1] == seg[0]):
            full_path.extend(seg[1:])
        else:
            full_path.extend(seg)

    def _add_vertex(point):
        v = [round(point[0], 1), round(point[1], 1)]
        if not vertices or vertices[-1] != v:
            vertices.append(v)

    movement_candidates = [_candidate_forward, _candidate_back]
    # Только face_cardinal — face_to (произвольный угол) больше не
    # используется: курсы строго кратны 45°.
    turn_candidates     = [_candidate_face_cardinal]

    cand_kw = dict(align_to_grid=align_to_grid,
                   prior_path=vertices, min_gap_cm=min_gap,
                   heading_step_deg=heading_step_deg)

    waypoints_created = 0
    # Поднял лимит: жёсткие ограничения (кардиналы + зазор + grid)
    # отбраковывают больше кандидатов, нужно больше попыток.
    safety_iter_max = max(120, n_waypoints * 50)
    safety_iter = 0
    while waypoints_created < n_waypoints and safety_iter < safety_iter_max:
        safety_iter += 1
        # 1/3 шансов на goto, 2/3 — на turn+move
        style = rng.choice(['turn_move', 'turn_move', 'goto'])

        if style == 'goto':
            result = _candidate_goto(state, geom, rng, **cand_kw)
            if result is None:
                continue
            state, voice, code, path_seg = result
            voice_steps.append(voice)
            code_steps.append(code)
            heading_steps.append(state["heading"])
            _extend_path(path_seg)
            _add_vertex((state["x"], state["y"]))
            waypoints.append([round(state["x"], 1), round(state["y"], 1)])
            waypoints_created += 1
            continue

        # turn + move — с откатом, если после поворота никуда не двинулись.
        # Иначе trajectory копит лишние face-без-forward и итоговый воркфлоу
        # выглядит «нервно».
        saved_state     = dict(state)
        saved_voice_n   = len(voice_steps)
        saved_code_n    = len(code_steps)
        saved_path_n    = len(full_path)
        saved_verts_n   = len(vertices)
        saved_heading_n = len(heading_steps)

        turn_fn = rng.choice(turn_candidates)
        result = turn_fn(state, geom, rng, **cand_kw)
        if result is None:
            continue
        state, voice, code, path_seg = result
        voice_steps.append(voice)
        code_steps.append(code)
        heading_steps.append(state["heading"])
        _extend_path(path_seg)

        moved = False
        for _ in range(8):
            mv_fn = rng.choice(movement_candidates)
            result = mv_fn(state, geom, rng, **cand_kw)
            if result is None:
                continue
            state, voice, code, path_seg = result
            voice_steps.append(voice)
            code_steps.append(code)
            heading_steps.append(state["heading"])
            _extend_path(path_seg)
            _add_vertex((state["x"], state["y"]))
            waypoints.append([round(state["x"], 1), round(state["y"], 1)])
            waypoints_created += 1
            moved = True
            break
        if not moved:
            # Откатываем поворот — он съел итерацию без пользы.
            state = saved_state
            del voice_steps[saved_voice_n:]
            del code_steps[saved_code_n:]
            del full_path[saved_path_n:]
            del vertices[saved_verts_n:]
            del heading_steps[saved_heading_n:]
            continue

    return {
        "state":         state,
        "waypoints":     waypoints,
        "full_path":     full_path,
        "vertices":      vertices,
        "voice_steps":   voice_steps,
        "code_steps":    code_steps,
        "heading_steps": heading_steps,
    }


# ── Размещение опасных зон вне траектории ──────────────────────────────────

def _dist_point_to_segment(px: float, py: float,
                           ax: float, ay: float,
                           bx: float, by: float) -> float:
    """Кратчайшее расстояние от точки P до отрезка AB."""
    dx, dy = bx - ax, by - ay
    seg_len_sq = dx * dx + dy * dy
    if seg_len_sq < 1e-9:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / seg_len_sq))
    proj_x = ax + t * dx
    proj_y = ay + t * dy
    return math.hypot(px - proj_x, py - proj_y)


def _dist_to_segment_interior(px: float, py: float,
                               ax: float, ay: float,
                               bx: float, by: float) -> float:
    """Расстояние до отрезка AB ТОЛЬКО когда подножие перпендикуляра
    выпадает СТРОГО внутри [A, B]. Иначе +∞.

    Зачем: для adjacent-prior (примыкающий к новому сегменту) точка
    стыковки = endpoint A или B, поэтому расстояние до отрезка по
    обычной формуле всегда мало (= расстояние до точки стыковки). Это
    false positive — стыковки естественны. А вот если новый сегмент
    «загибается обратно» (back с углом > 90°), подножие перпендикуляра
    падает внутрь prior, и это реальное обратное наложение — его мы и
    хотим поймать."""
    dx, dy = bx - ax, by - ay
    seg_len_sq = dx * dx + dy * dy
    if seg_len_sq < 1e-9:
        return float("inf")
    t = ((px - ax) * dx + (py - ay) * dy) / seg_len_sq
    if t <= 0.0 or t >= 1.0:
        return float("inf")
    proj_x = ax + t * dx
    proj_y = ay + t * dy
    return math.hypot(px - proj_x, py - proj_y)


def _min_dist_to_polyline(zx: float, zy: float,
                           path: list[list[float]]) -> float:
    """Минимальное расстояние от точки до ломаной (для проверки, что
    эталонная траектория не задевает опасную зону)."""
    if len(path) < 2:
        return float("inf")
    return min(
        _dist_point_to_segment(zx, zy, path[i][0], path[i][1],
                                path[i + 1][0], path[i + 1][1])
        for i in range(len(path) - 1)
    )


def _place_danger_zones(n_zones: int, full_path: list[list[float]],
                         geom: WorldGeom, rng: random.Random, *,
                         zone_radius_cm: float,
                         clearance_cm: float,
                         max_extra_cm: float = 25.0) -> list[list[float]]:
    """Расставить n_zones опасных зон ВДОЛЬ эталонной траектории.

    Стратегия: выбираем случайную точку на ломаной (равномерно по длине),
    отступаем от неё перпендикулярно к сегменту на (required + extra),
    где required = zone_radius + clearance гарантирует безопасный проход
    по эталону, а extra ∈ [0, max_extra_cm] — небольшое случайное
    смещение. Так зоны оказываются БЛИЗКО к траектории и реально
    угрожают, а не теряются где-то в углу поля.

    Зона валидна, если:
      • полностью внутри поля (центр не ближе zone_radius + wall_thick
        от стены),
      • расстояние от центра до ВСЕХ сегментов траектории ≥ required
        (важно: точка отступа близка к одному сегменту, но другие
        сегменты могут оказаться ближе — отбраковываем),
      • расстояние до старта ≥ required,
      • расстояние до уже размещённой зоны ≥ сумма радиусов + 20 см
        (визуальный разнос).

    Все радиусы зон одинаковые (zone_radius_cm). На уровнях 4-5 будет
    другая логика с переменным радиусом.

    Возвращает [[x, y, r], ...]. Если места не хватило — может вернуть
    меньше n_zones (или пустой список); вызывающий должен это учитывать."""
    if len(full_path) < 2:
        return []
    half_w = geom.world_w_cm / 2.0
    half_h = geom.world_h_cm / 2.0
    wall_pad = zone_radius_cm + geom.wall_thick_cm
    required = zone_radius_cm + clearance_cm

    # Длины сегментов — для выбора точки на пути равномерно по длине.
    seg_lens = []
    for i in range(len(full_path) - 1):
        dx = full_path[i + 1][0] - full_path[i][0]
        dy = full_path[i + 1][1] - full_path[i][1]
        seg_lens.append(math.hypot(dx, dy))
    total_len = sum(seg_lens)
    if total_len < 1.0:
        return []

    placed: list[list[float]] = []
    safety_iter = 0
    while len(placed) < n_zones and safety_iter < 400:
        safety_iter += 1
        # 1) Точка на ломаной (равномерно по длине).
        target = rng.uniform(0, total_len)
        accumulated = 0.0
        seg_idx = 0
        for i, sl in enumerate(seg_lens):
            if accumulated + sl >= target:
                seg_idx = i
                break
            accumulated += sl
        seg_len = seg_lens[seg_idx]
        if seg_len < 1.0:
            continue
        t = (target - accumulated) / seg_len
        a = full_path[seg_idx]
        b = full_path[seg_idx + 1]
        px = a[0] + (b[0] - a[0]) * t
        py = a[1] + (b[1] - a[1]) * t
        # 2) Перпендикуляр к сегменту, случайная сторона.
        dx = b[0] - a[0]
        dy = b[1] - a[1]
        L = math.hypot(dx, dy)
        nx, ny = -dy / L, dx / L
        side = 1 if rng.random() < 0.5 else -1
        offset = required + rng.uniform(0.0, max_extra_cm)
        zx = px + nx * offset * side
        zy = py + ny * offset * side
        # Округляем до 10 см — единый шаг с расстояниями и координатами
        # точек на уровне 2 (читаемое описание).
        zx = float(round(zx / 10) * 10)
        zy = float(round(zy / 10) * 10)
        # 3) Проверки на валидность.
        if not (-half_w + wall_pad <= zx <= half_w - wall_pad and
                -half_h + wall_pad <= zy <= half_h - wall_pad):
            continue
        # После округления центр мог сместиться → перепроверяем расстояние
        # ко всем сегментам, а не только к выбранному.
        if _min_dist_to_polyline(zx, zy, full_path) < required - 0.5:
            continue
        if math.hypot(zx - geom.start_x, zy - geom.start_y) < required:
            continue
        ok = True
        for (ox, oy, orad) in placed:
            if math.hypot(zx - ox, zy - oy) < zone_radius_cm + orad + 20:
                ok = False
                break
        if not ok:
            continue
        placed.append([zx, zy, float(zone_radius_cm)])
    return placed


# ── Скоринг разброса waypoints по квадрантам ───────────────────────────────

def _quadrant_spread_score(waypoints: list[list[float]],
                            start_x: float, start_y: float) -> tuple:
    """Скор «разброс по координатной плоскости» относительно старта.
    Возвращает кортеж (n_quadrants, total_radius), сортируется по нему:
    больше квадрантов охвачено — лучше; при равенстве — суммарное
    удаление от старта побеждает (компактные «слипшиеся» отвергаются)."""
    quads = set()
    radius_sum = 0.0
    for wx, wy in waypoints:
        dx = wx - start_x
        dy = wy - start_y
        # «На оси» — не считаем за отдельный квадрант, но и не теряем точку
        qx = 1 if dx > 1 else -1 if dx < -1 else 0
        qy = 1 if dy > 1 else -1 if dy < -1 else 0
        quads.add((qx, qy))
        radius_sum += math.hypot(dx, dy)
    return (len(quads), radius_sum)


def _generate_trajectory_best_of(n_waypoints: int, geom: WorldGeom,
                                   rng: random.Random, *,
                                   align_to_grid: bool,
                                   heading_step_deg: int,
                                   attempts: int = 10,
                                   ref_start_x: Optional[float] = None,
                                   ref_start_y: Optional[float] = None) -> dict:
    """Сгенерировать `attempts` траекторий и вернуть лучшую по разбросу
    waypoints по квадрантам вокруг (ref_start_x, ref_start_y). Без этого
    почти все seed'ы дают «слипшиеся в один угол» миссии."""
    sx = ref_start_x if ref_start_x is not None else geom.start_x
    sy = ref_start_y if ref_start_y is not None else geom.start_y
    best_traj = None
    best_score: tuple = (-1, -1.0)
    for _ in range(attempts):
        sub_rng = random.Random(rng.randrange(2 ** 31))
        traj = _generate_trajectory(n_waypoints, geom, sub_rng,
                                      align_to_grid=align_to_grid,
                                      heading_step_deg=heading_step_deg)
        wp = traj["waypoints"]
        if len(wp) < n_waypoints:
            # Неполная — учитываем хуже полной (но не отбрасываем целиком,
            # на случай тесного поля).
            score = (-1, len(wp))
        else:
            score = _quadrant_spread_score(wp, sx, sy)
        if score > best_score:
            best_score = score
            best_traj  = traj
    return best_traj


# ── Генератор уровня 1 ─────────────────────────────────────────────────────

def _generate_level_1(geom: WorldGeom, rng: random.Random) -> dict:
    """2-3 waypoints + поворотные команды между ними. Без зон.

    Каждая команда возвращает path_segment — список точек вдоль её
    траектории. Сегменты конкатенируются в полный путь миссии для
    отрисовки. Для будущих криволинейных команд (circle, spiral) их
    path_segment будет сэмплировать дугу, и линия в превью отразит
    реальную форму маршрута.
    """
    n_waypoints = rng.randint(2, 3)
    # Старт описания тоже снапим к сетке (если в настройках он не на узле):
    # waypoints отсчитываются от снапнутого старта, описание должно
    # совпадать с реальной геометрией миссии.
    desc_start_x = _snap_to_grid(geom.start_x)
    desc_start_y = _snap_to_grid(geom.start_y)
    traj = _generate_trajectory_best_of(
        n_waypoints, geom, rng,
        align_to_grid=True,
        heading_step_deg=_LEVEL_HEADING_STEP_DEG[1],
        ref_start_x=desc_start_x, ref_start_y=desc_start_y)
    return {
        "level":            1,
        # title оставляем пустым — пользователь введёт сам, иначе сервер
        # подставит «Миссия #N» (где N — присвоенный id).
        "title":            "",
        "description":      _format_description(
                                level=1, waypoints=traj["waypoints"], actions=[],
                                start_x=desc_start_x, start_y=desc_start_y,
                                danger_zones=[]),
        "waypoints":        json.dumps(traj["waypoints"]),
        "path":             json.dumps(traj["full_path"]),
        "danger_zones":     json.dumps([]),
        "actions_required": json.dumps([]),
        "reference_voice":  json.dumps(traj["voice_steps"]),
        "reference_code":   _format_reference_code(traj["code_steps"]),
        "safety_margin_cm": geom.safety_margin_cm,
    }


# ── Генератор уровня 2 ─────────────────────────────────────────────────────

def _generate_level_2(geom: WorldGeom, rng: random.Random) -> dict:
    """4-5 waypoints + 2 опасные зоны. Курсы кратны 15° (24 направления),
    точки не привязаны к сетке.

    Опасные зоны располагаются ВНЕ эталонной траектории: расстояние от
    центра зоны до любого её сегмента ≥ zone_radius + safety_margin +
    half_robot. Так гарантируется, что робот, идущий точно по эталону,
    не задевает зону корпусом даже при максимальном допустимом отклонении.

    Радиус зон берётся из geom.danger_zone_radius_cm (настройки пользователя)."""
    n_waypoints = rng.randint(4, 5)
    traj = _generate_trajectory_best_of(
        n_waypoints, geom, rng,
        align_to_grid=False,
        heading_step_deg=_LEVEL_HEADING_STEP_DEG[2])

    n_zones = 2
    # Допуск от траектории до зоны: половина габарита робота (корпус) +
    # safety_margin (тот же радиус, в пределах которого «отклонение
    # засчитывается»). Это симметрично с проверкой соответствия пути.
    clearance = geom.safety_margin_cm + max(geom.robot_w_cm, geom.robot_l_cm) / 2.0
    zones = _place_danger_zones(
        n_zones, traj["full_path"], geom, rng,
        zone_radius_cm=geom.danger_zone_radius_cm,
        clearance_cm=clearance,
    )

    return {
        "level":            2,
        "title":            "",
        "description":      _format_description(
                                level=2, waypoints=traj["waypoints"], actions=[],
                                start_x=geom.start_x, start_y=geom.start_y,
                                danger_zones=zones),
        "waypoints":        json.dumps(traj["waypoints"]),
        "path":             json.dumps(traj["full_path"]),
        "danger_zones":     json.dumps(zones),
        "actions_required": json.dumps([]),
        "reference_voice":  json.dumps(traj["voice_steps"]),
        "reference_code":   _format_reference_code(traj["code_steps"]),
        "safety_margin_cm": geom.safety_margin_cm,
    }


# ── Текст описания миссии ──────────────────────────────────────────────────

def _format_description(level: int, waypoints: list[list[float]],
                        actions: list[dict],
                        start_x: float = 0.0, start_y: float = 0.0,
                        danger_zones: list = None) -> str:
    """Универсальное описание миссии БЕЗ подсказок какими командами
    выполнять. Структурно, кратко. Формат — единый с кастомными:
       🟢 Начало маршрута (X, Y)
       📍 Контрольные точки маршрута (N шт.): coords
       📌 Установите зоны внимания: coords  (если есть)
       ⚠ Опасные зоны на карте (N шт.): coords  (если есть)
       ⭐ За правильно выполненное задание и прохождение траектории...
    """
    parts = []
    parts.append(f"🟢 Начало маршрута ({int(start_x)}, {int(start_y)})")
    if waypoints:
        wp_str = ", ".join(f"({int(x)}, {int(y)})" for x, y in waypoints)
        parts.append(
            f"📍 Контрольные точки маршрута ({len(waypoints)} шт.): {wp_str}.")
    place_actions = [a for a in actions if a.get("type") == "place_attention"]
    remove_actions = [a for a in actions
                      if a.get("type") in ("remove_danger", "remove_attention")]
    if place_actions:
        zs = ", ".join(f"({int(a['x'])}, {int(a['y'])})" for a in place_actions)
        parts.append(f"📌 Установите зоны внимания: {zs}.")
    if remove_actions:
        d_count = sum(1 for a in remove_actions if a["type"] == "remove_danger")
        a_count = sum(1 for a in remove_actions if a["type"] == "remove_attention")
        if d_count:
            parts.append(f"❌ Удалите все опасные зоны ({d_count} шт).")
        if a_count:
            parts.append(f"❌ Удалите зоны внимания ({a_count} шт).")
    if danger_zones:
        zs = ", ".join(f"({int(z[0])}, {int(z[1])})" for z in danger_zones)
        parts.append(
            f"⚠ Опасные зоны на карте ({len(danger_zones)} шт.): {zs}. "
            f"Не задевайте.")
    parts.append(
        "⭐ За правильно выполненное задание и прохождение траектории "
        "вы получите звёзды.")
    return "\n".join(parts)


def _format_reference_code(code_steps: list[str]) -> str:
    """Эталонный outline миссии — последовательность шагов в виде
    псевдокода. Назначение — admin-only подсказка «как пройти».

    Имена `forward_cmd(N)` / `face_n_cmd()` здесь условны (не реальные
    helper-функции из API 1T REX). Для запуска на роботе нужны вызовы
    через codegen из голосовых фраз reference_voice. На этапе UI-
    подсказки будет добавлена кнопка «загрузить как Python-код», которая
    прогонит reference_voice через _build_cmd → _python_call_lines_for_cmd
    и получит реально исполнимый код."""
    if not code_steps:
        return "# (нет действий)\n"
    lines = [
        "# Эталонный outline миссии (admin-only подсказка).",
        "# Для исполнимого кода — используйте reference_voice через ▶ Запуск.",
        "",
    ]
    lines.extend(code_steps)
    return "\n".join(lines) + "\n"


# ── Публичный API ──────────────────────────────────────────────────────────

def generate_mission(level: int = 1,
                     geom: Optional[WorldGeom] = None,
                     seed: Optional[int] = None) -> dict:
    """Сгенерировать миссию указанного уровня.

    Возвращает dict с полями для Mission(...). На уровнях > 1 пока
    не реализовано — для них тоже вернёт level-1 миссию (заглушка).
    """
    if geom is None:
        geom = WorldGeom()
    rng = random.Random(seed)
    if level == 1:
        return _generate_level_1(geom, rng)
    if level == 2:
        return _generate_level_2(geom, rng)
    # Заглушка для пока-не-реализованных уровней
    result = _generate_level_1(geom, rng)
    result["level"] = level
    result["title"] = f"Миссия — уровень {level} (в разработке)"
    return result
