"""mission_state.py — runtime-состояние активной миссии в UserSession.

Изолирует трекинг прохождения миссии (посещение точек, выполнение
действий с зонами, отклонения от траектории, динамический коэффициент)
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


# ── ActiveMission ──────────────────────────────────────────────────────────

# Допуск попадания в waypoint = safety_margin_cm миссии (= Запас безопасности
# пользователя на момент генерации). Это симметрично с проверкой отклонения
# от траектории: можно отклоняться в пределах того же радиуса, в пределах
# которого засчитывается точка. Проверяется не точка (которую робот может
# «проскочить» между двумя кадрами на быстрой скорости), а ОТРЕЗОК движения
# за один тик: точка засчитана, если её расстояние до отрезка
# (prev_pos → curr_pos) ≤ safety_margin_cm.
# Константа оставлена как fallback для миссий с safety_margin_cm = 0.
WAYPOINT_FALLBACK_TOLERANCE_CM = 1.0

# Радиус матчинга действия с зоной (для зон-действий).
ACTION_TOLERANCE_CM = 15.0

# Шаг изменения коэффициента за один tick (push_state). Подобран так,
# что 10 секунд непрерывного отклонения дают примерно −0.5 коэффициента
# (~10 push_state в секунду × 0.005 = 0.05/сек, ×10с = 0.5).
COEFF_STEP_PER_TICK = 0.005


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

    # Динамическое состояние трекинга
    waypoints_visited: set[int] = field(default_factory=set)
    actions_done:      set[int] = field(default_factory=set)
    coefficient:       float    = 1.0
    deviations:        int      = 0
    last_in_margin:    bool     = True
    last_robot_pos:    Optional[tuple[float, float]] = None
    started_at:        datetime = field(default_factory=datetime.utcnow)
    last_algo_duration_sec: Optional[float] = None
    # Учёт наездов на опасные зоны (инспектор-режим L2). Используется
    # state-машина «exit-only counting»:
    #   inside       — робот ВПРЯМО СЕЙЧАС внутри этой зоны
    #   forgiven     — игрок выполнил действие (remove_danger/place_attention)
    #                  пока находился внутри → при выходе наезд не штрафуется
    #   finalized    — финальное решение принято (либо −5%, либо прощено),
    #                  больше эту зону не учитываем
    # Логика: −5% начисляется только когда робот ПОКИДАЕТ зону, в которой
    # не было действия. Это даёт игроку возможность сначала зайти в зону
    # и убрать/установить, а потом выйти без штрафа.
    danger_zones_inside:    set[int] = field(default_factory=set)
    danger_zones_forgiven:  set[int] = field(default_factory=set)
    danger_zones_finalized: set[int] = field(default_factory=set)

    # ── Режим миссии ─────────────────────────────────────────────────────

    @property
    def is_inspector(self) -> bool:
        """Уровни 1-2 «инспектор» — без эталонной траектории.
        Оценка идёт только по посещению точек, выполнению действий и
        наездам на опасные зоны (каждый наезд −5% точности)."""
        return self.level in (1, 2)

    # ── Трекинг отклонения и обновление коэффициента ─────────────────────

    def full_path(self) -> list[tuple[float, float]]:
        """Эталонная траектория: старт → waypoint_1 → waypoint_2 → …"""
        return [(self.start_x, self.start_y), *self.waypoints]

    def update_coefficient(self, robot_x: float, robot_y: float) -> bool:
        """Обновить коэффициент по текущей позиции робота.

        В обычном режиме: расстояние до эталонной траектории. В пределах
        safety_margin — коэффициент растёт, вне — падает.

        В инспектор-режиме (L1/L2): эталонной траектории нет, коэффициент
        НЕ меняется по позиции. Штрафы идут только при наездах на опасные
        зоны (`mark_danger_zone_hits`). Возвращает True если случился
        переход «в margin / вне margin» (для журнала)."""
        if self.is_inspector:
            # Инспектор: проверяем только наезды на опасные зоны.
            self._mark_danger_zone_hits(robot_x, robot_y)
            return False

        d = dist_to_path(robot_x, robot_y, self.full_path())
        in_margin = d <= self.safety_margin_cm
        if in_margin:
            self.coefficient = min(1.0, self.coefficient + COEFF_STEP_PER_TICK)
        else:
            self.coefficient = max(0.0, self.coefficient - COEFF_STEP_PER_TICK)

        transitioned = (in_margin != self.last_in_margin)
        if transitioned and not in_margin:
            self.deviations += 1
        self.last_in_margin = in_margin
        return transitioned

    def _mark_danger_zone_hits(self, robot_x: float, robot_y: float) -> int:
        """Exit-only state-машина:
          • Робот ВНУТРИ зоны i, ранее не был → добавляем i в inside.
          • Робот ВЫШЕЛ из зоны i:
              если зона в forgiven → finalize без штрафа (игрок выполнил
                действие внутри);
              иначе → finalize со штрафом −5%.

        Зоны в finalized больше не учитываются (повторные заезды бесплатны
        — у нас «один наезд = один минус», как и раньше).

        Возвращает число штрафных наездов в этом тике."""
        if not self.danger_zones:
            return 0
        new_hits = 0
        for i, (zx, zy, zr) in enumerate(self.danger_zones):
            if i in self.danger_zones_finalized:
                continue
            inside_now = math.hypot(robot_x - zx, robot_y - zy) <= zr
            was_inside = i in self.danger_zones_inside
            if inside_now and not was_inside:
                self.danger_zones_inside.add(i)
            elif was_inside and not inside_now:
                # Вышел из зоны — момент решения.
                self.danger_zones_inside.discard(i)
                self.danger_zones_finalized.add(i)
                if i in self.danger_zones_forgiven:
                    self.danger_zones_forgiven.discard(i)
                else:
                    self.coefficient = max(0.0, self.coefficient - 0.05)
                    new_hits += 1
        return new_hits

    def forgive_current_zone_hits(self, robot_x: float, robot_y: float) -> None:
        """Помечает все опасные зоны, в которых сейчас находится робот,
        как «прощённые» — при выходе из них штраф −5% не начисляется.
        Вызывается ровно в момент выполнения действия (remove_danger /
        place_attention), чтобы наезд во время действия не учитывался."""
        for i, (zx, zy, zr) in enumerate(self.danger_zones):
            if i in self.danger_zones_finalized:
                continue
            if math.hypot(robot_x - zx, robot_y - zy) <= zr:
                self.danger_zones_forgiven.add(i)

    # ── Чекпоинты и действия ─────────────────────────────────────────────

    def mark_waypoint_visits(self, robot_x: float, robot_y: float) -> list[int]:
        """Отметить waypoints, через которые робот ПРОЕХАЛ.

        Допуск = safety_margin_cm миссии (Запас безопасности). Это тот же
        радиус, в пределах которого «отклонение от траектории» считается
        приемлемым — симметричная логика.

        Проверяется не точка (текущая позиция), а ОТРЕЗОК движения за
        один тик: точка засчитана, если расстояние от неё до отрезка
        (prev_pos → curr_pos) ≤ допуска. Это устраняет «проскок» через
        точку на быстрой скорости, когда между двумя push_state робот
        может уйти на 5-10 см и при точечной проверке промахнуться.

        На первом кадре отрезка ещё нет — fallback на точку."""
        tolerance = self.safety_margin_cm if self.safety_margin_cm > 0 \
                    else WAYPOINT_FALLBACK_TOLERANCE_CM
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
        return new

    def try_match_action(self, action_type: str,
                         x: float, y: float) -> Optional[int]:
        """Попытаться сматчить выполненное пользователем действие
        с обязательным action из списка. Возвращает индекс матча или None.

        Используется из диспетчера: когда юзер ставит/удаляет зону,
        вызываем try_match_action("place_attention", x, y) — если в
        actions_required есть такая (в радиусе ACTION_TOLERANCE_CM от
        указанной координаты), помечаем выполненной."""
        for i, action in enumerate(self.actions_required):
            if i in self.actions_done:
                continue
            if action.get("type") != action_type:
                continue
            ax = float(action.get("x", 0))
            ay = float(action.get("y", 0))
            if math.hypot(x - ax, y - ay) <= ACTION_TOLERANCE_CM:
                self.actions_done.add(i)
                return i
        return None

    # ── Завершение ───────────────────────────────────────────────────────

    def is_complete(self) -> bool:
        """Все обязательные цели достигнуты?"""
        all_waypoints = (len(self.waypoints_visited) == len(self.waypoints))
        all_actions   = (len(self.actions_done) == len(self.actions_required))
        return all_waypoints and all_actions

    def compute_stars(self, duration_sec: Optional[float] = None) -> int:
        """Финальное количество звёзд = факт + бонус_точности + бонус_скорости.

        Факт: 1 звезда за каждую посещённую точку + 1 за каждое
              выполненное действие. Не зависит от траектории — если робот
              физически попал на точку, звезда гарантирована.

        Бонус точности: floor(база × coefficient). При точности 100%
              удваивает базу. При точности 0% бонуса нет.

        Бонус скорости (если duration_sec задан): до +2 звёзд за быстрое
              прохождение, см. time_bonus_stars."""
        return (self.fact_stars()
                + self.track_bonus_stars()
                + self.time_bonus_stars(duration_sec))

    def fact_stars(self) -> int:
        """Звёзды-факт: по 1 за каждую посещённую точку и выполненное действие."""
        return len(self.waypoints_visited) + len(self.actions_done)

    def track_bonus_stars(self) -> int:
        """Бонусные звёзды за точность траектории."""
        return int(math.floor(self.fact_stars() * self.coefficient))

    def time_bonus_stars(self, duration_sec: Optional[float]) -> int:
        """Бонусные звёзды за скорость прохождения.

        Шкала привязана к количеству waypoints в миссии:
            target = 10 секунд × n_waypoints
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
        target_sec = 10.0 * n
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

    def progress_dict(self) -> dict:
        """Только динамика прогресса — для частых апдейтов на каждый push_state."""
        return {
            "waypoints_visited": sorted(self.waypoints_visited),
            "actions_done":      sorted(self.actions_done),
            "coefficient":       round(self.coefficient, 3),
            "deviations":        self.deviations,
            "in_margin":         self.last_in_margin,
            "stars_now":         self.compute_stars(),
            "stars_fact":        self.fact_stars(),
            "stars_track":       self.track_bonus_stars(),
            "complete":          self.is_complete(),
        }


# ── Конструктор из БД-записи ──────────────────────────────────────────────

def from_mission_row(mission_row, user_id: int,
                     start_x: float, start_y: float,
                     run_id: Optional[int] = None) -> ActiveMission:
    """Создать ActiveMission из ORM-объекта Mission."""
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
        start_x          = start_x,
        start_y          = start_y,
    )
