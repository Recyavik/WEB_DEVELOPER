"""
world_xy.py — состояние робота и мир в координатах X,Y

Вместо клеточного поля (как в VEGA) — непрерывное пространство.
Зоны опасности — круги с центром (x, y) и радиусом.
Позиция и курс робота — вещественные числа (сантиметры, градусы).
"""
import math
from dataclasses import dataclass, field
from typing import List, Optional, Tuple


@dataclass
class DangerZoneXY:
    x:      float
    y:      float
    radius: float = 50.0
    label:  str   = "Опасная зона"
    db_id:  Optional[int] = None
    # 'danger'    — красная зона обстановки
    # 'algorithm' — жёлтая пунктирная зона, проставленная алгоритмом
    kind:   str   = "danger"


@dataclass
class RobotStateXY:
    x:          float = 0.0
    y:          float = 0.0
    heading:    float = 0.0   # градусы, 0=север (Y+), 90=восток (X+)
    speed:     float = 0.0   # % мощности (+ вперёд, - назад, 0 = стоп)
    steer:     float = 0.0   # угол руля (-45..45), машина стоит пока не движется
    dist_left: float = 0.0   # осталось сантиметров; 0 = ехать до стопа
    mode:       str   = "normal"   # normal | marker | inspector
    cautious:   bool  = False
    laser_dist: float = 0.0   # см, 0 = нет данных
    laser_stop: bool  = False  # остановить когда лазер ≤ WALL_THICKNESS_CM
    light_color: tuple = (0, 0, 0)
    battery:    float = 100.0  # заряд аккумулятора, 0..100%
    # Состояние «робот думает» (планировщик в режиме «осторожно»):
    #   "idle"     — обычное
    #   "planning" — фиолетовая иконка 🖥, идёт A*-поиск пути
    #   "failed"   — красная иконка, путь не найден, нужен ручной режим
    thinking:   str   = "idle"


@dataclass
class WorldXY:
    width:        float = 500.0
    height:       float = 500.0
    danger_zones: List[DangerZoneXY]    = field(default_factory=list)
    path_history: List[Tuple[float, float]] = field(default_factory=list)
    # Сегменты пути, найденные планировщиком в режиме «осторожно».
    # Каждый сегмент — список waypoint'ов от старта до цели одного goto.
    # Рисуются на canvas фиолетовым пунктиром. Очищаются при сбросе поля.
    auto_segments: List[List[Tuple[float, float]]] = field(default_factory=list)

    def add_danger_zone(self, x: float, y: float,
                        radius: float = 50.0, label: str = "Опасная зона",
                        db_id: Optional[int] = None,
                        kind: str = "danger") -> DangerZoneXY:
        zone = DangerZoneXY(x=x, y=y, radius=radius, label=label,
                            db_id=db_id, kind=kind)
        self.danger_zones.append(zone)
        return zone

    def remove_danger_zone_by_db_id(self, db_id: int) -> bool:
        before = len(self.danger_zones)
        self.danger_zones = [z for z in self.danger_zones if z.db_id != db_id]
        return len(self.danger_zones) < before

    def remove_zones_at(self, x: float, y: float,
                        kind: Optional[str] = None) -> List[DangerZoneXY]:
        """Удаляет все зоны, в которые попадает точка (x, y).
        Если kind задан — фильтрует по типу ('danger' | 'algorithm')."""
        removed: List[DangerZoneXY] = []
        kept:    List[DangerZoneXY] = []
        for z in self.danger_zones:
            inside = math.hypot(x - z.x, y - z.y) <= z.radius
            kind_ok = (kind is None) or (z.kind == kind)
            if inside and kind_ok:
                removed.append(z)
            else:
                kept.append(z)
        self.danger_zones = kept
        return removed

    def clear_danger_zones(self):
        self.danger_zones.clear()

    def zone_at(self, x: float, y: float) -> Optional[DangerZoneXY]:
        for z in self.danger_zones:
            if math.hypot(x - z.x, y - z.y) <= z.radius:
                return z
        return None

    def add_path(self, x: float, y: float):
        # Без обрезания «хвоста» — пользователь хочет видеть весь путь
        # с самого начала. Чекбокс «Путь» в тулбаре полностью скрывает или
        # показывает траекторию.
        self.path_history.append((x, y))

    def clear_path(self):
        self.path_history.clear()

    def clear_auto_segments(self):
        self.auto_segments.clear()

    def add_auto_segment(self, waypoints: List[Tuple[float, float]]) -> None:
        """Зарегистрировать новый автоматически рассчитанный сегмент маршрута."""
        if len(waypoints) >= 2:
            self.auto_segments.append([(float(x), float(y)) for x, y in waypoints])

    def to_dict(self) -> dict:
        return {
            "width":  self.width,
            "height": self.height,
            "danger_zones": [
                {"x": z.x, "y": z.y, "radius": z.radius,
                 "label": z.label, "db_id": z.db_id, "kind": z.kind}
                for z in self.danger_zones
            ],
            "path_history":  self.path_history,
            "auto_segments": self.auto_segments,
        }


def state_to_dict(state: RobotStateXY) -> dict:
    return {
        "x":         state.x,
        "y":         state.y,
        "heading":   state.heading,
        "speed":     state.speed,
        "steer":     state.steer,
        "dist_left": state.dist_left,
        "mode":      state.mode,
        "cautious":  state.cautious,
        "laser_dist": state.laser_dist,
        "light_color": list(state.light_color),
        "battery":   state.battery,
        "thinking":  state.thinking,
    }


def heading_dx_dy(heading_deg: float) -> Tuple[float, float]:
    """Вектор движения для заданного курса (0=север=Y+)."""
    rad = math.radians(heading_deg)
    return math.sin(rad), math.cos(rad)


def update_position_dead_reckoning(state: RobotStateXY,
                                   dt: float,
                                   speed_cm_per_sec: float) -> None:
    """Обновляет позицию по методу мёртвого счисления."""
    dx, dy = heading_dx_dy(state.heading)
    state.x += dx * speed_cm_per_sec * dt
    state.y += dy * speed_cm_per_sec * dt
