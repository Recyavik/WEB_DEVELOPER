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


def _wall_clearance_cm(g: WorldGeom) -> float:
    """Минимальное допустимое расстояние от центра робота до стены.
    По ТЗ — «не ближе 2× габариты робота». Берём максимум из ширины/длины."""
    return 2.0 * max(g.robot_w_cm, g.robot_l_cm)


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


# ── Кандидаты команд (для уровня 1) ────────────────────────────────────────

# Каждый кандидат: функция(state, geom, rng) -> (new_state, voice, code) | None
# None означает «команду нельзя использовать в этом состоянии» (например,
# не хватает места впереди для forward 100см).

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


def _candidate_forward(state: dict, g: WorldGeom, rng: random.Random):
    """Forward-дистанция — пропорционально размеру поля. Возвращает
    также path_segment (сэмпл траектории) для визуализации."""
    half_min = min(g.world_w_cm, g.world_h_cm) / 2.0
    base = max(80.0, half_min - _wall_clearance_cm(g))
    for shrink in (1.0, 0.7, 0.5, 0.35):
        dist = round(rng.uniform(0.35, 0.85) * base * shrink / 10) * 10
        if dist < 30:
            continue
        new = _step_forward(state, dist)
        if _inside_field(new["x"], new["y"], g):
            path_seg = _interpolate_straight(state, new)
            return (new, f"Вега вперед {int(dist)} см",
                    f"forward_cmd({int(dist)})", path_seg)
    return None


def _candidate_back(state: dict, g: WorldGeom, rng: random.Random):
    """Back-дистанция короче forward — не «зеркалит» предыдущий forward."""
    half_min = min(g.world_w_cm, g.world_h_cm) / 2.0
    base = max(60.0, half_min - _wall_clearance_cm(g))
    for shrink in (1.0, 0.6, 0.4):
        dist = round(rng.uniform(0.2, 0.45) * base * shrink / 10) * 10
        if dist < 20:
            continue
        new = _step_back(state, dist)
        if _inside_field(new["x"], new["y"], g):
            path_seg = _interpolate_straight(state, new)
            return (new, f"Вега назад {int(dist)} см",
                    f"back_cmd({int(dist)})", path_seg)
    return None


def _candidate_face_cardinal(state: dict, g: WorldGeom, rng: random.Random):
    """Поворот на месте к стороне света. Позиция не меняется —
    path_segment = только одна точка (текущая)."""
    cardinals = [
        (0,   "север",        "face_n_cmd()"),
        (45,  "северо-восток","face_ne_cmd()"),
        (90,  "восток",       "face_e_cmd()"),
        (135, "юго-восток",   "face_se_cmd()"),
        (180, "юг",           "face_s_cmd()"),
        (225, "юго-запад",    "face_sw_cmd()"),
        (270, "запад",        "face_w_cmd()"),
        (315, "северо-запад", "face_nw_cmd()"),
    ]
    deg, ru, code = rng.choice(cardinals)
    new = _step_face(state, deg)
    path_seg = [[round(state["x"], 1), round(state["y"], 1)]]
    return new, f"Вега {ru}", code, path_seg


def _candidate_face_to(state: dict, g: WorldGeom, rng: random.Random):
    """Произвольный угол face_cmd(N). Тоже не двигает позицию."""
    deg = rng.randint(0, 359)
    while deg % 45 == 0:
        deg = rng.randint(0, 359)
    new = _step_face(state, deg)
    path_seg = [[round(state["x"], 1), round(state["y"], 1)]]
    return new, f"Вега поверни на {deg}", f"face_cmd({deg})", path_seg


def _candidate_goto(state: dict, g: WorldGeom, rng: random.Random):
    """«Вега в точку X Y» — робот сам поворачивает и едет к цели.
    Целевая точка случайная в безопасной зоне поля, не ближе 60см
    от текущей позиции (иначе goto уйдёт в tolerance и не запишется
    в код). Координаты округляются до 10см для читаемости."""
    half_w = g.world_w_cm / 2.0
    half_h = g.world_h_cm / 2.0
    margin = _wall_clearance_cm(g)
    for _ in range(20):
        tx = round(rng.uniform(-half_w + margin, half_w - margin) / 10) * 10
        ty = round(rng.uniform(-half_h + margin, half_h - margin) / 10) * 10
        if math.hypot(tx - state["x"], ty - state["y"]) < 60:
            continue
        # Курс после goto — направление на цель.
        new = {
            "x": tx, "y": ty,
            "heading": math.degrees(math.atan2(tx - state["x"],
                                               ty - state["y"])) % 360.0,
        }
        path_seg = _interpolate_straight(state, new)
        return (new, f"Вега в точку {tx:g} {ty:g}",
                f"goto_cmd({tx:g}, {ty:g})", path_seg)
    return None


