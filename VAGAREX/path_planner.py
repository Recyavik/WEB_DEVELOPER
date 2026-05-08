"""
path_planner.py — поиск пути в обход опасных зон в режиме «осторожно».

Алгоритм:
  1) Регулярная сетка (cell_size см) по всему полю.
  2) Каждая клетка помечается как непроходимая, если в нее попадает
     стена или любая зона, раздутая на радиус робота + safety_margin.
  3) A* (8-связный) от стартовой клетки до целевой.
  4) Сглаживание visibility-based: убираем waypoint'ы, до которых
     виден прямой проход через предыдущий — получается ломаная с
     максимально длинными прямыми отрезками.

Возвращает список waypoint'ов в МИРОВЫХ координатах [(x, y), ...]
где первая точка ≈ start, последняя ≈ target. Или None — нет пути.
"""
from __future__ import annotations

import heapq
import math
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple


@dataclass
class Obstacle:
    """Опасная зона как круг в мировых координатах."""
    x:      float
    y:      float
    radius: float


def plan_path(
    start_xy:        Tuple[float, float],
    target_xy:       Tuple[float, float],
    obstacles:       List[Obstacle],
    world_w:         float,
    world_h:         float,
    wall_thickness:  float,
    robot_inflation: float,
    cell_size:       float = 10.0,
    safety_margin:   float = 5.0,
) -> Optional[List[Tuple[float, float]]]:
    """A* + visibility smoothing.

    `robot_inflation` — половина max(длина, ширина) робота (см).
    `safety_margin` — дополнительный запас (см) сверх корпуса.

    Препятствие непроходимо, если расстояние от центра до клетки
    меньше `obstacle.radius + robot_inflation + safety_margin`.
    Стена считается непроходимой за внутренней кромкой
    `(half - wall_thickness/2 - inflation - safety_margin)`."""
    if cell_size <= 0:
        return None

    grid_w = max(2, int(world_w / cell_size) + 1)
    grid_h = max(2, int(world_h / cell_size) + 1)
    half_w = world_w / 2.0
    half_h = world_h / 2.0

    infl_total  = robot_inflation + safety_margin
    inner_x     = half_w - wall_thickness / 2.0 - infl_total
    inner_y     = half_h - wall_thickness / 2.0 - infl_total

    obstacles_sq: List[Tuple[float, float, float]] = [
        (o.x, o.y, (o.radius + infl_total) ** 2) for o in obstacles
    ]

    def cell_to_world(ix: int, iy: int) -> Tuple[float, float]:
        return (-half_w + (ix + 0.5) * cell_size,
                -half_h + (iy + 0.5) * cell_size)

    def world_to_cell(wx: float, wy: float) -> Tuple[int, int]:
        ix = int((wx + half_w) / cell_size)
        iy = int((wy + half_h) / cell_size)
        return (max(0, min(grid_w - 1, ix)),
                max(0, min(grid_h - 1, iy)))

    def is_blocked(ix: int, iy: int) -> bool:
        wx, wy = cell_to_world(ix, iy)
        if abs(wx) > inner_x or abs(wy) > inner_y:
            return True
        for ox, oy, r2 in obstacles_sq:
            if (wx - ox) ** 2 + (wy - oy) ** 2 < r2:
                return True
        return False

    sx, sy = world_to_cell(*start_xy)
    tx, ty = world_to_cell(*target_xy)

    if is_blocked(sx, sy) or is_blocked(tx, ty):
        return None
    if (sx, sy) == (tx, ty):
        return [start_xy, target_xy]

    # 8-связные направления и их стоимость
    NEIGHBORS = (
        (-1, -1, 1.41421356), (-1, 0, 1.0), (-1, 1, 1.41421356),
        ( 0, -1, 1.0),                       ( 0, 1, 1.0),
        ( 1, -1, 1.41421356), ( 1, 0, 1.0), ( 1, 1, 1.41421356),
    )

    def heuristic(ix: int, iy: int) -> float:
        return math.hypot(ix - tx, iy - ty)

    open_heap: List[Tuple[float, float, int, int]] = []
    heapq.heappush(open_heap, (heuristic(sx, sy), 0.0, sx, sy))
    came_from: dict[Tuple[int, int], Tuple[int, int]] = {}
    g_score:   dict[Tuple[int, int], float] = {(sx, sy): 0.0}
    closed:    set[Tuple[int, int]] = set()

    found = False
    while open_heap:
        _, g, ix, iy = heapq.heappop(open_heap)
        if (ix, iy) in closed:
            continue
        closed.add((ix, iy))
        if (ix, iy) == (tx, ty):
            found = True
            break
        for dx, dy, dcost in NEIGHBORS:
            nx, ny = ix + dx, iy + dy
            if not (0 <= nx < grid_w and 0 <= ny < grid_h):
                continue
            if (nx, ny) in closed:
                continue
            if is_blocked(nx, ny):
                continue
            ng = g + dcost
            if ng < g_score.get((nx, ny), math.inf):
                g_score[(nx, ny)]   = ng
                came_from[(nx, ny)] = (ix, iy)
                heapq.heappush(open_heap, (ng + heuristic(nx, ny), ng, nx, ny))

    if not found:
        return None

    # Восстановление пути
    cells: List[Tuple[int, int]] = [(tx, ty)]
    cur = (tx, ty)
    while cur in came_from:
        cur = came_from[cur]
        cells.append(cur)
    cells.reverse()

    smoothed = _smooth_los(cells, is_blocked)

    # Перевод в мировые координаты, плюс точные start и target.
    waypoints: List[Tuple[float, float]] = [start_xy]
    for c in smoothed[1:-1]:
        waypoints.append(cell_to_world(*c))
    waypoints.append(target_xy)
    return waypoints


