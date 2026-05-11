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

def _candidate_forward(state: dict, g: WorldGeom, rng: random.Random):
    dist = rng.choice([50, 80, 100, 120, 150])
    new = _step_forward(state, dist)
    if not _inside_field(new["x"], new["y"], g):
        return None
    return new, f"Вега вперед {dist} см", f"forward_cmd({dist})"


def _candidate_back(state: dict, g: WorldGeom, rng: random.Random):
    dist = rng.choice([40, 60, 80])
    new = _step_back(state, dist)
    if not _inside_field(new["x"], new["y"], g):
        return None
    return new, f"Вега назад {dist} см", f"back_cmd({dist})"


def _candidate_face_cardinal(state: dict, g: WorldGeom, rng: random.Random):
    """Стороны света — поворот на месте. Позиция не меняется, новой
    точки не возникает, поэтому это «вспомогательная» команда: её
    результат — изменение курса, чтобы следующий forward пошёл туда.
    В trajectory её НЕ включаем как waypoint, но в reference_voice/code —
    да, чтобы решение было воспроизводимым."""
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
    return new, f"Вега {ru}", code


def _candidate_face_to(state: dict, g: WorldGeom, rng: random.Random):
    """Произвольный угол face_cmd(N). Тоже не двигает позицию."""
    deg = rng.randint(0, 359)
    # Избегаем кардинальных углов, чтобы не дублировать face_X
    while deg % 45 == 0:
        deg = rng.randint(0, 359)
    new = _step_face(state, deg)
    return new, f"Вега поверни на {deg}", f"face_cmd({deg})"


# ── Генератор уровня 1 ─────────────────────────────────────────────────────

def _generate_level_1(geom: WorldGeom, rng: random.Random) -> dict:
    """2-3 waypoints + поворотные команды между ними. Без зон.

    Алгоритм: чередуем «развернись (face)» и «forward» — это даёт
    предсказуемую траекторию из прямых сегментов с поворотами на месте,
    каждый forward даёт новую waypoint.
    """
    n_waypoints   = rng.randint(2, 3)
    state         = {"x": geom.start_x, "y": geom.start_y, "heading": geom.start_heading}
    waypoints     = []
    voice_steps   = []
    code_steps    = []

    movement_candidates = [_candidate_forward, _candidate_back]
    turn_candidates     = [_candidate_face_cardinal, _candidate_face_to]

    waypoints_created = 0
    safety_iter = 0
    while waypoints_created < n_waypoints and safety_iter < 50:
        safety_iter += 1
        # Сначала — поворот (новый курс)
        turn_fn = rng.choice(turn_candidates)
        result = turn_fn(state, geom, rng)
        if result is None:
            continue
        state, voice, code = result
        voice_steps.append(voice)
        code_steps.append(code)

        # Потом — движение в новом курсе. Пробуем несколько раз пока
        # не получится попадание внутрь поля.
        moved = False
        for _ in range(8):
            mv_fn = rng.choice(movement_candidates)
            result = mv_fn(state, geom, rng)
            if result is None:
                continue
            state, voice, code = result
            voice_steps.append(voice)
            code_steps.append(code)
            waypoints.append([round(state["x"], 1), round(state["y"], 1)])
            waypoints_created += 1
            moved = True
            break
        if not moved:
            # Курс ведёт в стену — попробуем другой поворот
            continue

    return {
        "level":            1,
        "title":            "Миссия — ознакомительный уровень",
        "description":      _format_description(level=1, waypoints=waypoints,
                                                actions=[]),
        "waypoints":        json.dumps(waypoints),
        "danger_zones":     json.dumps([]),
        "actions_required": json.dumps([]),
        "reference_voice":  json.dumps(voice_steps),
        "reference_code":   _format_reference_code(code_steps),
        "safety_margin_cm": geom.safety_margin_cm,
    }


# ── Текст описания миссии ──────────────────────────────────────────────────

def _format_description(level: int, waypoints: list[list[float]],
                        actions: list[dict]) -> str:
    """Универсальное описание миссии БЕЗ подсказок какими командами
    выполнять. Структурно, кратко."""
    level_names = {
        1: "Ознакомительный",
        2: "Начальный",
        3: "Базовый",
        4: "Углублённый",
        5: "Продвинутый",
    }
    parts = []
    parts.append(f"Уровень: {level_names.get(level, level)}.")
    if waypoints:
        wp_str = ", ".join(
            f"({int(x)}, {int(y)})" for x, y in waypoints
        )
        parts.append(f"📍 Посетите контрольные точки: {wp_str}.")
    place_actions  = [a for a in actions if a.get("type") == "place_attention"]
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
    parts.append("⭐ За правильно выполненное задание вы получите звёзды.")
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