# ── Генератор уровня 1 ─────────────────────────────────────────────────────

def _generate_level_1(geom: WorldGeom, rng: random.Random) -> dict:
    """2-3 waypoints + поворотные команды между ними. Без зон.

    Алгоритм: чередуем «развернись (face)» и «forward» — это даёт
    предсказуемую траекторию из прямых сегментов с поворотами на месте,
    каждый forward даёт новую waypoint.

    Каждая команда возвращает path_segment — список точек вдоль её
    траектории. Сегменты конкатенируются в полный путь миссии для
    отрисовки. Для будущих криволинейных команд (circle, spiral) их
    path_segment будет сэмплировать дугу, и линия в превью отразит
    реальную форму маршрута.
    """
    n_waypoints   = rng.randint(2, 3)
    state         = {"x": geom.start_x, "y": geom.start_y, "heading": geom.start_heading}
    waypoints     = []
    voice_steps   = []
    code_steps    = []
    full_path     = [[round(state["x"], 1), round(state["y"], 1)]]

    def _extend_path(seg):
        """Добавить сегмент в полный путь, избегая дубля стыковочной точки."""
        if not seg: return
        if (full_path and len(seg) > 0
                and full_path[-1] == seg[0]):
            full_path.extend(seg[1:])
        else:
            full_path.extend(seg)

    # Команды-кандидаты двух типов:
    # 1) «turn + move» — отдельный поворот, потом forward/back
    # 2) «goto» — самодостаточная команда (включает поворот к цели)
    # Случайный выбор стиля каждую итерацию обеспечивает разнообразие.
    movement_candidates = [_candidate_forward, _candidate_back]
    turn_candidates     = [_candidate_face_cardinal, _candidate_face_to]

    waypoints_created = 0
    safety_iter = 0
    while waypoints_created < n_waypoints and safety_iter < 50:
        safety_iter += 1
        # 1/3 шансов на goto, 2/3 — на turn+move
        style = rng.choice(['turn_move', 'turn_move', 'goto'])

        if style == 'goto':
            result = _candidate_goto(state, geom, rng)
            if result is None:
                continue
            state, voice, code, path_seg = result
            voice_steps.append(voice)
            code_steps.append(code)
            _extend_path(path_seg)
            waypoints.append([round(state["x"], 1), round(state["y"], 1)])
            waypoints_created += 1
            continue

        # turn + move
        turn_fn = rng.choice(turn_candidates)
        result = turn_fn(state, geom, rng)
        if result is None:
            continue
        state, voice, code, path_seg = result
        voice_steps.append(voice)
        code_steps.append(code)
        _extend_path(path_seg)

        moved = False
        for _ in range(8):
            mv_fn = rng.choice(movement_candidates)
            result = mv_fn(state, geom, rng)
            if result is None:
                continue
            state, voice, code, path_seg = result
            voice_steps.append(voice)
            code_steps.append(code)
            _extend_path(path_seg)
            waypoints.append([round(state["x"], 1), round(state["y"], 1)])
            waypoints_created += 1
            moved = True
            break
        if not moved:
            continue

    # full_path содержит реальную траекторию по сегментам команд.
    # Для прямых движений — это просто старт-конец, для будущих
    # криволинейных команд (circle, spiral) — будут промежуточные точки.
    return {
        "level":            1,
        # title оставляем пустым — пользователь введёт сам, иначе сервер
        # подставит «Миссия #N» (где N — присвоенный id).
        "title":            "",
        "description":      _format_description(
                                level=1, waypoints=waypoints, actions=[],
                                start_x=geom.start_x, start_y=geom.start_y,
                                danger_zones=[]),
        "waypoints":        json.dumps(waypoints),
        "path":             json.dumps(full_path),
        "danger_zones":     json.dumps([]),
        "actions_required": json.dumps([]),
        "reference_voice":  json.dumps(voice_steps),
        "reference_code":   _format_reference_code(code_steps),
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
    # Заглушка для пока-не-реализованных уровней
    result = _generate_level_1(geom, rng)
    result["level"] = level
    result["title"] = f"Миссия — уровень {level} (в разработке)"
    return result