def _smooth_los(cells: List[Tuple[int, int]],
                is_blocked: Callable[[int, int], bool]
                ) -> List[Tuple[int, int]]:
    """Visibility-based сглаживание: оставляем только те waypoint'ы, до
    которых нельзя «дотянуться по прямой» из предыдущего."""
    if len(cells) < 3:
        return cells
    smoothed = [cells[0]]
    i = 0
    while i < len(cells) - 1:
        # Ищем самую дальнюю клетку, до которой еще «видно» по прямой.
        j = len(cells) - 1
        while j > i + 1:
            if _line_clear(cells[i], cells[j], is_blocked):
                break
            j -= 1
        smoothed.append(cells[j])
        i = j
    return smoothed


def _line_clear(a: Tuple[int, int], b: Tuple[int, int],
                is_blocked: Callable[[int, int], bool]) -> bool:
    """Bresenham по сетке: True если ни одна клетка на отрезке a-b
    не помечена как блокирующая."""
    x0, y0 = a
    x1, y1 = b
    dx = abs(x1 - x0)
    dy = abs(y1 - y0)
    sx = 1 if x0 < x1 else -1
    sy = 1 if y0 < y1 else -1
    err = dx - dy
    x, y = x0, y0
    while True:
        if is_blocked(x, y):
            return False
        if (x, y) == (x1, y1):
            return True
        e2 = 2 * err
        if e2 > -dy:
            err -= dy
            x   += sx
        if e2 < dx:
            err += dx
            y   += sy


# ── Сглаживание ломаной в дугообразную кривую ──────────────────────────

def chaikin_smooth(points: List[Tuple[float, float]],
                   iterations: int = 3) -> List[Tuple[float, float]]:
    """Алгоритм Чайкина: отрезает углы ломаной, превращая ее в дугообразную
    кривую. Каждая итерация удваивает число точек и сглаживает повороты —
    робот, следующий за такой траекторией, будет крутить рулем непрерывно
    и плавно, без K-turn'ов.

    Эндпоинты не двигаются. 3 итерации обычно дают визуально гладкую дугу."""
    if len(points) < 3:
        return list(points)
    pts = [tuple(p) for p in points]
    for _ in range(max(1, int(iterations))):
        new = [pts[0]]
        for i in range(len(pts) - 1):
            p, q = pts[i], pts[i + 1]
            # Точки в 1/4 и 3/4 отрезка p→q
            new.append(((3.0 * p[0] + q[0]) / 4.0, (3.0 * p[1] + q[1]) / 4.0))
            new.append(((p[0] + 3.0 * q[0]) / 4.0, (p[1] + 3.0 * q[1]) / 4.0))
        new.append(pts[-1])
        pts = new
    return pts


def resample_curve(points: List[Tuple[float, float]],
                   step_cm: float = 5.0) -> List[Tuple[float, float]]:
    """Пересэмплирует ломаную/кривую так, чтобы соседние точки были
    приблизительно на расстоянии step_cm друг от друга. Нужно для
    pure-pursuit lookahead: одинаковый шаг — стабильный поиск цели."""
    if len(points) < 2 or step_cm <= 0:
        return list(points)
    out = [points[0]]
    accum = 0.0
    for i in range(len(points) - 1):
        ax, ay = points[i]
        bx, by = points[i + 1]
        seg = math.hypot(bx - ax, by - ay)
        if seg < 1e-6:
            continue
        # Сколько целых шагов укладывается с учетом «остатка» с прошлого сегмента
        t0 = (step_cm - accum) / seg
        t = t0
        while t < 1.0:
            out.append((ax + t * (bx - ax), ay + t * (by - ay)))
            t += step_cm / seg
        accum = (1.0 - (t - step_cm / seg)) * seg
    if (out[-1][0], out[-1][1]) != (points[-1][0], points[-1][1]):
        out.append(points[-1])
    return out


# ── Геометрия для проверки прямого участка без планировщика ────────────
def line_hits_zones(
    a: Tuple[float, float],
    b: Tuple[float, float],
    obstacles: List[Obstacle],
    robot_inflation: float,
    safety_margin: float = 5.0,
) -> bool:
    """True, если отрезок a→b пересекает какую-либо раздутую зону.
    Используется чтобы решить «нужен ли планировщик» — если прямая
    свободна, ничего планировать не надо."""
    ax, ay = a
    bx, by = b
    abx, aby = bx - ax, by - ay
    ab2 = abx * abx + aby * aby
    if ab2 < 1e-9:
        return False
    for o in obstacles:
        eff_r = o.radius + robot_inflation + safety_margin
        # Проекция центра окружности на отрезок
        t = ((o.x - ax) * abx + (o.y - ay) * aby) / ab2
        t = max(0.0, min(1.0, t))
        cx = ax + t * abx
        cy = ay + t * aby
        if (cx - o.x) ** 2 + (cy - o.y) ** 2 < eff_r * eff_r:
            return True
    return False
