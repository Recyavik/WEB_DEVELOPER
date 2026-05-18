"""mission_state.py — runtime-состояние активной миссии в UserSession.

Изолирует трекинг прохождения миссии (посещение точек, выполнение
действий с зонами, отклонения от траектории, качество прохождения)
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

# Скорость убывания «Качества» за один tick (push_state), пока робот
# вне коридора траектории. ~10 push_state/с × 0.005 = 0.05/с → 10 секунд
# отклонения стоят −0.5 качества.
QUALITY_DRAIN_PER_TICK = 0.005

# Штраф к «Качеству» за каждую запрошенную подсказку (как за наезд
# на зону). Считается отдельным счётчиком hints_used и вычитается в
# effective_quality() — переживает перезапуск программы (▶).
HINT_PENALTY = 0.05


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
    # «Качество прохождения» 0..1: стартует с 0, растёт за посещённые
    # точки и выполненные действия, убывает за отклонение от траектории
    # и наезды на опасные зоны. Зажато в [0, 1]. Идёт в звёзды-бонус.
    quality:           float    = 0.0
    # «Точность ведения» 0..1: чистый храповик следования траектории —
    # старт 1.0, падает ТОЛЬКО за выход из коридора, не зависит от
    # посещения точек и наездов на зоны. Справочная метрика рядом с
    # «Качеством»; в звёзды НЕ идёт (бонус считается от качества).
    coefficient:       float    = 1.0
    deviations:        int      = 0
    # Сколько подсказок запросил игрок за всю миссию. НЕ сбрасывается
    # прогоном ▶ — штраф за подсказку должен пережить перезапуск кода.
    hints_used:        int      = 0
    last_in_margin:    bool     = True
    last_robot_pos:    Optional[tuple[float, float]] = None
    started_at:        datetime = field(default_factory=datetime.utcnow)
    last_algo_duration_sec: Optional[float] = None
    # Учёт наездов на опасные зоны. Используется
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
    # Индексы danger_zones, которые являются задачей «удалить зону»
    # (есть парный remove_danger action). Наезд на них НЕ штрафуется —
    # заехать внутрь нужно по заданию. Штрафуются только нетронутые
    # зоны-препятствия. Заполняется в __post_init__.
    removable_zone_idx: set[int] = field(default_factory=set)
    # Вклад одной проверочной позиции (точка / действие с зоной) в
    # «Качество» = 1 / (точки + действия). Заполняется в __post_init__.
    quality_step: float = 0.0

    def __post_init__(self):
        """Вычисляет производные поля:
          • removable_zone_idx — danger-зоны с парным remove_danger
            (наезд на них не штрафуется — заехать нужно по заданию);
          • quality_step — вклад одной проверочной позиции в «Качество»."""
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

    # ── Трекинг отклонения и обновление качества ─────────────────────────

    def full_path(self) -> list[tuple[float, float]]:
        """Эталонная траектория: старт → waypoint_1 → waypoint_2 → …"""
        return [(self.start_x, self.start_y), *self.waypoints]

    def update_quality(self, robot_x: float, robot_y: float) -> bool:
        """Обновить «Качество прохождения» по текущей позиции робота.

        Качество РАСТЁТ за посещённые точки и выполненные действия —
        это делают mark_waypoint_visits / try_match_action. Здесь оно
        только УБЫВАЕТ:
          • робот вне коридора траектории → качество плавно падает,
            пока он не вернётся. Эталон — плотная траектория `path`
            (если задана), иначе ломаная старт→waypoints; ширина
            коридора — `track_tolerance_cm` (габарит робота);
          • наезд на нетронутую опасную зону → −5% (_mark_danger_zone_hits).
        Параллельно ведётся `coefficient` («точность ведения») — чистый
        храповик следования траектории: падает за то же отклонение, но
        не реагирует на точки/зоны и не растёт обратно.
        Зажато в [0, 1]. Возвращает True при переходе «в коридор / вне»."""
        self._mark_danger_zone_hits(robot_x, robot_y)

        ref = self.path if len(self.path) >= 2 else self.full_path()
        d = dist_to_path(robot_x, robot_y, ref)
        in_margin = d <= self.track_tolerance_cm
        if not in_margin:
            self.quality = max(0.0, self.quality - QUALITY_DRAIN_PER_TICK)
            self.coefficient = max(0.0,
                                   self.coefficient - QUALITY_DRAIN_PER_TICK)

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

        Зоны-задачи «удалить» (removable_zone_idx) НЕ штрафуются вовсе —
        заехать в них нужно по заданию. Штрафуются только нетронутые
        зоны-препятствия.

        Зоны в finalized больше не учитываются (повторные заезды бесплатны
        — у нас «один наезд = один минус», как и раньше).

        Возвращает число штрафных наездов в этом тике."""
        if not self.danger_zones:
            return 0
        new_hits = 0
        for i, (zx, zy, zr) in enumerate(self.danger_zones):
            if i in self.danger_zones_finalized:
                continue
            if i in self.removable_zone_idx:
                continue   # зона-задача «удалить» — наезд не штрафуется
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
                    self.quality = max(0.0, self.quality - 0.05)
                    new_hits += 1
        return new_hits

    def finalize_remaining_zones(self) -> int:
        """Закрыть учёт зон в конце миссии (вызывается из stop_mission).

        Робот мог завершить прогон, ОСТАВШИСЬ внутри опасной зоны —
        выхода не было, exit-only машина `_mark_danger_zone_hits` такую
        зону не финализировала. Здесь добиваем только такие зоны:
        прощена (действие выполнено внутри) → бесплатно, иначе −5%.

        Зоны, которых робот вообще не касался, тут НЕ трогаются — они
        не в `danger_zones_inside`, наездом не считаются.
        Возвращает число штрафных зон."""
        new_hits = 0
        for i in list(self.danger_zones_inside):
            if i in self.danger_zones_finalized:
                continue
            self.danger_zones_inside.discard(i)
            self.danger_zones_finalized.add(i)
            if i in self.danger_zones_forgiven:
                self.danger_zones_forgiven.discard(i)
            else:
                self.quality = max(0.0, self.quality - 0.05)
                new_hits += 1
        return new_hits

    def reset_for_new_run(self) -> None:
        """Сброс динамики трекинга перед новым прогоном (▶ Проверка кода).

        Оценивается только ПОСЛЕДНЕЕ исполнение — иначе старый успех даёт
        звёзды даже после порчи кода. Учёт зон тоже обнуляется: финализация
        прошлого прогона не должна «помнить» зоны в этом. Накопленное
        время алгоритма (last_algo_duration_sec) НЕ сбрасывается."""
        self.waypoints_visited.clear()
        self.actions_done.clear()
        self.quality        = 0.0
        self.coefficient    = 1.0
        self.deviations     = 0
        self.last_in_margin = True
        self.last_robot_pos = None
        self.danger_zones_inside.clear()
        self.danger_zones_forgiven.clear()
        self.danger_zones_finalized.clear()

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
        # Каждая новая посещённая точка поднимает «Качество».
        if new:
            self.quality = min(1.0, self.quality
                               + self.quality_step * len(new))
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
                # Выполненное действие с зоной поднимает «Качество».
                self.quality = min(1.0, self.quality + self.quality_step)
                return i
        return None

    # ── Завершение ───────────────────────────────────────────────────────

    def is_complete(self) -> bool:
        """Все обязательные цели достигнуты?"""
        all_waypoints = (len(self.waypoints_visited) == len(self.waypoints))
        all_actions   = (len(self.actions_done) == len(self.actions_required))
        return all_waypoints and all_actions

    def compute_stars(self, duration_sec: Optional[float] = None) -> int:
        """Финальное количество звёзд = факт + бонус_качества + бонус_скорости.

        Факт: 1 звезда за каждую посещённую точку + 1 за каждое
              выполненное действие. Не зависит от траектории — если робот
              физически попал на точку, звезда гарантирована.

        Бонус качества: floor(база × качество). При качестве 100%
              удваивает базу. При качестве 0% бонуса нет.

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
        к качеству вычисляется в effective_quality()."""
        self.hints_used += 1

    def effective_quality(self) -> float:
        """«Качество» с учётом штрафа за подсказки. Поле quality —
        run-аккумулятор, сбрасывается каждым прогоном ▶; hints_used
        живёт всю миссию. Поэтому штраф за подсказки вычитаем здесь,
        поверх аккумулятора. Зажато в [0, 1]."""
        return max(0.0, self.quality - self.hints_used * HINT_PENALTY)

    def track_bonus_stars(self) -> int:
        """Бонусные звёзды за качество прохождения: floor(факт × качество)."""
        return int(math.floor(self.fact_stars() * self.effective_quality()))

    def time_bonus_stars(self, duration_sec: Optional[float]) -> int:
        """Бонусные звёзды за скорость прохождения.

        Шкала привязана к количеству waypoints в миссии:
            target = 30 секунд × n_waypoints
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
        target_sec = 30.0 * n
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
