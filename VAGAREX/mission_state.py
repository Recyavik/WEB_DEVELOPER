"""mission_state.py — runtime-состояние активной миссии в UserSession.

Изолирует трекинг прохождения миссии (посещение точек, выполнение
действий с зонами, отклонения от траектории, аккуратность прохождения)
от основной логики UserSession. Чистая структура данных + утилиты,
без внешних зависимостей.

Используется в session.py как `self._mission: Optional[ActiveMission]`.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


# ── Геометрия: расстояние от точки до отрезка ──────────────────────────────

def _dist_point_to_segment(px: float, py: float,
                           ax: float, ay: float,
                           bx: float, by: float) -> float:
    """Кратчайшее расстояние от точки P до отрезка AB."""
    dx, dy = bx - ax, by - ay
    seg_len_sq = dx * dx + dy * dy
    if seg_len_sq < 1e-9:
        # Вырожденный отрезок — расстояние до точки A.
        return math.hypot(px - ax, py - ay)
    # Параметр t проекции P на прямую AB, ограниченный [0, 1].
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / seg_len_sq))
    proj_x = ax + t * dx
    proj_y = ay + t * dy
    return math.hypot(px - proj_x, py - proj_y)


def dist_to_path(x: float, y: float,
                 path: list[tuple[float, float]]) -> float:
    """Минимальное расстояние от (x, y) до ломаной из waypoints.
    Стартовая точка считается частью path (вызывающий должен её
    добавить, если нужно)."""
    if len(path) < 2:
        return float("inf")
    return min(
        _dist_point_to_segment(x, y, *path[i], *path[i + 1])
        for i in range(len(path) - 1)
    )


def path_total_length(path: list[tuple[float, float]]) -> float:
    if len(path) < 2:
        return 0.0
    return sum(
        math.hypot(path[i + 1][0] - path[i][0], path[i + 1][1] - path[i][1])
        for i in range(len(path) - 1)
    )


# ── Ключевые точки из отрезков манёвров ────────────────────────────────────

def keypoints_from_segments(segments: list,
                            merge_cm: float = 3.0) -> list[tuple[float, float]]:
    """Из отрезков движущихся манёвров [((x0,y0),(x1,y1)), …] собирает
    цепочку «ключевых» точек: старт первого манёвра, концы манёвров,
    а также начала манёвров, если разворот между ними сдвинул робота.
    Соседние совпадающие точки (ближе merge_cm) склеиваются.

    Развороты в segments не попадают (их не пишет RobotProxy._run_segment) —
    точки разворотов в ключевые не идут, как и задумано: развернуться
    можно по-разному."""
    pts: list[tuple[float, float]] = []

    def push(x: float, y: float) -> None:
        p = (round(float(x), 1), round(float(y), 1))
        if not pts or math.hypot(p[0] - pts[-1][0], p[1] - pts[-1][1]) > merge_cm:
            pts.append(p)

    for seg in segments:
        (sx, sy), (ex, ey) = seg
        push(sx, sy)
        push(ex, ey)
    return pts


# ── ActiveMission ──────────────────────────────────────────────────────────

# Допуск попадания в контрольную точку (см). Жёсткий и небольшой: точку
# нужно посетить ТОЧНО, а не «проехать рядом». 5 см совпадает с точностью
# захода автопилота (TOL_CM) и с типовым «Запасом безопасности» миссии.
# Это НЕ то же, что track_tolerance_cm (габарит робота, ~20 см) — тот шире
# и отвечает только за коридор следования траектории, не за зачёт точек.
# Проверяется не позиция (её робот может «проскочить» между двумя кадрами
# на быстрой скорости), а ОТРЕЗОК движения за тик: точка засчитана, если
# её расстояние до отрезка (prev_pos → curr_pos) ≤ этого допуска.
WAYPOINT_TOLERANCE_CM = 5.0

# Радиус матчинга действия с зоной (для зон-действий).
ACTION_TOLERANCE_CM = 15.0

# Скорость убывания «Аккуратности» за один tick (push_state), пока робот
# вне коридора траектории. ~10 push_state/с × 0.005 = 0.05/с → 10 секунд
# отклонения стоят −0.5 аккуратности.
QUALITY_DRAIN_PER_TICK = 0.005

# Штраф к «Аккуратности» за каждую запрошенную подсказку. Считается
# отдельным счётчиком hints_used и вычитается в effective_quality() —
# переживает перезапуск программы (▶).
HINT_PENALTY = 0.20

# Штраф к «Аккуратности» за каждый наезд на зону (опасную или свою зону
# внимания). Копится в zone_penalty, вычитается в effective_quality().
ZONE_HIT_PENALTY = 0.20

# Зона внимания установлена с неверным радиусом: если радиус отличается
# от требуемого больше чем на RADIUS_TOLERANCE_CM — мягкий штраф
# RADIUS_PENALTY (действие при этом засчитывается, важна позиция).
RADIUS_TOLERANCE_CM = 5.0
RADIUS_PENALTY      = 0.05


@dataclass
class ActiveMission:
    """Состояние одного прохождения миссии. Создаётся при start_mission,
    обновляется на каждый push_state, завершается при stop_mission либо
    при выполнении всех целей."""
    mission_id:       int
    run_id:           Optional[int]            # MissionRun.id (выставится при сохранении)
    user_id:          int
    waypoints:        list[tuple[float, float]]
    danger_zones:     list[tuple[float, float, float]]   # [(x,y,r), ...]
    actions_required: list[dict]               # см. mission_generator
    safety_margin_cm: float
    start_x:          float
    start_y:          float
    # Полная траектория для визуализации (плотный сэмпл, включая криволинейные
    # участки у кастомных миссий). [] = клиент использует ломаную start→waypoints.
    path:             list[tuple[float, float]] = field(default_factory=list)
    title:            str = ""
    description:      str = ""
    level:            int = 1
    # Допуск точности маршрута, см = габарит робота max(длина, ширина).
    # Дальше этого от линии траектории — отклонение, точность падает.
    track_tolerance_cm: float = 20.0

    # Динамическое состояние трекинга
    waypoints_visited: set[int] = field(default_factory=set)
    actions_done:      set[int] = field(default_factory=set)
    # «Аккуратность прохождения» 0..1: стартует с 0, растёт за посещённые
    # точки и выполненные действия, убывает за отклонение от траектории
    # и наезды на опасные зоны. Зажато в [0, 1]. Идёт в звёзды-бонус.
    quality:           float    = 0.0
    # Накопленный штраф за наезды на зоны (опасные + свои зоны внимания).
    # Каждый наезд → +ZONE_HIT_PENALTY. Хранится ОТДЕЛЬНО от quality, потому что
    # quality — растущий аккумулятор: наезд в начале прогона (когда
    # quality ещё ~0) при прямом вычитании «съелся» бы нижней границей 0
    # и штраф пропал. effective_quality() вычитает zone_penalty поверх.
    zone_penalty:      float    = 0.0
    # Накопленный штраф за выход из коридора эталонной траектории
    # (QUALITY_DRAIN_PER_TICK за каждый тик вне коридора). Хранится
    # ОТДЕЛЬНО от quality по той же причине, что и zone_penalty: дрейф
    # в начале прогона (quality ещё ~0) не должен «съедаться» границей 0.
    track_penalty:     float    = 0.0
    deviations:        int      = 0
    # Финиш достигнут — посещены ВСЕ контрольные точки. После финиша
    # точность траектории больше не проверяется (track_penalty не растёт),
    # а снятие опасных зон не штрафуется. Ставится в mark_waypoint_visits.
    finish_reached:    bool     = False
    # Сколько подсказок запросил игрок за всю миссию. НЕ сбрасывается
    # прогоном ▶ — штраф за подсказку должен пережить перезапуск кода.
    hints_used:        int      = 0
    last_in_margin:    bool     = True
    last_robot_pos:    Optional[tuple[float, float]] = None
    started_at:        datetime = field(default_factory=datetime.utcnow)
    last_algo_duration_sec: Optional[float] = None
    # Учёт наездов на опасные зоны:
    #   inside       — индексы зон, которых робот уже касался
    #   finalized    — зона уже учтена (повторные въезды бесплатны)
    #   penalized    — за зону начислен −20% (можно вернуть через forgive)
    #   forgiven     — игрок выполнил действие внутри зоны → въезд не
    #                  штрафуется (или штраф возвращается)
    # Логика: −20% начисляется СРАЗУ при ВЪЕЗДЕ в зону, один раз за зону.
    # Если игрок выполнил действие (remove_danger/place_attention) внутри
    # зоны — forgive_current_zone_hits возвращает штраф.
    danger_zones_inside:    set[int] = field(default_factory=set)
    danger_zones_forgiven:  set[int] = field(default_factory=set)
    danger_zones_finalized: set[int] = field(default_factory=set)
    danger_zones_penalized: set[int] = field(default_factory=set)
    # Индексы danger_zones, которые являются задачей «удалить зону»
    # (есть парный remove_danger action). Наезд на них НЕ штрафуется —
    # заехать внутрь нужно по заданию. Штрафуются только нетронутые
    # зоны-препятствия. Заполняется в __post_init__.
    removable_zone_idx: set[int] = field(default_factory=set)
    # Вклад одной проверочной позиции (точка / действие с зоной) в
    # «Аккуратность» = 1 / (точки + действия). Заполняется в __post_init__.
    quality_step: float = 0.0
    # Установленные игроком зоны внимания. Каждая: {x, y, r, armed,
    # inside, finalized}. Установка и нахождение внутри при установке —
    # без штрафа; первый выезд «взводит» зону (armed), дальше она ведёт
    # себя как опасная: повторный заезд+выезд → −20% (один раз).
    placed_attention: list[dict] = field(default_factory=list)

    def __post_init__(self):
        """Вычисляет производные поля:
          • removable_zone_idx — danger-зоны с парным remove_danger
            (наезд на них не штрафуется — заехать нужно по заданию);
          • quality_step — вклад одной проверочной позиции в «Аккуратность»."""
        for i, (zx, zy, zr) in enumerate(self.danger_zones):
            for a in self.actions_required:
                if a.get("type") != "remove_danger":
                    continue
                ax, ay = float(a.get("x", 0)), float(a.get("y", 0))
                if math.hypot(zx - ax, zy - ay) <= max(float(zr), 5.0):
                    self.removable_zone_idx.add(i)
                    break
        n_positions = len(self.waypoints) + len(self.actions_required)
        self.quality_step = (1.0 / n_positions) if n_positions else 0.0

    # ── Трекинг отклонения и обновление аккуратности ─────────────────────────

    def update_quality(self, robot_x: float, robot_y: float) -> bool:
        """Обновить «Аккуратность прохождения» по текущей позиции робота.

        «Аккуратность» РАСТЁТ за посещённые точки и выполненные действия
        (mark_waypoint_visits / try_match_action). Штрафы копятся
        ОТДЕЛЬНЫМИ аккумуляторами и вычитаются в effective_quality():
          • робот вне коридора эталонной траектории `path` → track_penalty
            растёт на QUALITY_DRAIN_PER_TICK за тик; ширина коридора —
            `track_tolerance_cm`;
          • въезд в опасную зону → zone_penalty +20% (_mark_danger_zone_hits);
          • повторный въезд в свою зону внимания → +20%
            (_mark_placed_attention_hits).

        Если у миссии НЕТ эталонной траектории (`path` короче 2 точек —
        напр. уровень 1 «посети точки любым путём»), точность траектории
        НЕ проверяется: аккуратность складывается только из посещённых точек
        и наездов на зоны.
        Возвращает True при переходе «в коридор / вне»."""
        self._mark_danger_zone_hits(robot_x, robot_y)
        self._mark_placed_attention_hits(robot_x, robot_y)

        # Нет эталонной траектории — отклонение не штрафуем.
        if len(self.path) < 2:
            return False

        # После финиша точность траектории не проверяется — игрок может
        # свободно съехать с эталона (напр. чтобы доснять опасные зоны).
        if self.finish_reached:
            return False

        d = dist_to_path(robot_x, robot_y, self.path)
        in_margin = d <= self.track_tolerance_cm
        if not in_margin:
            # Штраф копим отдельно: прямое вычитание из quality (растущего
            # с 0) «съело» бы дрейф в начале прогона нижней границей 0.
            self.track_penalty += QUALITY_DRAIN_PER_TICK

        transitioned = (in_margin != self.last_in_margin)
        if transitioned and not in_margin:
            self.deviations += 1
        self.last_in_margin = in_margin
        return transitioned

    def _mark_danger_zone_hits(self, robot_x: float, robot_y: float) -> int:
        """Штраф за наезд на опасную зону начисляется СРАЗУ при ВЪЕЗДЕ:
          • Робот впервые коснулся зоны i → −20% немедленно, зона
            finalized (повторные въезды бесплатны — «один наезд = один
            минус»).
          • Если зона уже в forgiven (игрок выполнил действие внутри —
            см. forgive_current_zone_hits) → въезд без штрафа.

        Зоны-задачи «удалить» (removable_zone_idx) НЕ штрафуются вовсе —
        заехать в них нужно по заданию.

        Возвращает число штрафных наездов в этом тике."""
        if not self.danger_zones:
            return 0
        new_hits = 0
        for i, (zx, zy, zr) in enumerate(self.danger_zones):
            if i in self.danger_zones_finalized:
                continue
            if i in self.removable_zone_idx:
                continue   # зона-задача «удалить» — наезд не штрафуется
            if math.hypot(robot_x - zx, robot_y - zy) > zr:
                continue   # робот ещё не коснулся зоны
            # Въезд в зону — штраф сразу, зона закрыта.
            self.danger_zones_inside.add(i)
            self.danger_zones_finalized.add(i)
            if i in self.danger_zones_forgiven:
                self.danger_zones_forgiven.discard(i)
            else:
                self.zone_penalty += ZONE_HIT_PENALTY
                self.danger_zones_penalized.add(i)
                new_hits += 1
        return new_hits

    def _mark_placed_attention_hits(self, robot_x: float, robot_y: float) -> int:
        """Зоны внимания, установленные самим игроком (place_attention).

        В момент установки робот стоит внутри зоны — это НЕ штраф.
        Первый выезд из зоны «взводит» её (armed) — тоже без штрафа.
        Дальше зона ведёт себя как опасная: повторный ВЪЕЗД в неё →
        −20% аккуратности сразу (один раз, затем finalized).

        Возвращает число штрафных наездов в этом тике."""
        if not self.placed_attention:
            return 0
        new_hits = 0
        for z in self.placed_attention:
            if z["finalized"]:
                continue
            inside_now = math.hypot(robot_x - z["x"], robot_y - z["y"]) <= z["r"]
            if inside_now and not z["inside"]:
                z["inside"] = True
                if z["armed"]:
                    # Повторный въезд после взвода — штраф сразу.
                    self.zone_penalty += ZONE_HIT_PENALTY
                    z["finalized"] = True
                    new_hits += 1
            elif z["inside"] and not inside_now:
                z["inside"] = False
                z["armed"] = True   # выезд — взвели зону, без штрафа
        return new_hits

    def finalize_remaining_zones(self) -> int:
        """Раньше закрывала зоны, в которых робот «застрял» на финише
        (exit-only учёт). Теперь штраф начисляется при ВЪЕЗДЕ в зону, так
        что к финалу все задетые зоны уже учтены. Метод оставлен no-op'ом
        для совместимости с вызовом из stop_mission."""
        return 0

    def reset_for_new_run(self) -> None:
        """Сброс динамики трекинга перед новым прогоном (▶ Проверка кода).

        Оценивается только ПОСЛЕДНЕЕ исполнение — иначе старый успех даёт
        звёзды даже после порчи кода. Учёт зон тоже обнуляется: финализация
        прошлого прогона не должна «помнить» зоны в этом. Накопленное
        время алгоритма (last_algo_duration_sec) НЕ сбрасывается."""
        self.waypoints_visited.clear()
        self.actions_done.clear()
        self.quality        = 0.0
        self.zone_penalty   = 0.0
        self.track_penalty  = 0.0
        self.deviations     = 0
        self.finish_reached = False
        self.last_in_margin = True
        self.last_robot_pos = None
        self.danger_zones_inside.clear()
        self.danger_zones_forgiven.clear()
        self.danger_zones_finalized.clear()
        self.danger_zones_penalized.clear()
        self.placed_attention.clear()

    def forgive_current_zone_hits(self, robot_x: float, robot_y: float) -> None:
        """Игрок выполнил действие (remove_danger / place_attention)
        внутри опасной зоны → наезд на эту зону прощается. Вызывается
        ровно в момент выполнения действия.

        Два случая:
          • штраф за въезд уже начислен → возвращаем его (refund);
          • робот ещё не въехал (действие сработало раньше тика
            _mark_danger_zone_hits) → помечаем зону forgiven, чтобы
            будущий въезд не штрафовался."""
        for i, (zx, zy, zr) in enumerate(self.danger_zones):
            if math.hypot(robot_x - zx, robot_y - zy) > zr:
                continue
            if i in self.danger_zones_penalized:
                # Штраф уже был начислен при въезде — возвращаем.
                self.zone_penalty = max(0.0,
                                        self.zone_penalty - ZONE_HIT_PENALTY)
                self.danger_zones_penalized.discard(i)
            # И на будущее: если въезд ещё впереди — не штрафовать.
            self.danger_zones_forgiven.add(i)

    # ── Чекпоинты и действия ─────────────────────────────────────────────

    def mark_waypoint_visits(self, robot_x: float, robot_y: float) -> list[int]:
        """Отметить waypoints, через которые робот ПРОЕХАЛ.

        Допуск = `WAYPOINT_TOLERANCE_CM` (5 см) — жёсткий: точку нужно
        посетить ТОЧНО. Это НЕ `track_tolerance_cm` (габарит робота,
        ~20 см) — тот отвечает только за коридор следования траектории.

        Проверяется не точка (текущая позиция), а ОТРЕЗОК движения за
        один тик: точка засчитана, если расстояние от неё до отрезка
        (prev_pos → curr_pos) ≤ допуска. Это устраняет «проскок» через
        точку на быстрой скорости, когда между двумя push_state робот
        может уйти на 5-10 см и при точечной проверке промахнуться.

        На первом кадре отрезка ещё нет — fallback на точку."""
        tolerance = WAYPOINT_TOLERANCE_CM
        prev = self.last_robot_pos
        self.last_robot_pos = (robot_x, robot_y)
        new = []
        for i, (wx, wy) in enumerate(self.waypoints):
            if i in self.waypoints_visited:
                continue
            if prev is None:
                d = math.hypot(robot_x - wx, robot_y - wy)
            else:
                d = _dist_point_to_segment(wx, wy, prev[0], prev[1],
                                            robot_x, robot_y)
            if d <= tolerance:
                self.waypoints_visited.add(i)
                new.append(i)
        # Каждая новая посещённая точка поднимает «Аккуратность».
        if new:
            self.quality = min(1.0, self.quality
                               + self.quality_step * len(new))
            # Финиш = посещены ВСЕ контрольные точки.
            if len(self.waypoints_visited) == len(self.waypoints):
                self.finish_reached = True
        return new

    def try_match_action(self, action_type: str,
                         x: float, y: float,
                         radius: Optional[float] = None) -> Optional[int]:
        """Попытаться сматчить выполненное пользователем действие
        с обязательным action из списка. Возвращает индекс матча или None.

        Используется из диспетчера: когда юзер ставит/удаляет зону,
        вызываем try_match_action("place_attention", x, y, radius) — если
        в actions_required есть такая (в радиусе ACTION_TOLERANCE_CM от
        указанной координаты), помечаем выполненной.

        Для place_attention `radius` — фактический радиус поставленной
        зоны. Если он отличается от требуемого больше чем на
        RADIUS_TOLERANCE_CM — мягкий штраф RADIUS_PENALTY (действие всё
        равно засчитывается: важна позиция, радиус — точность исполнения)."""
        for i, action in enumerate(self.actions_required):
            if i in self.actions_done:
                continue
            if action.get("type") != action_type:
                continue
            ax = float(action.get("x", 0))
            ay = float(action.get("y", 0))
            if math.hypot(x - ax, y - ay) <= ACTION_TOLERANCE_CM:
                self.actions_done.add(i)
                # Выполненное действие с зоной поднимает «Аккуратность».
                self.quality = min(1.0, self.quality + self.quality_step)
                if action_type == "place_attention":
                    req_r = float(action.get("radius",
                                             action.get("r", 15.0)))
                    actual_r = float(radius) if radius is not None else req_r
                    # Неверный радиус — мягкий штраф −5%.
                    if abs(actual_r - req_r) > RADIUS_TOLERANCE_CM:
                        self.zone_penalty += RADIUS_PENALTY
                    # Робот сейчас стоит внутри только что поставленной
                    # зоны — учитываем её как «свою» (с фактическим
                    # радиусом). Первый выезд взведёт, повторный въезд → −20%.
                    self.placed_attention.append({
                        "x": ax, "y": ay, "r": actual_r,
                        "armed": False, "inside": True, "finalized": False,
                    })
                elif action_type == "remove_danger":
                    # На миссиях с эталонной траекторией (L5) опасные зоны
                    # снимают ПОСЛЕ финиша. Снятие до финиша засчитывается,
                    # но штрафует аккуратность на ZONE_HIT_PENALTY (−20%).
                    if len(self.path) >= 2 and not self.finish_reached:
                        self.zone_penalty += ZONE_HIT_PENALTY
                return i
        return None

    # ── Завершение ───────────────────────────────────────────────────────

    def is_complete(self) -> bool:
        """Все обязательные цели достигнуты?"""
        all_waypoints = (len(self.waypoints_visited) == len(self.waypoints))
        all_actions   = (len(self.actions_done) == len(self.actions_required))
        return all_waypoints and all_actions

    def compute_stars(self, duration_sec: Optional[float] = None) -> int:
        """Финальное количество звёзд = факт + бонус_аккуратности + бонус_скорости.

        Факт: 1 звезда за каждую посещённую точку + 1 за каждое
              выполненное действие. Не зависит от траектории — если робот
              физически попал на точку, звезда гарантирована.

        Бонус аккуратности: floor(база × аккуратность). При аккуратности 100%
              удваивает базу. При аккуратности 0% бонуса нет.

        Бонус скорости (если duration_sec задан): до +2 звёзд за быстрое
              прохождение, см. time_bonus_stars."""
        return (self.fact_stars()
                + self.track_bonus_stars()
                + self.time_bonus_stars(duration_sec))

    def fact_stars(self) -> int:
        """Звёзды-факт: по 1 за каждую посещённую точку и выполненное действие."""
        return len(self.waypoints_visited) + len(self.actions_done)

    def register_hint(self) -> None:
        """Игрок запросил подсказку — увеличиваем счётчик. Сам штраф
        к аккуратности вычисляется в effective_quality()."""
        self.hints_used += 1

    def effective_quality(self) -> float:
        """«Аккуратность» с учётом штрафов. Поле quality — run-аккумулятор
        (растёт за точки/действия). Поверх него вычитаем:
          • zone_penalty — наезды на зоны (по 20% за наезд, копится за
            прогон);
          • track_penalty — выход из коридора эталонной траектории
            (копится по QUALITY_DRAIN_PER_TICK за тик вне коридора);
          • hints_used × 20% — подсказки (счётчик живёт всю миссию).
        Штрафы хранятся отдельными аккумуляторами, чтобы штраф в начале
        прогона не «съелся» нижней границей quality=0. Зажато в [0, 1]."""
        return max(0.0, self.quality
                   - self.zone_penalty
                   - self.track_penalty
                   - self.hints_used * HINT_PENALTY)

    def track_bonus_stars(self) -> int:
        """Бонусные звёзды за аккуратность прохождения: floor(факт × аккуратность)."""
        return int(math.floor(self.fact_stars() * self.effective_quality()))

    def time_bonus_stars(self, duration_sec: Optional[float]) -> int:
        """Бонусные звёзды за скорость прохождения.

        Шкала привязана к количеству waypoints в миссии:
            target = 90 секунд × n_waypoints
            ≤ target/2 → 2⭐
            ≤ target   → 1⭐
            > target   → 0
        Бонус даётся только если миссия выполнена (есть посещённые точки
        ИЛИ выполненные действия) и duration_sec известен."""
        if duration_sec is None or duration_sec <= 0:
            return 0
        if self.fact_stars() == 0:
            return 0
        n = max(1, len(self.waypoints))
        target_sec = 90.0 * n
        if duration_sec <= target_sec / 2.0:
            return 2
        if duration_sec <= target_sec:
            return 1
        return 0

    # ── Сериализация для клиента ─────────────────────────────────────────

    def to_client_dict(self) -> dict:
        """Снимок для отправки клиенту через WebSocket (полный)."""
        return {
            "mission_id":   self.mission_id,
            "run_id":       self.run_id,
            "title":        self.title,
            "description":  self.description,
            "level":        self.level,
            "waypoints":    [list(p) for p in self.waypoints],
            "path":         [list(p) for p in self.path],
            "danger_zones": [list(z) for z in self.danger_zones],
            "actions":      list(self.actions_required),
            "safety_margin_cm": self.safety_margin_cm,
            "progress":     self.progress_dict(),
        }

    def action_counts(self) -> dict:
        """Счётчики обязательных действий с зонами по типам:
        установлено зон внимания (place_*) и удалено опасных зон —
        каждое в виде done/total. Для строки «Установлено K/M, удалено P/Q»."""
        place_total = place_done = remove_total = remove_done = 0
        for i, a in enumerate(self.actions_required):
            is_place = str(a.get("type", "")).startswith("place")
            done = i in self.actions_done
            if is_place:
                place_total += 1
                place_done  += int(done)
            else:
                remove_total += 1
                remove_done  += int(done)
        return {
            "place_done":   place_done,
            "place_total":  place_total,
            "remove_done":  remove_done,
            "remove_total": remove_total,
        }

    def progress_dict(self) -> dict:
        """Только динамика прогресса — для частых апдейтов на каждый push_state."""
        return {
            "waypoints_visited": sorted(self.waypoints_visited),
            "actions_done":      sorted(self.actions_done),
            "action_counts":     self.action_counts(),
            "quality":           round(self.effective_quality(), 3),
            "stars_now":         self.compute_stars(),
            "stars_fact":        self.fact_stars(),
            "stars_track":       self.track_bonus_stars(),
            "complete":          self.is_complete(),
        }


# ── Конструктор из БД-записи ──────────────────────────────────────────────

def from_mission_row(mission_row, user_id: int,
                     start_x: float, start_y: float,
                     run_id: Optional[int] = None,
                     track_tolerance_cm: float = 20.0) -> ActiveMission:
    """Создать ActiveMission из ORM-объекта Mission.
    `track_tolerance_cm` — габарит робота (max длина/ширина), допуск
    точности маршрута; передаёт start_mission из cfg."""
    waypoints = [tuple(p) for p in json.loads(mission_row.waypoints or "[]")]
    danger    = [tuple(z) for z in json.loads(mission_row.danger_zones or "[]")]
    actions   = json.loads(mission_row.actions_required or "[]")
    # path — полная плотная траектория для серого пунктира на canvas.
    # У старых миссий поле может отсутствовать → пустой список,
    # клиент тогда отрисует ломаную start→waypoints как fallback.
    path_raw  = getattr(mission_row, "path", None) or "[]"
    path      = [tuple(p) for p in json.loads(path_raw)]
    return ActiveMission(
        mission_id       = mission_row.id,
        run_id           = run_id,
        user_id          = user_id,
        title            = mission_row.title or "",
        description      = mission_row.description or "",
        level            = int(mission_row.level or 1),
        waypoints        = waypoints,
        path             = path,
        danger_zones     = danger,
        actions_required = actions,
        safety_margin_cm = float(mission_row.safety_margin_cm or 5.0),
        track_tolerance_cm = float(track_tolerance_cm),
        start_x          = start_x,
        start_y          = start_y,
    )
