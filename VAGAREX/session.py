"""
session.py — изоляция состояния симулятора по пользователям.

Каждый залогиненный пользователь получает отдельный UserSession:
— свой robot_state, world, драйвер робота
— свои настройки (UserSettings из БД)
— свою программу (_program), очередь команд (_pending), фоновые задачи
— свои WebSocket-подключения (broadcast только своим)
— свою запись в RobotSession (для логов команд)

Сессии хранятся в SESSIONS dict, ключ = user_id. Создаются лениво
при первом обращении (HTTP-запрос или WS-подключение залогиненного
пользователя). Останавливаются вручную (или при остановке приложения).
"""
from __future__ import annotations

import asyncio
import itertools
import logging
import math
import re
import time
from dataclasses import asdict, dataclass
from typing import Optional

from fastapi import WebSocket
from sqlalchemy.orm import Session

import nlu
from database import SessionLocal
from models import (CommandLog, DangerZone, PathPoint, ProgramCommand,
                    RobotSession, User, UserSettings)
from robot_driver import SimDriver, make_driver
from world_xy import (DangerZoneXY, RobotStateXY, WorldXY,
                       state_to_dict, update_position_dead_reckoning)

log = logging.getLogger(__name__)

# Константы, общие для всех пользователей
SOUND_SPEED_CM_S = 34000.0
LIGHT_INDEX = 0
LIGHT_COUNT = 1
LIGHT_DEFAULT_COLOR = (255, 255, 255)

# Интенты, которые не записываются в программу
_NO_RECORD = {"report_pos", "report_status", "path_show", "path_hide", "recharge"}


# ═══════════════════════════════════════════════════════════════════════════════
# Команда в очереди
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class RobotCmd:
    id:       int
    intent:   str
    label:    str
    code:     str
    raw:      str
    playback: bool = False


# ═══════════════════════════════════════════════════════════════════════════════
# Настройки пользователя (snapshot из БД, без обращений в БД на каждый тик)
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class UserCfg:
    rex_host:           str
    rex_port:           int
    simulation_mode:    bool
    move_speed:         int
    turn_angle:         int
    wheel_circ_cm:      float
    speed_at_100:       float
    heading_per_rot:    float
    turn_speed_ref:     int
    world_w_cm:         float
    world_h_cm:         float
    wall_thickness_cm:  float
    robot_length_cm:    float
    robot_width_cm:     float
    start_x_cm:         float
    start_y_cm:         float
    start_heading_deg:  float
    sensor_type:        str
    sonar_interval_ms:  int
    danger_zone_radius: float
    battery_minutes:    int  = 60
    # runtime-only флаг, не из БД: дальномер по факту используется
    # для остановки у стены. Переключается через тулбар-чекбокс «Дальномер».
    laser_enabled:      bool = True

    @staticmethod
    def from_row(row: UserSettings) -> "UserCfg":
        return UserCfg(
            rex_host           = row.rex_host,
            rex_port           = row.rex_port,
            simulation_mode    = row.simulation_mode,
            move_speed         = row.move_speed,
            turn_angle         = row.turn_angle,
            wheel_circ_cm      = row.wheel_circ_cm,
            speed_at_100       = row.speed_at_100,
            heading_per_rot    = row.heading_per_rot,
            turn_speed_ref     = row.turn_speed_ref,
            world_w_cm         = row.world_w_cm,
            world_h_cm         = row.world_h_cm,
            wall_thickness_cm  = row.wall_thickness_cm,
            robot_length_cm    = row.robot_length_cm,
            robot_width_cm     = row.robot_width_cm,
            start_x_cm         = row.start_x_cm,
            start_y_cm         = row.start_y_cm,
            start_heading_deg  = row.start_heading_deg,
            sensor_type        = row.sensor_type,
            sonar_interval_ms  = row.sonar_interval_ms,
            danger_zone_radius = row.danger_zone_radius,
            battery_minutes    = max(1, int(row.battery_minutes or 60)),
        )


def _ensure_user_settings(db: Session, user_id: int) -> UserSettings:
    """Возвращает UserSettings пользователя; создаёт с дефолтами если нет."""
    row = db.query(UserSettings).filter(UserSettings.user_id == user_id).first()
    if row is None:
        row = UserSettings(user_id=user_id)
        db.add(row)
        db.commit()
        db.refresh(row)
    return row


# ═══════════════════════════════════════════════════════════════════════════════
# UserSession — вся изолированная логика одного пользователя
# ═══════════════════════════════════════════════════════════════════════════════

class UserSession:
    def __init__(self, user_id: int, cfg: UserCfg):
        self.user_id = user_id
        self.cfg     = cfg

        self.world       = WorldXY(width=cfg.world_w_cm, height=cfg.world_h_cm)
        self.robot_state = RobotStateXY(
            x       = float(cfg.start_x_cm),
            y       = float(cfg.start_y_cm),
            heading = float(cfg.start_heading_deg) % 360,
        )
        self.robot = make_driver(cfg.simulation_mode, cfg.rex_host, cfg.rex_port)

        self._cmd_counter:  itertools.count = itertools.count(1)
        self._pending:      list[RobotCmd]  = []
        self._executing:    Optional[RobotCmd]      = None
        self._exec_task:    Optional[asyncio.Task]  = None
        self._program:      list[RobotCmd]  = []
        self._sonar_state   = {"last_fire": 0.0}

        self._connections: list[WebSocket] = []
        self._physics_task: Optional[asyncio.Task] = None
        self._queue_task:   Optional[asyncio.Task] = None
        self._db_session_id: Optional[int] = None

    # ── Жизненный цикл ─────────────────────────────────────────────────────────

    async def start(self):
        try:
            await self.robot.connect()
        except Exception as exc:
            log.warning("[user %d] robot connect failed: %s", self.user_id, exc)
        # Открываем запись DB-сессии для логов
        db = SessionLocal()
        try:
            sess = RobotSession(user_id=self.user_id,
                                simulated=self.cfg.simulation_mode)
            db.add(sess)
            db.commit()
            self._db_session_id = sess.id
            # Восстанавливаем заряд батареи из настроек: «зарядка» привязана
            # к кнопке, а не к жизни сессии. После рестарта/перелогина —
            # тот же уровень.
            us = db.query(UserSettings).filter(UserSettings.user_id == self.user_id).first()
            if us is not None and us.battery_pct is not None:
                self.robot_state.battery = max(0.0, min(100.0, float(us.battery_pct)))
        finally:
            db.close()
        # Загружаем зоны и программу пользователя
        self._load_user_state()
        # Запускаем фоновые задачи
        self._physics_task = asyncio.create_task(self.update_physics())
        self._queue_task   = asyncio.create_task(self._queue_runner())
        log.info("[user %d] session started (sim=%s, host=%s:%d)",
                 self.user_id, self.cfg.simulation_mode, self.cfg.rex_host, self.cfg.rex_port)

    def _save_battery_pct(self):
        """Сохраняет текущий заряд в UserSettings, чтобы пережить рестарт сервера
        и переподключение страницы. «Зарядка» обнуляется только кнопкой."""
        try:
            db = SessionLocal()
            try:
                us = db.query(UserSettings).filter(
                    UserSettings.user_id == self.user_id).first()
                if us is not None:
                    us.battery_pct = float(self.robot_state.battery)
                    db.commit()
            finally:
                db.close()
        except Exception as exc:
            log.warning("[user %d] battery save failed: %s", self.user_id, exc)

    async def stop(self):
        for task in (self._physics_task, self._queue_task):
            if task and not task.done():
                task.cancel()
        if self._exec_task and not self._exec_task.done():
            self._exec_task.cancel()
        try:
            await self.robot.stop()
            await self.robot.disconnect()
        except Exception:
            pass
        # Финальная фиксация заряда — на случай рестарта сервера.
        self._save_battery_pct()
        # Закрываем DB-сессию
        if self._db_session_id:
            from datetime import datetime
            db = SessionLocal()
            try:
                rs = db.query(RobotSession).filter(RobotSession.id == self._db_session_id).first()
                if rs:
                    rs.ended_at = datetime.utcnow()
                    db.commit()
            finally:
                db.close()
        log.info("[user %d] session stopped", self.user_id)

    def _load_user_state(self):
        """Загрузить программу и зоны опасности пользователя из БД."""
        db = SessionLocal()
        try:
            cmds = (db.query(ProgramCommand)
                      .filter(ProgramCommand.user_id == self.user_id)
                      .order_by(ProgramCommand.order_num).all())
            self._program = [RobotCmd(
                id     = next(self._cmd_counter),
                intent = c.intent,
                label  = c.label,
                code   = c.code,
                raw    = c.raw_text,
            ) for c in cmds]

            zones = (db.query(DangerZone)
                       .filter(DangerZone.user_id == self.user_id)
                       .filter(DangerZone.active == True).all())
            for z in zones:
                self.world.add_danger_zone(z.x, z.y, z.radius, z.label,
                                           db_id=z.id,
                                           kind=(z.kind or "danger"))
        finally:
            db.close()

    def _save_program(self):
        db = SessionLocal()
        try:
            db.query(ProgramCommand).filter(ProgramCommand.user_id == self.user_id).delete()
            for i, cmd in enumerate(self._program):
                db.add(ProgramCommand(
                    user_id   = self.user_id,
                    order_num = i,
                    raw_text  = cmd.raw,
                    intent    = cmd.intent,
                    label     = cmd.label,
                    code      = cmd.code,
                ))
            db.commit()
        finally:
            db.close()

    # ── Применение новых настроек (при сохранении в settings) ──────────────────

    async def apply_new_cfg(self, cfg: UserCfg, reconnect: bool):
        self.cfg = cfg
        self.world.width  = cfg.world_w_cm
        self.world.height = cfg.world_h_cm
        if reconnect:
            try:
                await self.robot.disconnect()
            except Exception:
                pass
            self.robot = make_driver(cfg.simulation_mode, cfg.rex_host, cfg.rex_port)
            try:
                await self.robot.connect()
            except Exception as exc:
                log.warning("[user %d] reconnect failed: %s", self.user_id, exc)
        await self.push_world()

    # ── WebSocket-управление ──────────────────────────────────────────────────

    async def add_ws(self, ws: WebSocket):
        await ws.accept()
        self._connections.append(ws)
        await ws.send_json(self._full_state())
        await ws.send_json({
            "type":  "program",
            "lines": self._program_lines(),
            "text":  self._program_text(),
        })

    def remove_ws(self, ws: WebSocket):
        if ws in self._connections:
            self._connections.remove(ws)

    async def broadcast(self, data: dict):
        dead = []
        for ws in self._connections:
            try:
                await ws.send_json(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            if ws in self._connections:
                self._connections.remove(ws)

    def _world_dict(self) -> dict:
        d = self.world.to_dict()
        d["wall_thickness"]    = self.cfg.wall_thickness_cm
        d["sensor_type"]       = self.cfg.sensor_type
        d["sonar_cone_deg"]    = 30.0
        d["sonar_interval_ms"] = self.cfg.sonar_interval_ms
        d["robot_length_cm"]   = self.cfg.robot_length_cm
        d["robot_width_cm"]    = self.cfg.robot_width_cm
        d["start_x_cm"]        = self.cfg.start_x_cm
        d["start_y_cm"]        = self.cfg.start_y_cm
        d["start_heading_deg"] = self.cfg.start_heading_deg
        return d

    def _full_state(self) -> dict:
        return {
            "type":         "full_state",
            "robot":        self._state_dict_with_effective(),
            "world":        self._world_dict(),
            "robot_online": self.robot.connected,
            "simulated":    self.cfg.simulation_mode,
        }

    def _program_lines(self) -> list[str]:
        lines = [c.code for c in self._program]
        if not lines or lines[0] != "reset()":
            lines = ["reset()"] + lines
        lines.append("brake()")
        return lines

    def _python_body_for_cmd(self, cmd: RobotCmd) -> str:
        """Только тело Python-кода команды (без преамбулы)."""
        _, full = self._python_code_for_cmd(cmd)
        sentinel = "# === НАЧАЛО ПРОГРАММЫ ==="
        if sentinel in full:
            return full.split(sentinel, 1)[1].strip()
        return full.strip()

    def _program_text(self) -> str:
        """Полный текст программы для textarea: преамбула Python + блоки команд.
        Каждый блок начинается с маркера `# CMD: <dsl>` — это источник правды
        для парсера при «Запуске». Тело — копируй на реальный робот; чтобы
        удалить команду, убери её # CMD-строку (можно вместе с телом)."""
        parts = [self._python_code_preamble().rstrip()]
        if not self._program:
            parts.append("")
            parts.append("# (программа пуста — выполни команды кнопками управления)")
            return "\n".join(parts) + "\n"
        for cmd in self._program:
            parts.append("")
            # Тело уже содержит свою # CMD-строку (см. _python_code_for_cmd),
            # поэтому здесь маркер не дублируем.
            parts.append(self._python_body_for_cmd(cmd))
        return "\n".join(parts) + "\n"

    def _state_dict_with_effective(self) -> dict:
        """state_to_dict + поле effective_speed_pct: скорость, фактически
        выдаваемая мотором с учётом просадки батареи."""
        d = state_to_dict(self.robot_state)
        factor = self._battery_factor(self.robot_state.battery)
        d["effective_speed_pct"] = self.robot_state.speed * factor
        d["battery_factor"]      = factor
        return d

    async def push_state(self):
        await self.broadcast({
            "type":  "state",
            "robot": self._state_dict_with_effective(),
            "path":  self.world.path_history,
        })

    async def push_message(self, text: str, level: str = "info",
                           code: str = None, description: str = None):
        payload = {"type": "message", "text": text, "level": level}
        if code is not None:        payload["code"] = code
        if description is not None: payload["description"] = description
        await self.broadcast(payload)

    async def push_world(self):
        await self.broadcast({"type": "world", "world": self._world_dict()})

    async def push_queue(self):
        await self.broadcast({
            "type":      "queue",
            "executing": asdict(self._executing) if self._executing else None,
            "pending":   [asdict(c) for c in self._pending],
        })

    async def push_program(self):
        # Отправляем сразу и lines (для совместимости) и text (актуальный формат)
        await self.broadcast({
            "type":  "program",
            "lines": self._program_lines(),
            "text":  self._program_text(),
        })

    # ═══════════════════════════════════════════════════════════════════════════
    # Физические вспомогательные функции
    # ═══════════════════════════════════════════════════════════════════════════

    def _turn_rate_factor(self, speed_pct: float) -> float:
        ref = self.cfg.turn_speed_ref / 100.0
        spd = max(0.05, abs(speed_pct) / 100.0)
        return max(0.25, min(3.0, ref / spd))

    def _wall_dist_cm(self, heading: float = None) -> float:
        s = self.robot_state
        h_rad  = math.radians(heading if heading is not None else s.heading)
        dx, dy = math.sin(h_rad), math.cos(h_rad)
        hw, hh = self.world.width / 2, self.world.height / 2
        t_vals: list[float] = []
        if abs(dx) > 1e-9:
            t = (hw - s.x) / dx if dx > 0 else (-hw - s.x) / dx
            t_vals.append(max(0.0, t))
        if abs(dy) > 1e-9:
            t = (hh - s.y) / dy if dy > 0 else (-hh - s.y) / dy
            t_vals.append(max(0.0, t))
        return round(min(t_vals), 1) if t_vals else 999.0

    def _robot_corners(self) -> list[tuple[float, float]]:
        s = self.robot_state
        body_rad = math.radians(s.heading)
        L  = self.cfg.robot_length_cm
        hW = self.cfg.robot_width_cm / 2.0
        fx, fy = math.sin(body_rad), math.cos(body_rad)
        rx, ry = math.cos(body_rad), -math.sin(body_rad)
        nose_x, nose_y = s.x, s.y
        rear_x, rear_y = nose_x - L * fx, nose_y - L * fy
        return [
            (nose_x - rx * hW, nose_y - ry * hW),
            (nose_x + rx * hW, nose_y + ry * hW),
            (rear_x - rx * hW, rear_y - ry * hW),
            (rear_x + rx * hW, rear_y + ry * hW),
        ]

    def _wall_dist_for_robot(self, heading: float = None) -> float:
        s = self.robot_state
        h_rad  = math.radians(heading if heading is not None else s.heading)
        dx, dy = math.sin(h_rad), math.cos(h_rad)
        hw, hh = self.world.width / 2, self.world.height / 2
        min_t  = float('inf')
        for cx, cy in self._robot_corners():
            t_vals: list[float] = []
            if abs(dx) > 1e-9:
                t = (hw - cx) / dx if dx > 0 else (-hw - cx) / dx
                t_vals.append(max(0.0, t))
            if abs(dy) > 1e-9:
                t = (hh - cy) / dy if dy > 0 else (-hh - cy) / dy
                t_vals.append(max(0.0, t))
            if t_vals:
                min_t = min(min_t, min(t_vals))
        return round(min_t, 1) if min_t != float('inf') else 999.0

    # ── Остановка/ожидание ───────────────────────────────────────────────────

    async def _do_stop(self):
        self._pending.clear()
        if self._exec_task and not self._exec_task.done():
            self._exec_task.cancel()
        self.robot_state.speed     = 0
        self.robot_state.dist_left = 0
        self.robot_state.laser_stop = False
        await self.robot.move(0)
        await self.robot.set_servo_center()

    async def _wait_movement(self, timeout: float = 30.0):
        steps = int(timeout / 0.05)
        for _ in range(steps):
            await asyncio.sleep(0.05)
            if self.robot_state.speed == 0:
                return
        self.robot_state.speed     = 0
        self.robot_state.dist_left = 0
        await self.robot.move(0)

    def _clip_dist_cautious(self, dist_cm: float) -> float:
        s = self.robot_state
        if not s.cautious or not self.world.danger_zones:
            return dist_cm
        hrad = math.radians(s.heading)
        dx   = math.sin(hrad)
        dy   = math.cos(hrad)
        MARGIN   = 20.0
        min_dist = dist_cm
        for zone in self.world.danger_zones:
            zx  = zone.x - s.x
            zy  = zone.y - s.y
            dot = zx * dx + zy * dy
            if dot <= 0:
                continue
            perp   = abs(zx * dy - zy * dx)
            stop_r = zone.radius + MARGIN
            if perp < stop_r:
                min_dist = min(min_dist, max(0.0, dot - stop_r))
        return min_dist

    # ── Манёвр K-turn ────────────────────────────────────────────────────────

    async def _k_turn_n(self, direction: int = 1, steps: int = 3):
        """Разворот на 180° в N приёмов (steps — пользовательский параметр,
        задаёт ритм видимых пар forward-back). Та же 3-фазная схема, что
        и у `_k_turn_to_heading`:
          Фаза 1: N видимых пар дуг (по 90°/steps на дугу)
          Фаза 2: плавный возврат в исходную точку (без snap'а позиции)
          Фаза 3: коррекция курса мелкими дугами, если Фаза 2 его сбила
        Никаких принудительных перемещений координат в конце."""
        s   = self.robot_state
        STEER         = 36
        spd           = self.cfg.move_speed
        steer_ratio   = STEER / 45.0
        trf           = self._turn_rate_factor(spd)
        arc90         = 90.0 * self.cfg.wheel_circ_cm / (self.cfg.heading_per_rot * steer_ratio * trf)
        step_arc      = arc90 / steps
        start_x, start_y = s.x, s.y
        start_heading    = s.heading
        target_heading   = (start_heading + 180) % 360
        TOL_POS = 1.5
        TOL_HDG = 1.5
        MAX_ITER = 5
        try:
            # ── Фаза 1: N видимых пар дуг (как раньше) ────────────────────
            for i in range(steps):
                await self.push_message(f"Разворот {i+1}/{steps}: вперёд…", "info")
                fwd_space = max(10.0, self._wall_dist_cm(s.heading) - self.cfg.wall_thickness_cm - 5.0)
                s.steer     = float(direction * STEER)
                s.speed     = float(spd)
                s.dist_left = min(step_arc, fwd_space)
                await self.robot.set_angle(direction * STEER)
                await self.robot.move(spd)
                await self._wait_movement()
                await asyncio.sleep(0.15)

                await self.push_message(f"Разворот {i+1}/{steps}: назад…", "info")
                bwd_heading = (s.heading + 180) % 360
                bwd_space = max(10.0, self._wall_dist_cm(bwd_heading) - self.cfg.wall_thickness_cm - 5.0)
                s.steer     = float(-direction * STEER)
                s.speed     = float(-spd)
                s.dist_left = min(step_arc, bwd_space)
                await self.robot.set_angle(-direction * STEER)
                await self.robot.move(-spd)
                await self._wait_movement()
                if i < steps - 1:
                    await asyncio.sleep(0.15)

            # ── Фаза 2+3 итеративно — возврат в точку + коррекция курса ──
            for it in range(MAX_ITER):
                pos_err = math.hypot(start_x - s.x, start_y - s.y)
                hdg_err = (target_heading - s.heading + 540.0) % 360.0 - 180.0
                if pos_err < TOL_POS and abs(hdg_err) < TOL_HDG:
                    break

                if pos_err >= TOL_POS:
                    if it == 0:
                        await self.push_message("Возврат в исходную точку…", "info")
                    await self._drive_to_point(start_x, start_y, TOL_POS, spd)

                hdg_err = (target_heading - s.heading + 540.0) % 360.0 - 180.0
                if abs(hdg_err) >= TOL_HDG:
                    await self.push_message(
                        f"Коррекция курса: {hdg_err:+.1f}° (итерация {it+1})", "info")
                    per_arc = 8.0 if it == 0 else 5.0
                    await self._k_turn_arcs(hdg_err, STEER, spd, per_arc_deg=per_arc)
        except asyncio.CancelledError:
            pass
        finally:
            s.speed     = 0
            s.steer     = 0.0
            s.dist_left = 0
            await self.robot.stop()
            await self.robot.set_servo_center()

        # Snap ТОЛЬКО курса
        s.heading = float(target_heading)
        await self.push_state()

    # ── Примитивы движения ───────────────────────────────────────────────────

    async def _run_forward(self, dist_cm: Optional[float], spd: int):
        s = self.robot_state
        if dist_cm and s.cautious:
            dist_cm = self._clip_dist_cautious(dist_cm)
        await self.robot.set_angle(int(s.steer))
        await self.robot.move(spd)
        s.speed     = float(spd)
        s.dist_left = float(dist_cm) if dist_cm else 0.0
        # Если дальномер включён — физика остановит у стены, не доезжая
        # запрошенных см. Если выключен — поедет до коллизии (стенка симулятора).
        s.laser_stop = bool(self.cfg.laser_enabled)
        if dist_cm:
            try:
                await self._wait_movement()
            except asyncio.CancelledError:
                s.speed     = 0
                s.dist_left = 0
                s.laser_stop = False
                await self.robot.move(0)
                raise
            finally:
                s.laser_stop = False

    async def _run_back(self, dist_cm: Optional[float], spd: int):
        s = self.robot_state
        await self.robot.set_angle(int(s.steer))
        await self.robot.move(-spd)
        s.speed     = float(-spd)
        s.dist_left = float(dist_cm) if dist_cm else 0.0
        s.laser_stop = bool(self.cfg.laser_enabled)
        if dist_cm:
            try:
                await self._wait_movement()
            except asyncio.CancelledError:
                s.speed     = 0
                s.dist_left = 0
                s.laser_stop = False
                await self.robot.move(0)
                raise
            finally:
                s.laser_stop = False

    async def _run_forward_to_wall(self, spd: int):
        s = self.robot_state
        zone_dist = self._clip_dist_cautious(9999.0) if s.cautious else 9999.0
        s.dist_left  = zone_dist if zone_dist < 9900.0 else 0.0
        s.laser_stop = True
        await self.robot.move(spd)
        s.speed = float(spd)
        try:
            await self._wait_movement(timeout=60.0)
        except asyncio.CancelledError:
            raise
        finally:
            s.laser_stop = False
            s.speed      = 0
            s.dist_left  = 0
            await self.robot.move(0)

    async def _run_backward_to_wall(self, spd: int):
        s = self.robot_state
        zone_dist = self._clip_dist_cautious(9999.0) if s.cautious else 9999.0
        s.dist_left  = zone_dist if zone_dist < 9900.0 else 0.0
        s.laser_stop = True
        await self.robot.move(-spd)
        s.speed = float(-spd)
        try:
            await self._wait_movement(timeout=60.0)
        except asyncio.CancelledError:
            raise
        finally:
            s.laser_stop = False
            s.speed      = 0
            s.dist_left  = 0
            await self.robot.move(0)

    async def _run_steer_delta(self, delta_deg: float):
        s = self.robot_state
        MAX = float(self.cfg.turn_angle)
        new_steer = max(-MAX, min(MAX, s.steer + delta_deg))
        s.steer   = new_steer
        await self.robot.set_angle(int(new_steer))

    async def _run_set_course(self, target_deg: int):
        s = self.robot_state
        TOLERANCE   = 5.0
        STEER       = 36
        spd         = self.cfg.move_speed
        steer_ratio = STEER / 45.0
        trf         = self._turn_rate_factor(spd)
        arc90       = 90.0 * self.cfg.wheel_circ_cm / (self.cfg.heading_per_rot * steer_ratio * trf)
        completed   = False
        try:
            for _ in range(20):
                diff = (target_deg - s.heading + 180) % 360 - 180
                if abs(diff) < TOLERANCE:
                    completed = True
                    break
                direction   = 1 if diff > 0 else -1
                frac        = min(abs(diff), 90) / 90.0
                s.steer     = float(direction * STEER)
                s.speed     = float(spd)
                s.dist_left = arc90 * frac
                await self.robot.set_angle(direction * STEER)
                await self.robot.move(spd)
                await self._wait_movement()
                await asyncio.sleep(0.1)
            completed = True
        except asyncio.CancelledError:
            pass
        finally:
            s.speed     = 0
            s.steer     = 0.0
            s.dist_left = 0
            await self.robot.stop()
            await self.robot.set_servo_center()
        if completed:
            s.heading = float(target_deg % 360)
        await self.push_state()

    # ── Сложные манёвры (круг / восьмёрка / спираль / синусоида) ────────────

    async def _arc_at_steer(self, steer_deg: float, sweep_deg: float, spd: int,
                              backward: bool = False):
        """Едет дугой при заданном угле руля до изменения курса на sweep_deg.
        backward=True — едет ЗАДОМ (steer тот же, скорость противоположная).

        Дальномер ВКЛЮЧЁН — если корпус подходит близко к стене, дуга
        прервётся для безопасности. K-turn внутри `_run_goto` потом сам
        проверит, дошли ли до цели, и при необходимости отъедет назад
        и попробует снова."""
        if steer_deg == 0 or sweep_deg <= 0:
            return
        s = self.robot_state
        steer_ratio = abs(steer_deg) / 45.0
        trf = self._turn_rate_factor(spd)
        arc = abs(sweep_deg) * self.cfg.wheel_circ_cm / (
              self.cfg.heading_per_rot * steer_ratio * trf)
        drive_spd = -spd if backward else spd
        s.steer     = float(steer_deg)
        s.speed     = float(drive_spd)
        s.dist_left = arc
        s.laser_stop = bool(self.cfg.laser_enabled)
        await self.robot.set_angle(int(steer_deg))
        await self.robot.move(drive_spd)
        try:
            await self._wait_movement(timeout=60.0)
        except asyncio.CancelledError:
            raise
        finally:
            s.laser_stop = False

    async def _run_circle(self, direction: int = -1):
        """Один полный круг (2π) при максимальном угле руля.
        По умолчанию ПРОТИВ часовой стрелки (CCW, математическое
        положительное направление, от 0 до 2π). direction=+1 — по часовой."""
        s = self.robot_state
        spd = self.cfg.move_speed
        steer = direction * self.cfg.turn_angle
        try:
            await self._arc_at_steer(steer, 360.0, spd)
        except asyncio.CancelledError:
            pass
        finally:
            s.steer = 0.0
            s.speed = 0
            s.dist_left = 0
            await self.robot.stop()
            await self.robot.set_servo_center()
        await self.push_state()

    async def _run_figure_eight(self, direction: int = -1):
        """Восьмёрка: первый круг ПРОТИВ часовой (CCW), второй — по часовой.
        direction=+1 — наоборот, начать по часовой."""
        s = self.robot_state
        spd = self.cfg.move_speed
        steer = self.cfg.turn_angle
        try:
            await self.push_message("Восьмёрка: круг 1/2…", "info")
            await self._arc_at_steer(direction * steer, 360.0, spd)
            await asyncio.sleep(0.2)
            await self.push_message("Восьмёрка: круг 2/2…", "info")
            await self._arc_at_steer(-direction * steer, 360.0, spd)
        except asyncio.CancelledError:
            pass
        finally:
            s.steer = 0.0
            s.speed = 0
            s.dist_left = 0
            await self.robot.stop()
            await self.robot.set_servo_center()
        await self.push_state()

    async def _run_spiral(self, direction: int = 1, outward: bool = True):
        """Плавная спираль: робот непрерывно едет, угол руля линейно
        интерполируется между крайними значениями за 2 полных оборота.
        Радиусы выбраны так, чтобы спираль помещалась в обычное поле:
        outward=True  — руль 36° → 24° (от самого тугого до среднего)
        outward=False — руль 24° → 36° (от среднего до самого тугого)
        Прогресс отслеживаем по реально набранной развёртке курса (не по времени)."""
        s = self.robot_state
        spd = self.cfg.move_speed
        if outward:
            steer_start, steer_end = 36.0, 24.0
        else:
            steer_start, steer_end = 24.0, 36.0
        target_sweep_abs = 2 * 360.0  # два полных оборота

        await self.push_message(
            f"Спираль {'наружу' if outward else 'внутрь'}: "
            f"руль {steer_start:.0f}° → {steer_end:.0f}° за 2 оборота…",
            "info")

        # Старт: ставим начальный руль и едем
        s.steer     = float(direction * steer_start)
        s.speed     = float(spd)
        s.dist_left = 0.0   # ехать «бесконечно», останавливаем сами по курсу
        await self.robot.set_angle(int(direction * steer_start))
        await self.robot.move(spd)

        prev_heading   = s.heading
        sweep_abs      = 0.0
        last_msg_pct   = 0
        try:
            while sweep_abs < target_sweep_abs:
                await asyncio.sleep(0.1)  # 10 Гц обновления руля
                # Накопление развёртки курса (модуль, без учёта направления вращения)
                d = (s.heading - prev_heading + 540.0) % 360.0 - 180.0
                sweep_abs += abs(d)
                prev_heading = s.heading

                t = min(1.0, sweep_abs / target_sweep_abs)
                steer_now = steer_start + (steer_end - steer_start) * t
                new_steer = float(direction * steer_now)
                if int(new_steer) != int(s.steer):
                    s.steer = new_steer
                    await self.robot.set_angle(int(new_steer))

                # Прогресс — не чаще раза в 25%
                pct = int(t * 100)
                if pct >= last_msg_pct + 25 and pct < 100:
                    last_msg_pct = (pct // 25) * 25
                    await self.push_message(
                        f"Спираль: {pct}%  (руль {steer_now:.1f}°)", "info")

                # Защита от зависания: если робот уперся в стену и не движется,
                # курс не меняется → sweep_abs не растёт. Выходим через таймаут.
                if s.speed == 0:
                    await self.push_message("Спираль прервана: робот остановился.", "warning")
                    break
        except asyncio.CancelledError:
            pass
        finally:
            s.steer     = 0.0
            s.speed     = 0
            s.dist_left = 0
            await self.robot.stop()
            await self.robot.set_servo_center()
        await self.push_state()

    async def _run_bypass(self, start_dir: int = +1, max_steer: float = 36.0):
        """Объезд препятствия — одна S-волна на 2π:
          start_dir=+1 — сначала вправо (объезд СПРАВА от препятствия),
          start_dir=-1 — сначала влево  (объезд СЛЕВА).
        Робот заканчивает движение в исходном курсе и на исходной линии."""
        s = self.robot_state
        spd = self.cfg.move_speed
        swing = 30.0  # размах курса в каждую сторону
        side = "справа" if start_dir > 0 else "слева"
        # Фазы 0,3 → знак start_dir; фазы 1,2 → противоположный
        try:
            for phase in range(4):
                sign = start_dir if phase in (0, 3) else -start_dir
                await self.push_message(
                    f"Объезд {side}: четверть {phase+1}/4 (руль {sign*max_steer:+.0f}°)",
                    "info")
                await self._arc_at_steer(sign * max_steer, swing, spd)
                await asyncio.sleep(0.05)
        except asyncio.CancelledError:
            pass
        finally:
            s.steer = 0.0
            s.speed = 0
            s.dist_left = 0
            await self.robot.stop()
            await self.robot.set_servo_center()
        await self.push_state()

    # ── Перейти в координату / домой ────────────────────────────────────────

    async def _run_goto(self, target_x: float, target_y: float):
        """Заход в точку с проверкой и повторными попытками.

        Алгоритм:
          1) ПОВОРОТ: 3-дуговой симметричный K-turn — выставить курс на цель.
          2) ПРЯМАЯ: проехать distance см.
          3) ПРОВЕРКА: если робот не дошёл (дальномер прервал движение или
             K-turn упёрся в стену) — отъехать назад на 30 см, чтобы выйти
             из неудобного положения, и повторить с новой исходной позиции.
          Максимум 3 попытки. Каждое отклонение и попытка сообщаются оператору."""
        s = self.robot_state
        TOL_CM       = 5.0
        BACKOFF_CM   = 30.0
        MAX_ATTEMPTS = 3

        for attempt in range(1, MAX_ATTEMPTS + 1):
            dx = float(target_x) - s.x
            dy = float(target_y) - s.y
            distance = math.hypot(dx, dy)

            if distance < TOL_CM:
                if attempt > 1:
                    await self.push_message(
                        f"✓ Дошёл до ({target_x:.0f}, {target_y:.0f}) "
                        f"с {attempt}-й попытки.", "success")
                return

            target_heading = math.degrees(math.atan2(dx, dy)) % 360
            if attempt == 1:
                await self.push_message(
                    f"В точку ({target_x:.0f}, {target_y:.0f}): "
                    f"курс {target_heading:.0f}°, прямая {distance:.0f} см.",
                    "info")
            else:
                await self.push_message(
                    f"⚠ Попытка {attempt}/{MAX_ATTEMPTS}: "
                    f"осталось {distance:.0f} см до ({target_x:.0f}, {target_y:.0f}), "
                    f"новый курс {target_heading:.0f}°.", "warning")

            # Фаза 1: повернуться лицом к цели
            await self._k_turn_to_heading(target_heading)

            # Фаза 2: проехать прямой (дальномер при необходимости остановит у стены)
            dx2 = float(target_x) - s.x
            dy2 = float(target_y) - s.y
            dist2 = math.hypot(dx2, dy2)
            if dist2 > TOL_CM:
                await self._run_forward(dist2, self.cfg.move_speed)

            # Проверка достижения
            dx3 = float(target_x) - s.x
            dy3 = float(target_y) - s.y
            dist3 = math.hypot(dx3, dy3)
            if dist3 < TOL_CM:
                if attempt > 1:
                    await self.push_message(
                        f"✓ Дошёл до ({target_x:.0f}, {target_y:.0f}).", "success")
                return

            # Не дошли — стена/препятствие. Отъезжаем на безопасную дистанцию,
            # чтобы при следующей попытке был свободный заход с новой позиции.
            if attempt < MAX_ATTEMPTS:
                await self.push_message(
                    f"📍 Не дошёл до цели (отклонение {dist3:.0f} см). "
                    f"Отъезжаю назад на {BACKOFF_CM:.0f} см и пробую ещё раз.",
                    "info")
                await self._run_back(BACKOFF_CM, self.cfg.move_speed)

        # Все попытки исчерпаны
        final_dist = math.hypot(target_x - s.x, target_y - s.y)
        await self.push_message(
            f"⚠ Не удалось точно прийти в ({target_x:.0f}, {target_y:.0f}) "
            f"за {MAX_ATTEMPTS} попыток. Текущая позиция "
            f"({s.x:.0f}, {s.y:.0f}), отклонение {final_dist:.0f} см. "
            f"Вмешайтесь вручную или выберите промежуточную точку.",
            "warning")

    async def _run_home(self):
        """Возврат в стартовую точку — это тот же `goto` с координатами
        из настроек (`START_X_CM`, `START_Y_CM`)."""
        sx, sy = float(self.cfg.start_x_cm), float(self.cfg.start_y_cm)
        await self.push_message(f"🏠 Домой: точка ({sx:.0f}, {sy:.0f}).", "info")
        await self._run_goto(sx, sy)

    # ── Развернуться лицом к указанному курсу (на месте, K-turn) ────────────

    async def _k_turn_to_heading(self, target_deg: float):
        """Многошаговый разворот на месте до target_deg.

        Стратегия трёх фаз — без рывков:
          Фаза 1: серия коротких симметричных дуг (forward+back) с малым
                  per_arc (~10°). Чем мельче дуга, тем меньше дрейф
                  позиции за одну пару, и сумма дрейфа минимальна.
          Фаза 2: если позиция всё-таки уехала больше чем на TOLERANCE —
                  пропорциональный регулятор довозит робот в исходную
                  точку (за счёт этого курс может сбиться).
          Фаза 3: если курс сбился больше PORT_TOL — короткий доразворот
                  такими же мелкими дугами, чтобы вернуть точный курс.
        Никаких принудительных snap-ов координат."""
        s = self.robot_state
        target_deg = float(target_deg) % 360
        diff = (target_deg - s.heading + 540.0) % 360.0 - 180.0
        if abs(diff) < 3.0:
            s.heading = target_deg
            await self.push_state()
            return

        spd       = self.cfg.move_speed
        STEER     = 36
        direction = +1 if diff > 0 else -1

        # ── Аналитический 3-дуговой паттерн (Reeds-Shepp) ────────────────
        # Симметричный K-turn forward(α) – backward(β) – forward(α).
        # Условие точного возврата в исходную точку:
        #   2α + β = |Δh|  и  sin(β/2) = sin(|Δh|/2) / 2
        # Решение существует для любого |Δh| ∈ (0°, 180°].
        half_rad   = math.radians(abs(diff) / 2.0)
        s_half     = math.sin(half_rad)
        # sin(β/2) = sin(Δh/2)/2 → β = 2·arcsin(sin(Δh/2)/2)
        beta_rad   = 2.0 * math.asin(s_half / 2.0)
        beta_deg   = math.degrees(beta_rad)
        alpha_deg  = (abs(diff) - beta_deg) / 2.0

        try:
            await self.push_message(
                f"3-дуговой K-turn: α={alpha_deg:.1f}°, β={beta_deg:.1f}°, "
                f"α={alpha_deg:.1f}° (Δh={diff:+.0f}°)", "info")
            # Дуга 1: вперёд, рулём в нужную сторону
            if alpha_deg > 0.5:
                await self._arc_at_steer(direction * STEER, alpha_deg, spd)
            # Дуга 2: назад, ОБРАТНЫЙ руль (но курс продолжает крутиться
            # в ту же сторону, что и в дуге 1, благодаря смене знака v и steer)
            await self._arc_at_steer(-direction * STEER, beta_deg, spd, backward=True)
            # Дуга 3: вперёд, тот же руль, что дуга 1
            if alpha_deg > 0.5:
                await self._arc_at_steer(direction * STEER, alpha_deg, spd)
        except asyncio.CancelledError:
            pass
        finally:
            s.speed     = 0
            s.steer     = 0.0
            s.dist_left = 0
            await self.robot.stop()
            await self.robot.set_servo_center()

        # Финальный мягкий снэп ТОЛЬКО курса (без позиции)
        s.heading = target_deg
        await self.push_state()

    async def _k_turn_arcs(self, total_deg: float, STEER: int, spd: int,
                            per_arc_deg: float = 10.0):
        """Серия коротких пар дуг (forward+back) для разворота на total_deg.
        Каждая дуга — per_arc_deg градусов курса; пары симметричны и почти
        не двигают позицию. Знак total_deg задаёт направление вращения."""
        s = self.robot_state
        if abs(total_deg) < 1.0:
            return
        direction = 1 if total_deg > 0 else -1
        # Делим на чётное число дуг, чтобы каждая пара была симметричной
        total_arcs = max(2, int(math.ceil(abs(total_deg) / per_arc_deg)))
        if total_arcs % 2 == 1:
            total_arcs += 1
        per_arc_actual = abs(total_deg) / total_arcs
        steps = total_arcs // 2

        steer_ratio = STEER / 45.0
        trf         = self._turn_rate_factor(spd)
        arc_dist    = per_arc_actual * self.cfg.wheel_circ_cm / (
                      self.cfg.heading_per_rot * steer_ratio * trf)

        for i in range(steps):
            # Forward
            fwd_space = max(arc_dist + 5.0,
                            self._wall_dist_cm(s.heading) - self.cfg.wall_thickness_cm - 2.0)
            s.steer     = float(direction * STEER)
            s.speed     = float(spd)
            s.dist_left = min(arc_dist, fwd_space)
            await self.robot.set_angle(direction * STEER)
            await self.robot.move(spd)
            await self._wait_movement()
            # Backward
            bwd = (s.heading + 180) % 360
            bwd_space = max(arc_dist + 5.0,
                            self._wall_dist_cm(bwd) - self.cfg.wall_thickness_cm - 2.0)
            s.steer     = float(-direction * STEER)
            s.speed     = float(-spd)
            s.dist_left = min(arc_dist, bwd_space)
            await self.robot.set_angle(-direction * STEER)
            await self.robot.move(-spd)
            await self._wait_movement()

    async def _drive_to_point(self, tx: float, ty: float, tol_cm: float, spd: int,
                                timeout: float = 15.0):
        """Подъезжает к (tx, ty) с допуском tol_cm. Сама выбирает направление —
        ВПЕРЁД если цель находится впереди (в направлении носа), НАЗАД если сзади.
        Это важно для финальной точной подстановки в исходную точку: если К-turn
        оставил нас слегка впереди старта, разумнее сдать назад, не разворачиваясь.
        timeout — максимальное время поездки (по умолчанию 15 с; для дальних
        перемещений вызывающий код передаёт пропорциональное расстоянию значение)."""
        s = self.robot_state
        # Выбираем направление по проекции цели на ось носа
        h_rad     = math.radians(s.heading)
        forward_x = math.sin(h_rad)
        forward_y = math.cos(h_rad)
        dx0 = tx - s.x
        dy0 = ty - s.y
        forward_proj = forward_x * dx0 + forward_y * dy0   # > 0 цель впереди

        sgn = +1 if forward_proj >= 0 else -1
        STEER_GAIN = 1.5
        MAX        = float(self.cfg.turn_angle)

        s.speed     = float(sgn * spd)
        s.dist_left = 0.0
        await self.robot.move(sgn * spd)

        loop       = asyncio.get_event_loop()
        deadline   = loop.time() + timeout
        # Защита от перелёта мимо цели: если расстояние начало РАСТИ —
        # значит проехали мимо, надо остановиться и перерасчитать.
        prev_dist  = math.hypot(dx0, dy0)
        growing_ticks = 0
        while loop.time() < deadline:
            dx = tx - s.x
            dy = ty - s.y
            cur_dist = math.hypot(dx, dy)
            if cur_dist < tol_cm:
                break
            # Защита от перелёта: расстояние растёт 4 тика подряд НА ОЩУТИМУЮ
            # величину И мы достаточно далеко от цели. Близко к цели мелкие
            # колебания дистанции из-за подруливания НЕ должны прерывать
            # доезд — иначе робот не сможет дотянуться до точки.
            if cur_dist > tol_cm * 4 and cur_dist > prev_dist + 1.5:
                growing_ticks += 1
                if growing_ticks >= 4:
                    break
            else:
                growing_ticks = 0
            prev_dist = cur_dist

            # Адаптивная скорость: замедляемся при подходе к цели,
            # чтобы за один тик (0.1 с) не перелететь tol_cm.
            # На полной скорости 32 см/с робот за тик проходит ~3.2 см —
            # этого достаточно чтобы промахнуться мимо точки с tol_cm=2.
            if cur_dist < 25.0:
                # Линейно от 25 см → spd, от 0 см → 15% (минимум для движения)
                drive_spd = max(15, int(spd * cur_dist / 25.0))
            else:
                drive_spd = spd
            target_speed = float(sgn * drive_spd)
            if abs(s.speed - target_speed) > 1.0:
                s.speed = target_speed
                await self.robot.move(int(target_speed))
            elif s.speed == 0:                        # уперся в стену — толкнём ещё раз
                s.speed = target_speed
                await self.robot.move(int(target_speed))

            if sgn > 0:
                # ВПЕРЁД: курс должен указывать НА цель
                nav_h = math.degrees(math.atan2(dx, dy)) % 360
                diff  = (nav_h - s.heading + 180) % 360 - 180
                steer = max(-MAX, min(MAX, diff * STEER_GAIN))
            else:
                # НАЗАД: курс должен указывать ОТ цели (нос смотрит против движения).
                # Плюс инвертируем знак руля — при реверсе физика поворота зеркальна.
                nav_h = (math.degrees(math.atan2(dx, dy)) + 180) % 360
                diff  = (nav_h - s.heading + 180) % 360 - 180
                steer = -max(-MAX, min(MAX, diff * STEER_GAIN))
            s.steer = steer
            await self.robot.set_angle(int(steer))
            await asyncio.sleep(0.1)
        # Останавливаем перед возможной фазой коррекции курса
        s.speed     = 0
        s.dist_left = 0
        s.steer     = 0.0
        await self.robot.move(0)
        await self.robot.set_servo_center()
        await asyncio.sleep(0.15)

    async def _run_face_cardinal(self, deg: float, label: str):
        await self.push_message(
            f"🧭 Развернуться лицом к {label} (курс {deg:.0f}°).", "info")
        await self._k_turn_to_heading(deg)

    async def _run_mark_danger(self,
                                target_x: Optional[float] = None,
                                target_y: Optional[float] = None,
                                radius:   Optional[float] = None,
                                db: Session = None):
        """Поставить красную зону обстановки.
        Если координаты не заданы — берётся текущая позиция робота
        (старое поведение: «опасно прямо здесь»).
        Если заданы — зона ставится в (X, Y) БЕЗ движения робота:
        это «константа обстановки», задаваемая до запуска программы."""
        s = self.robot_state
        if target_x is None or target_y is None:
            x = s.x; y = s.y
            # читаем дальномер, как раньше (для UI «впереди» в момент пометки)
            dist = await self.robot.get_laser()
            if dist and dist > 0:
                s.laser_dist = dist
        else:
            x = float(target_x); y = float(target_y)
        r = float(radius) if radius is not None else float(self.cfg.danger_zone_radius)
        zone = self.world.add_danger_zone(x, y,
                                          radius=r,
                                          label="Зона опасности",
                                          kind="danger")
        if db:
            dz = DangerZone(user_id=self.user_id, label=zone.label,
                            x=zone.x, y=zone.y, radius=zone.radius,
                            kind="danger")
            db.add(dz)
            db.commit()
            zone.db_id = dz.id
        await self.push_world()

    async def _run_pause(self, seconds: float):
        """Робот стоит на месте `seconds` секунд, затем продолжает выполнение
        очереди. Во время паузы движение остановлено, руль не трогаем."""
        s = self.robot_state
        secs = max(0.1, min(60.0, float(seconds)))
        s.speed = 0
        s.dist_left = 0
        try:
            await self.robot.move(0)
        except Exception:
            pass
        await self.push_message(f"⏸ Пауза {secs:g} сек.", "info")
        # Кооперативная задержка — отменяемая через _do_stop.
        await asyncio.sleep(secs)
        await self.push_state()

    async def _run_recharge(self):
        """Полная зарядка батареи робота. В симуляторе мгновенная;
        для реального робота — заглушка."""
        s = self.robot_state
        s.battery = 100.0
        self._save_battery_pct()
        await self.push_message("🔋 Батарея заряжена до 100%.", "success")

    async def _run_remove_zone(self, target_x: Optional[float] = None,
                               target_y: Optional[float] = None,
                               db: Session = None) -> int:
        """Удалить зону (любого типа), в которую попадает точка (X, Y).
        Если координаты не заданы — берётся текущая позиция робота.
        Возвращает число удалённых зон."""
        s = self.robot_state
        x = float(target_x) if target_x is not None else s.x
        y = float(target_y) if target_y is not None else s.y
        removed = self.world.remove_zones_at(x, y)
        if not removed:
            await self.push_message(
                f"В точке ({x:.0f}, {y:.0f}) зон не найдено.", "warning")
            return 0
        if db:
            ids = [z.db_id for z in removed if z.db_id is not None]
            if ids:
                (db.query(DangerZone)
                   .filter(DangerZone.id.in_(ids))
                   .update({"active": False}, synchronize_session=False))
                db.commit()
        await self.push_world()
        return len(removed)

    async def _run_set_algorithm_zone(self, target_x: float, target_y: float,
                                      radius: Optional[float] = None,
                                      db: Session = None):
        """Робот доезжает до (X, Y) и помечает там жёлтую пунктирную зону —
        это часть алгоритма (не зона обстановки). Радиус по умолчанию из cfg."""
        s = self.robot_state
        await self.push_message(
            f"📍 Установить алгоритмическую зону в ({target_x:.0f}, {target_y:.0f}).",
            "info")
        # 1) Доехать до точки
        await self._run_goto(float(target_x), float(target_y))
        # 2) Поставить жёлтую пунктирную зону в текущей позиции робота
        r = float(radius) if radius is not None else float(self.cfg.danger_zone_radius)
        zone = self.world.add_danger_zone(s.x, s.y,
                                          radius=r,
                                          label="Зона алгоритма",
                                          kind="algorithm")
        if db:
            dz = DangerZone(user_id=self.user_id, label=zone.label,
                            x=zone.x, y=zone.y, radius=zone.radius,
                            kind="algorithm")
            db.add(dz)
            db.commit()
            zone.db_id = dz.id
        await self.push_world()

    async def _run_reset(self, db: Session = None):
        s = self.robot_state
        self.world.clear_danger_zones()
        self.world.clear_path()
        s.x         = float(self.cfg.start_x_cm)
        s.y         = float(self.cfg.start_y_cm)
        s.heading   = float(self.cfg.start_heading_deg) % 360
        s.speed     = 0
        s.steer     = 0.0
        s.dist_left = 0
        s.mode      = "normal"
        s.cautious  = False
        s.light_color = (0, 0, 0)
        await self.robot.stop()
        await self.robot.set_servo_center()
        if db:
            db.query(DangerZone).filter(DangerZone.user_id == self.user_id).update({"active": False})
            db.commit()
        await self.push_world()

    # ── Сборка команды и Python-кода ─────────────────────────────────────────

    def _build_cmd(self, intent: str, raw: str) -> Optional[RobotCmd]:
        cmd_id = next(self._cmd_counter)
        c = self.cfg
        if intent == "forward":
            dist  = nlu.extract_distance(raw)
            spd   = nlu.extract_speed(raw, c.move_speed)
            code  = f"forward({dist})" if dist else "forward()"
            label = f"Вперёд {dist} см ({spd}%)" if dist else f"Вперёд ({spd}%)"
        elif intent == "back":
            dist  = nlu.extract_distance(raw)
            spd   = nlu.extract_speed(raw, c.move_speed)
            code  = f"back({dist})" if dist else "back()"
            label = f"Назад {dist} см ({spd}%)" if dist else f"Назад ({spd}%)"
        elif intent == "forward_to_wall":
            spd   = nlu.extract_speed(raw, c.move_speed)
            code  = "forward_to_wall()"
            label = f"Вперёд до упора ({spd}%)"
        elif intent == "backward_to_wall":
            spd   = nlu.extract_speed(raw, c.move_speed)
            code  = "backward_to_wall()"
            label = f"Назад до упора ({spd}%)"
        elif intent == "brake":
            code, label = "brake()", "Тормоз"
        elif intent == "stop":
            code, label = "stop()", "Стоп"
        elif intent == "steer_right":
            delta = nlu.extract_angle(raw) or c.turn_angle
            code, label = f"steer(+{delta})", f"Руль вправо {delta}°"
        elif intent == "steer_left":
            delta = nlu.extract_angle(raw) or c.turn_angle
            code, label = f"steer(-{delta})", f"Руль влево {delta}°"
        elif intent == "steer_right_small":
            code, label = f"steer(+{nlu.SMALL_STEER_DEG})", f"Правее {nlu.SMALL_STEER_DEG}°"
        elif intent == "steer_left_small":
            code, label = f"steer(-{nlu.SMALL_STEER_DEG})", f"Левее {nlu.SMALL_STEER_DEG}°"
        elif intent == "steer_center":
            code, label = "steer(0)", "Руль прямо"
        elif intent == "set_speed":
            spd = nlu.extract_speed(raw, c.move_speed)
            code, label = f"set_speed({spd})", f"Скорость {spd}%"
        elif intent == "turn_around":
            n = nlu.norm(raw)
            side = "влево" if any(w in n for w in ("налево", "влево", "против")) else "вправо"
            code, label = "turn_around()", f"Разворот {side}"
        elif intent == "turn_around_place":
            steps = nlu.extract_kturn_steps(raw)
            code, label = f"turn_around_place({steps})", f"Разворот на месте за {steps} шагов"
        elif intent == "circle":
            n = nlu.norm(raw)
            cw = any(w in n for w in ("направо", "вправо", "по часовой"))
            side = "по часовой" if cw else "против часовой"
            code, label = "circle()", f"Круг {side}"
        elif intent == "figure_eight":
            code, label = "figure_eight()", "Восьмёрка"
        elif intent == "spiral_out":
            code, label = "spiral_out()", "Спираль наружу (3 круга)"
        elif intent == "spiral_in":
            code, label = "spiral_in()", "Спираль внутрь (3 круга)"
        elif intent == "bypass_right":
            code, label = "bypass_right()", "Объезд справа"
        elif intent == "bypass_left":
            code, label = "bypass_left()",  "Объезд слева"
        elif intent == "goto":
            xy = nlu.extract_coordinates(raw)
            if xy is None: return None
            tx, ty = xy
            code, label = f"goto({tx:.0f}, {ty:.0f})", f"В точку ({tx:.0f}, {ty:.0f})"
        elif intent == "home":
            code, label = "home()", "🏠 Домой"
        elif intent == "face_n":
            code, label = "face_n()",  "Лицом на С"
        elif intent == "face_ne":
            code, label = "face_ne()", "Лицом на СВ"
        elif intent == "face_e":
            code, label = "face_e()",  "Лицом на В"
        elif intent == "face_se":
            code, label = "face_se()", "Лицом на ЮВ"
        elif intent == "face_s":
            code, label = "face_s()",  "Лицом на Ю"
        elif intent == "face_sw":
            code, label = "face_sw()", "Лицом на ЮЗ"
        elif intent == "face_w":
            code, label = "face_w()",  "Лицом на З"
        elif intent == "face_nw":
            code, label = "face_nw()", "Лицом на СЗ"
        elif intent == "set_course":
            target = nlu.extract_course(raw)
            if target is None: return None
            code, label = f"set_course({target})", f"Курс {target}°"
        elif intent == "mark_danger":
            xy = nlu.extract_coordinates(raw)
            r  = nlu.extract_radius(raw)
            if xy is not None:
                tx, ty = xy
                if r is not None:
                    code  = f"mark_danger({tx:.0f}, {ty:.0f}, {r:.0f})"
                    label = f"⚠ Опасная зона ({tx:.0f}, {ty:.0f}) r={r:.0f}"
                else:
                    code  = f"mark_danger({tx:.0f}, {ty:.0f})"
                    label = f"⚠ Опасная зона ({tx:.0f}, {ty:.0f})"
            else:
                code, label = "mark_danger()", "⚠ Опасная зона под роботом"
        elif intent == "pause":
            secs = nlu.extract_pause_seconds(raw)
            code, label = f"pause({secs:g})", f"⏸ Пауза {secs:g} с"
        elif intent == "set_algorithm_zone":
            xy = nlu.extract_coordinates(raw)
            if xy is None:
                return None
            tx, ty = xy
            r = nlu.extract_radius(raw)
            if r is not None:
                code  = f"set_algorithm_zone({tx:.0f}, {ty:.0f}, {r:.0f})"
                label = f"📍 Зона алгоритма ({tx:.0f}, {ty:.0f}) r={r:.0f}"
            else:
                code  = f"set_algorithm_zone({tx:.0f}, {ty:.0f})"
                label = f"📍 Зона алгоритма ({tx:.0f}, {ty:.0f})"
        elif intent == "remove_zone":
            xy = nlu.extract_coordinates(raw)
            if xy is not None:
                tx, ty = xy
                code  = f"remove_zone({tx:.0f}, {ty:.0f})"
                label = f"✕ Убрать зону в ({tx:.0f}, {ty:.0f})"
            else:
                code  = "remove_zone()"
                label = "✕ Убрать зону под роботом"
        elif intent == "mode_inspector":
            code, label = "mode(inspector)", "Режим инспектор"
        elif intent == "mode_cautious":
            code, label = "mode(cautious)", "Режим осторожно"
        elif intent == "path_show":
            code, label = "path_show()", "Показать путь"
        elif intent == "path_hide":
            code, label = "path_hide()", "Скрыть путь"
        elif intent == "reset":
            code, label = "reset()", "Новое поле"
        elif intent == "report_pos":
            code, label = "report_pos()", "Где робот"
        elif intent == "report_status":
            code, label = "report_status()", "📊 Статус робота"
        elif intent == "light_on":
            code, label = "light_on()", "Свет включить"
        elif intent == "light_off":
            code, label = "light_off()", "Свет выключить"
        elif intent == "light_color":
            color = nlu.extract_color(raw) or LIGHT_DEFAULT_COLOR
            code, label = f"light_color({color})", f"Цвет подсветки {color}"
        elif intent == "recharge":
            code, label = "recharge()", "🔋 Зарядить батарею"
        else:
            return None
        return RobotCmd(id=cmd_id, intent=intent, label=label, code=code, raw=raw)

    def _python_code_preamble(self) -> str:
        c = self.cfg
        return ("""# Python-скрипт для реального робота
# Предполагается, что объект `robot` уже инициализирован.
DEFAULT_SPEED = %d
DEFAULT_TURN_ANGLE = %d
SPEED_CM_PER_S_AT_100 = %.1f
LIGHT_INDEX = %d
LIGHT_COUNT = %d
LIGHT_DEFAULT_COLOR = %s

# Стартовая точка робота на поле (используется при сбросе/калибровке).
START_X_CM = %.1f
START_Y_CM = %.1f
START_HEADING_DEG = %.1f


def duration_for_distance(distance_cm, power_pct=DEFAULT_SPEED):
    speed_cm_s = SPEED_CM_PER_S_AT_100 * power_pct / 100.0
    return max(0.1, distance_cm / speed_cm_s)


def report_position():
    return robot.get_angle()["Z"]


# === НАЧАЛО ПРОГРАММЫ ===
""" % (c.move_speed, c.turn_angle, c.speed_at_100,
       LIGHT_INDEX, LIGHT_COUNT, str(LIGHT_DEFAULT_COLOR),
       c.start_x_cm, c.start_y_cm, c.start_heading_deg))

    def _python_code_for_cmd(self, cmd: RobotCmd) -> tuple[str, str]:
        raw = cmd.raw
        intent = cmd.intent
        c = self.cfg
        default_speed = c.move_speed
        dist = nlu.extract_distance(raw)
        cur_steer = int(self.robot_state.steer)
        # Маркер `# CMD: <dsl>` — единственный источник правды для парсера на «Запуск».
        # Тело ниже — для глаз пользователя и копирования в реальный робот.
        code_lines = [self._python_code_preamble(),
                      f"# CMD: {cmd.code}      — {cmd.label}"]
        description = ""

        def steer(angle_expr: str) -> str: return f"robot.set_angle({angle_expr})"
        def move(power: str, dur: str) -> str: return f"robot.move({power}, {dur})"
        def stop() -> str: return "robot.stop()"
        def light(c_expr: str) -> str: return f"robot.set_rgb(LIGHT_INDEX, {c_expr}, LIGHT_COUNT)"

        if intent == "forward":
            description = "Ехать вперед."
            if dist:
                code_lines += [steer(str(cur_steer)),
                               move(str(default_speed), f"duration_for_distance({dist}, {default_speed})"),
                               stop()]
            else:
                code_lines += [steer(str(cur_steer)),
                               move(str(default_speed), "1.0")]
        elif intent == "back":
            description = "Движение назад."
            if dist:
                code_lines += [steer(str(cur_steer)),
                               move(f"-{default_speed}", f"duration_for_distance({dist}, {default_speed})"),
                               stop()]
            else:
                code_lines += [steer(str(cur_steer)),
                               move(f"-{default_speed}", "1.0")]
        elif intent == "forward_to_wall":
            description = "Двигаться вперед до стены, используя дальномер."
            code_lines += [
                "def cmd_forward_to_wall(power_pct=DEFAULT_SPEED, stop_margin_mm=150):",
                "    \"\"\"Ехать вперед до препятствия по дальномеру.\"\"\"",
                "    import time",
                "    robot.move(power_pct)",
                "    while True:",
                "        dist = robot.get_laser()",
                "        if dist is None or dist <= stop_margin_mm:",
                "            robot.stop()",
                "            break",
                "        time.sleep(0.05)",
                "",
                steer(str(cur_steer)),
                f"cmd_forward_to_wall({default_speed})",
            ]
        elif intent == "backward_to_wall":
            description = "Двигаться назад до стены, используя дальномер."
            code_lines += [
                "def cmd_backward_to_wall(power_pct=DEFAULT_SPEED, stop_margin_mm=150):",
                "    \"\"\"Ехать назад до препятствия по дальномеру.\"\"\"",
                "    import time",
                "    robot.move(-power_pct)",
                "    while True:",
                "        dist = robot.get_laser()",
                "        if dist is None or dist <= stop_margin_mm:",
                "            robot.stop()",
                "            break",
                "        time.sleep(0.05)",
                "",
                steer(str(cur_steer)),
                f"cmd_backward_to_wall({default_speed})",
            ]
        elif intent == "brake":
            description = "Тормоз — немедленная остановка."
            code_lines += ["# Немедленная остановка ровера.", stop()]
        elif intent == "stop":
            description = "Стоп — немедленная остановка."
            code_lines += ["# Остановить ровера сразу.", stop()]
        elif intent in ("steer_right", "steer_left", "steer_right_small", "steer_left_small"):
            description = "Поворот руля."
            steer_delta = nlu.extract_angle(raw) or (nlu.SMALL_STEER_DEG if intent.endswith("small") else c.turn_angle)
            if intent in ("steer_left", "steer_left_small"):
                steer_delta = -steer_delta
            code_lines += [steer(str(steer_delta))]
        elif intent == "steer_center":
            description = "Выпрямить руль."
            code_lines += ["# Установить руль прямо.", "robot.set_angle(0)"]
        elif intent == "set_speed":
            spd = nlu.extract_speed(raw, default_speed)
            description = "Изменить скорость движения."
            code_lines += [f"# Установить скорость по умолчанию на {spd}%.",
                           f"DEFAULT_SPEED = {spd}"]
        elif intent == "turn_around":
            description = "Разворот робота на 180° по дуге."
            direction = -1 if "налево" in nlu.norm(raw) or "влево" in nlu.norm(raw) or "против" in nlu.norm(raw) else 1
            code_lines += [
                "def cmd_turn_around(direction=1, power_pct=DEFAULT_SPEED):",
                "    \"\"\"Повернуть ровера на 180° по дуге.\"\"\"",
                "    " + steer("direction * DEFAULT_TURN_ANGLE"),
                "    " + move("power_pct", "1.5"),
                "    " + stop(),
                "",
                f"cmd_turn_around({direction})",
            ]
        elif intent == "turn_around_place":
            steps = nlu.extract_kturn_steps(raw)
            description = f"Разворот на месте за {steps} шагов."
            code_lines += [
                "def cmd_turn_around_place(steps, power_pct=DEFAULT_SPEED):",
                "    \"\"\"Выполнить разворот на месте.\"\"\"",
                "    for i in range(steps):",
                "        " + steer("DEFAULT_TURN_ANGLE"),
                "        " + move("power_pct", "0.5"),
                "        " + steer("-DEFAULT_TURN_ANGLE"),
                "        " + move("-power_pct", "0.5"),
                "    " + stop(),
                "",
                f"cmd_turn_around_place({steps})",
            ]
        elif intent == "circle":
            n = nlu.norm(raw)
            direction = +1 if any(w in n for w in ("направо", "вправо", "по часовой")) else -1
            description = "Один полный круг (2π) против часовой стрелки по умолчанию."
            code_lines += [
                "def cmd_circle(direction=-1, power_pct=DEFAULT_SPEED, full_turn_sec=4.0):",
                "    \"\"\"Один полный круг при максимальном угле руля.",
                "    direction=-1 — против часовой стрелки (math: 0→2π).",
                "    direction=+1 — по часовой.\"\"\"",
                "    " + steer("direction * DEFAULT_TURN_ANGLE"),
                "    " + move("power_pct", "full_turn_sec"),
                "    " + stop(),
                "",
                f"cmd_circle({direction})",
            ]
        elif intent == "figure_eight":
            n = nlu.norm(raw)
            direction = +1 if any(w in n for w in ("направо", "вправо", "по часовой")) else -1
            description = "Восьмёрка: первый круг против часовой, второй по часовой."
            code_lines += [
                "def cmd_figure_eight(direction=-1, power_pct=DEFAULT_SPEED, full_turn_sec=4.0):",
                "    \"\"\"Движение восьмёркой.\"\"\"",
                "    " + steer("direction * DEFAULT_TURN_ANGLE"),
                "    " + move("power_pct", "full_turn_sec"),
                "    " + steer("-direction * DEFAULT_TURN_ANGLE"),
                "    " + move("power_pct", "full_turn_sec"),
                "    " + stop(),
                "",
                f"cmd_figure_eight({direction})",
            ]
        elif intent == "spiral_out":
            n = nlu.norm(raw)
            direction = +1 if any(w in n for w in ("направо", "вправо", "по часовой")) else -1
            description = "Плавная спираль наружу: руль непрерывно меняется 36°→24° за 2 оборота, радиус растёт."
            code_lines += [
                "def cmd_spiral(direction=-1, power_pct=DEFAULT_SPEED,",
                "               steer_from=36, steer_to=24, turns=2, micro_sec=0.3):",
                "    \"\"\"Плавная спираль: руль меняется непрерывно во время движения.",
                "    На реальном роботе разбиваем на короткие шаги по micro_sec секунд,",
                "    чтобы успевать корректировать угол поворота колёс.\"\"\"",
                "    total_steps = int(turns * 360 / 5)   # ~5° курса на шаг",
                "    for i in range(total_steps):",
                "        t = i / max(1, total_steps - 1)",
                "        steer_deg = steer_from + (steer_to - steer_from) * t",
                "        " + steer("int(direction * steer_deg)"),
                "        " + move("power_pct", "micro_sec"),
                "    " + stop(),
                "",
                f"cmd_spiral(direction={direction}, steer_from=36, steer_to=24)",
            ]
        elif intent == "spiral_in":
            n = nlu.norm(raw)
            direction = +1 if any(w in n for w in ("направо", "вправо", "по часовой")) else -1
            description = "Плавная спираль внутрь: руль непрерывно меняется 24°→36° за 2 оборота, радиус сужается."
            code_lines += [
                "def cmd_spiral(direction=-1, power_pct=DEFAULT_SPEED,",
                "               steer_from=24, steer_to=36, turns=2, micro_sec=0.3):",
                "    \"\"\"Плавная спираль: руль меняется непрерывно во время движения.\"\"\"",
                "    total_steps = int(turns * 360 / 5)",
                "    for i in range(total_steps):",
                "        t = i / max(1, total_steps - 1)",
                "        steer_deg = steer_from + (steer_to - steer_from) * t",
                "        " + steer("int(direction * steer_deg)"),
                "        " + move("power_pct", "micro_sec"),
                "    " + stop(),
                "",
                f"cmd_spiral(direction={direction}, steer_from=24, steer_to=36)",
            ]
        elif intent in ("bypass_right", "bypass_left"):
            start_dir = +1 if intent == "bypass_right" else -1
            side      = "справа" if start_dir > 0 else "слева"
            description = f"Объезд препятствия {side}: одна S-волна на 2π, руль ±36°. Робот возвращается на исходную линию."
            code_lines += [
                "def cmd_bypass(start_dir=1, max_steer=36, power_pct=DEFAULT_SPEED, quarter_sec=0.5):",
                "    \"\"\"Объезд препятствия: 4 четверти-дуги, каждая по quarter_sec.",
                "    start_dir=+1 — объезд справа (сначала вправо, потом возврат).",
                "    start_dir=-1 — объезд слева.\"\"\"",
                "    for phase in range(4):",
                "        sign = start_dir if phase in (0, 3) else -start_dir",
                "        " + steer("sign * max_steer"),
                "        " + move("power_pct", "quarter_sec"),
                "    " + stop(),
                "",
                f"cmd_bypass(start_dir={start_dir})",
            ]
        elif intent == "goto":
            xy = nlu.extract_coordinates(raw)
            description = "Перейти в точку (X, Y) — поворот к цели + прямая дуга."
            if xy is None:
                code_lines += ["# goto: координаты не указаны"]
            else:
                tx, ty = xy
                code_lines += [
                    "def cmd_goto(target_x, target_y, power_pct=DEFAULT_SPEED):",
                    "    \"\"\"Развернуться лицом к цели и доехать прямой.",
                    "    Требуется внешняя одометрия для отслеживания (x, y, heading).\"\"\"",
                    "    import math",
                    "    # current_x, current_y, current_heading = ...  # из одометрии",
                    "    # dx, dy = target_x - current_x, target_y - current_y",
                    "    # distance = math.hypot(dx, dy)",
                    "    # target_heading = math.degrees(math.atan2(dx, dy)) % 360",
                    "    # ... повернуться, проехать distance см",
                    "    " + stop(),
                    "",
                    f"cmd_goto({tx:g}, {ty:g})",
                ]
        elif intent == "home":
            description = "Возврат в стартовую точку поля."
            code_lines += [
                "def cmd_home(power_pct=DEFAULT_SPEED):",
                "    \"\"\"То же что cmd_goto, только координаты — из стартовых настроек.\"\"\"",
                "    cmd_goto(START_X_CM, START_Y_CM, power_pct)",
                "",
                "cmd_home()",
            ]
        elif intent in ("face_n", "face_ne", "face_e", "face_se",
                         "face_s", "face_sw", "face_w", "face_nw"):
            cardinal_deg = {"face_n":0, "face_ne":45, "face_e":90, "face_se":135,
                             "face_s":180, "face_sw":225, "face_w":270, "face_nw":315}
            cardinal_lbl = {"face_n":"С", "face_ne":"СВ", "face_e":"В", "face_se":"ЮВ",
                             "face_s":"Ю", "face_sw":"ЮЗ", "face_w":"З", "face_nw":"СЗ"}
            tgt = cardinal_deg[intent]
            lbl = cardinal_lbl[intent]
            description = f"Развернуться лицом к стороне света {lbl} ({tgt}°) на месте."
            code_lines += [
                "def cmd_face_cardinal(target_deg, power_pct=DEFAULT_SPEED):",
                "    \"\"\"Развернуться лицом к указанному курсу через K-turn на месте.",
                "    Шагов берём пропорционально углу поворота (~60° за шаг).\"\"\"",
                "    # current_heading = ... (из одометрии)",
                "    # diff = (target_deg - current_heading + 540) % 360 - 180",
                "    # direction = 1 if diff > 0 else -1",
                "    # steps = max(1, round(abs(diff) / 60))",
                "    # for _ in range(steps):",
                "    #     " + steer("direction * DEFAULT_TURN_ANGLE"),
                "    #     " + move("power_pct", "0.5"),
                "    #     " + steer("-direction * DEFAULT_TURN_ANGLE"),
                "    #     " + move("-power_pct", "0.5"),
                "    " + stop(),
                "",
                f"cmd_face_cardinal({tgt})  # {lbl}",
            ]
        elif intent == "set_course":
            target = nlu.extract_course(raw)
            description = "Выставить курс робота."
            if target is None:
                code_lines += ["# Курс не распознан"]
            else:
                code_lines += [
                    "def cmd_set_course(target_deg):",
                    "    current = robot.get_angle()['Z']",
                    "    diff = (target_deg - current + 180) % 360 - 180",
                    "    " + steer("max(-DEFAULT_TURN_ANGLE, min(DEFAULT_TURN_ANGLE, diff))"),
                    "    " + move("DEFAULT_SPEED", "abs(diff) / 30.0"),
                    "    " + stop(),
                    "",
                    f"cmd_set_course({target})",
                ]
        elif intent == "mark_danger":
            xy = nlu.extract_coordinates(raw)
            r  = nlu.extract_radius(raw)
            description = ("Отметка красной зоны обстановки. "
                           "Если указаны координаты — без движения робота.")
            if xy is None:
                code_lines += [
                    "def cmd_mark_danger_here():",
                    "    \"\"\"Отметить опасную зону под текущей позицией робота.\"\"\"",
                    "    pos = robot.get_gps()",
                    "    # отметить (pos['x'], pos['y']) на карте обстановки",
                    "",
                    "cmd_mark_danger_here()",
                ]
            else:
                tx, ty = xy
                radius_arg = f"{r:g}" if r is not None else f"{c.danger_zone_radius:g}"
                code_lines += [
                    "def cmd_mark_danger(x, y, radius):",
                    "    \"\"\"Внести зону обстановки в карту по координатам, без движения.\"\"\"",
                    "    # карта.add_danger_zone(x, y, radius)",
                    "    pass",
                    "",
                    f"cmd_mark_danger({tx:g}, {ty:g}, {radius_arg})",
                ]
        elif intent == "pause":
            secs = nlu.extract_pause_seconds(raw)
            description = f"Пауза {secs:g} секунд — робот стоит на месте."
            code_lines += [
                "import time",
                "robot.stop()",
                f"time.sleep({secs:g})",
            ]
        elif intent == "set_algorithm_zone":
            xy = nlu.extract_coordinates(raw)
            r  = nlu.extract_radius(raw)
            description = ("Перейти в (X, Y) и пометить алгоритмическую зону "
                           "(жёлтая пунктирная — часть алгоритма, не обстановки).")
            if xy is None:
                code_lines += ["# set_algorithm_zone: координаты не указаны"]
            else:
                tx, ty = xy
                radius_arg = f"{r:g}" if r is not None else f"{c.danger_zone_radius:g}"
                code_lines += [
                    "def cmd_set_algorithm_zone(target_x, target_y, radius):",
                    "    \"\"\"Доехать до точки и пометить там алгоритмическую зону.",
                    "    На реальном роботе зона помечается во внешнем",
                    "    приложении/карте по координатам одометрии.\"\"\"",
                    "    cmd_goto(target_x, target_y)",
                    "    pos = robot.get_gps()",
                    "    # отметить зону: (pos['x'], pos['y'], radius)",
                    "",
                    f"cmd_set_algorithm_zone({tx:g}, {ty:g}, {radius_arg})",
                ]
        elif intent == "remove_zone":
            xy = nlu.extract_coordinates(raw)
            description = ("Удалить зоны (любого типа), в которые попадает точка. "
                           "Без координат — берётся текущая позиция робота.")
            if xy is None:
                code_lines += [
                    "def cmd_remove_zone_here():",
                    "    \"\"\"Снять отметку зоны под текущей позицией.\"\"\"",
                    "    pos = robot.get_gps()",
                    "    # удалить зону, содержащую (pos['x'], pos['y'])",
                    "",
                    "cmd_remove_zone_here()",
                ]
            else:
                tx, ty = xy
                code_lines += [
                    "def cmd_remove_zone(x, y):",
                    "    \"\"\"Снять любую зону, в которую попадает точка (x, y).\"\"\"",
                    "    # удалить зону, содержащую (x, y)",
                    "    pass",
                    "",
                    f"cmd_remove_zone({tx:g}, {ty:g})",
                ]
        elif intent == "mode_inspector":
            description = "Режим инспектор."
            code_lines += ["# Режим инспектора: нет прямой команды."]
        elif intent == "mode_cautious":
            description = "Режим осторожности."
            code_lines += ["# Режим осторожности: нет прямой команды."]
        elif intent == "path_show":
            description = "Показать путь."
            code_lines += ["# Показать путь на карте (интерфейсное)."]
        elif intent == "path_hide":
            description = "Скрыть путь."
            code_lines += ["# Скрыть путь на карте (интерфейсное)."]
        elif intent == "reset":
            description = "Сброс поля."
            code_lines += ["# Сбросить состояние и остановить.", stop()]
        elif intent == "report_pos":
            description = "Текущая позиция."
            code_lines += ["# Показать координаты и курс.", "report_position()"]
        elif intent == "report_status":
            description = "Полный статус робота."
            code_lines += [
                "# Полный статус: позиция, курс, скорость, руль, режим, дальномер, заряд.",
                "report_full_status()",
            ]
        elif intent == "light_on":
            description = "Включить свет."
            code_lines += ["# Включить световой индикатор.", light("LIGHT_DEFAULT_COLOR")]
        elif intent == "light_off":
            description = "Выключить свет."
            code_lines += ["# Выключить световой индикатор.", light("(0, 0, 0)")]
        elif intent == "light_color":
            description = "Цвет светового индикатора."
            color = nlu.extract_color(raw) or LIGHT_DEFAULT_COLOR
            code_lines += ["# Установить цвет.", light(str(color))]
        elif intent == "recharge":
            description = "Зарядить батарею до 100% (для симулятора — мгновенно)."
            code_lines += [
                "# В симуляторе зарядка мгновенная.",
                "# Для реального робота — здесь должен быть свой протокол:",
                "# например, ожидание подключения зарядного устройства.",
                "# robot.wait_for_charge(target_pct=100)",
            ]
        else:
            description = "Код для команды не сгенерирован."
            code_lines += ["# Нет шаблона"]
        return description, "\n".join(code_lines)

    # ── Выполнение одной команды ─────────────────────────────────────────────

    async def _dispatch(self, cmd: RobotCmd, db: Session = None):
        s = self.robot_state
        c = self.cfg
        intent = cmd.intent
        raw    = cmd.raw
        msg    = ""
        ok     = True

        if intent == "forward":
            dist = nlu.extract_distance(raw)
            spd  = nlu.extract_speed(raw, c.move_speed)
            await self._run_forward(dist, spd)
            msg = f"Вперёд {dist} см, {spd}%." if dist else f"Вперёд, {spd}%."
        elif intent == "back":
            dist = nlu.extract_distance(raw)
            spd  = nlu.extract_speed(raw, c.move_speed)
            await self._run_back(dist, spd)
            msg = f"Назад {dist} см, {spd}%." if dist else f"Назад, {spd}%."
        elif intent == "forward_to_wall":
            spd = nlu.extract_speed(raw, c.move_speed)
            await self._run_forward_to_wall(spd)
            msg = "Достиг стены."
        elif intent == "backward_to_wall":
            spd = nlu.extract_speed(raw, c.move_speed)
            await self._run_backward_to_wall(spd)
            msg = "Достиг стены (назад)."
        elif intent == "brake":
            s.speed = 0; s.dist_left = 0
            await self.robot.move(0)
            msg = "Тормоз."
        elif intent == "steer_right":
            delta = nlu.extract_angle(raw) or c.turn_angle
            await self._run_steer_delta(float(delta))
            msg = f"Руль +{delta}° (теперь {s.steer:+.0f}°)."
        elif intent == "steer_left":
            delta = nlu.extract_angle(raw) or c.turn_angle
            await self._run_steer_delta(-float(delta))
            msg = f"Руль −{delta}° (теперь {s.steer:+.0f}°)."
        elif intent == "steer_right_small":
            await self._run_steer_delta(float(nlu.SMALL_STEER_DEG))
            msg = f"Правее {nlu.SMALL_STEER_DEG}° (теперь {s.steer:+.0f}°)."
        elif intent == "steer_left_small":
            await self._run_steer_delta(-float(nlu.SMALL_STEER_DEG))
            msg = f"Левее {nlu.SMALL_STEER_DEG}° (теперь {s.steer:+.0f}°)."
        elif intent == "steer_center":
            s.steer = 0.0
            await self.robot.set_servo_center()
            msg = "Руль прямо."
        elif intent == "set_speed":
            spd = nlu.extract_speed(raw, c.move_speed)
            self.cfg.move_speed = spd
            if s.speed != 0:
                sign = -1 if s.speed < 0 else 1
                await self.robot.move(sign * spd)
                s.speed = float(sign * spd)
            msg = f"Скорость {spd}%."
        elif intent == "turn_around":
            n = nlu.norm(raw)
            direction = -1 if any(w in n for w in ("налево", "влево", "против")) else 1
            side = "влево" if direction == -1 else "вправо"
            await self._k_turn_n(direction, steps=1)
            msg = f"Разворот {side} завершён."
        elif intent == "turn_around_place":
            n = nlu.norm(raw)
            direction = -1 if any(w in n for w in ("налево", "влево", "против")) else 1
            steps = nlu.extract_kturn_steps(raw)
            await self._k_turn_n(direction, steps)
            msg = "Разворот на месте завершён."
        elif intent == "circle":
            # По умолчанию CCW (против часовой, мат. направление 0→2π).
            # Только если явно сказано «направо/вправо/по часовой» — CW.
            n = nlu.norm(raw)
            direction = +1 if any(w in n for w in ("направо", "вправо", "по часовой")) else -1
            await self._run_circle(direction)
            msg = "Круг завершён (" + ("по часовой" if direction > 0 else "против часовой") + ")."
        elif intent == "figure_eight":
            n = nlu.norm(raw)
            direction = +1 if any(w in n for w in ("направо", "вправо", "по часовой")) else -1
            await self._run_figure_eight(direction)
            msg = "Восьмёрка завершена."
        elif intent == "spiral_out":
            n = nlu.norm(raw)
            direction = +1 if any(w in n for w in ("направо", "вправо", "по часовой")) else -1
            await self._run_spiral(direction, outward=True)
            msg = "Спираль наружу завершена."
        elif intent == "spiral_in":
            n = nlu.norm(raw)
            direction = +1 if any(w in n for w in ("направо", "вправо", "по часовой")) else -1
            await self._run_spiral(direction, outward=False)
            msg = "Спираль внутрь завершена."
        elif intent == "bypass_right":
            await self._run_bypass(start_dir=+1)
            msg = "Объезд справа завершён."
        elif intent == "bypass_left":
            await self._run_bypass(start_dir=-1)
            msg = "Объезд слева завершён."
        elif intent == "goto":
            xy = nlu.extract_coordinates(raw)
            if xy is None:
                msg, ok = "Координаты не распознаны (нужно: 'в точку X Y').", False
            else:
                tx, ty = xy
                await self._run_goto(tx, ty)
                msg = f"Прибыл в окрестность ({tx:.0f}, {ty:.0f})."
        elif intent == "home":
            await self._run_home()
            msg = "🏠 Возврат в стартовую точку завершён."
        elif intent == "face_n":
            await self._run_face_cardinal(0,   "северу"); msg = "Лицом на север."
        elif intent == "face_ne":
            await self._run_face_cardinal(45,  "северо-востоку"); msg = "Лицом на северо-восток."
        elif intent == "face_e":
            await self._run_face_cardinal(90,  "востоку"); msg = "Лицом на восток."
        elif intent == "face_se":
            await self._run_face_cardinal(135, "юго-востоку"); msg = "Лицом на юго-восток."
        elif intent == "face_s":
            await self._run_face_cardinal(180, "югу"); msg = "Лицом на юг."
        elif intent == "face_sw":
            await self._run_face_cardinal(225, "юго-западу"); msg = "Лицом на юго-запад."
        elif intent == "face_w":
            await self._run_face_cardinal(270, "западу"); msg = "Лицом на запад."
        elif intent == "face_nw":
            await self._run_face_cardinal(315, "северо-западу"); msg = "Лицом на северо-запад."
        elif intent == "set_course":
            target = nlu.extract_course(raw)
            if target is not None:
                await self._run_set_course(target)
                msg = f"Курс {target}° выставлен."
            else:
                msg, ok = "Курс не распознан.", False
        elif intent == "mark_danger":
            xy = nlu.extract_coordinates(raw)
            r  = nlu.extract_radius(raw)
            if xy is not None:
                tx, ty = xy
                await self._run_mark_danger(tx, ty, r, db)
                msg = (f"Зона опасности установлена в ({tx:.0f}, {ty:.0f})"
                       + (f", радиус {r:.0f}." if r is not None else "."))
            else:
                await self._run_mark_danger(None, None, r, db)
                msg = f"Зона опасности в ({s.x:.0f}, {s.y:.0f})."
        elif intent == "pause":
            secs = nlu.extract_pause_seconds(raw)
            await self._run_pause(secs)
            msg = f"Пауза {secs:g} с завершена."
        elif intent == "set_algorithm_zone":
            xy = nlu.extract_coordinates(raw)
            if xy is None:
                msg, ok = "Координаты не распознаны (нужно: 'установи зону X Y').", False
            else:
                tx, ty = xy
                r = nlu.extract_radius(raw)
                await self._run_set_algorithm_zone(tx, ty, r, db)
                msg = (f"Алгоритмическая зона установлена в ({tx:.0f}, {ty:.0f})"
                       + (f", радиус {r:.0f}." if r is not None else "."))
        elif intent == "remove_zone":
            xy = nlu.extract_coordinates(raw)
            tx, ty = (xy if xy is not None else (s.x, s.y))
            n_removed = await self._run_remove_zone(tx, ty, db)
            if n_removed > 0:
                msg = (f"Удалено зон: {n_removed} "
                       f"в точке ({tx:.0f}, {ty:.0f}).")
            else:
                msg, ok = (f"В точке ({tx:.0f}, {ty:.0f}) "
                           f"зон не найдено."), False
        elif intent == "mode_inspector":
            s.mode = "normal"; s.cautious = False
            msg = "Режим инспектор."
        elif intent == "mode_cautious":
            s.cautious = True
            msg = "Режим осторожно."
        elif intent == "path_show":
            await self.broadcast({"type": "path_visible", "visible": True})
            msg = "Путь показан."
        elif intent == "path_hide":
            await self.broadcast({"type": "path_visible", "visible": False})
            msg = "Путь скрыт."
        elif intent == "reset":
            await self._run_reset(db)
            msg = "Поле очищено."
        elif intent == "report_pos":
            msg = f"X={s.x:.0f} см, Y={s.y:.0f} см, курс={s.heading:.0f}°."
        elif intent == "report_status":
            laser_lbl = (f"{s.laser_dist:.0f} см"
                         if (self.cfg.laser_enabled and s.laser_dist > 0)
                         else ("выключен" if not self.cfg.laser_enabled else "—"))
            batt_factor = self._battery_factor(s.battery)
            effective   = s.speed * batt_factor
            if s.speed == 0:
                cur_lbl = "0% (стоит)"
            elif batt_factor < 0.999:
                cur_lbl = (f"{effective:+.0f}% "
                           f"(подано {s.speed:+.0f}% × {batt_factor:.2f} от заряда)")
            else:
                cur_lbl = f"{s.speed:+.0f}%"
            msg = (
                f"📊 Статус робота:\n"
                f"  • координаты: X={s.x:.1f}, Y={s.y:.1f}\n"
                f"  • курс: {s.heading:.0f}°\n"
                f"  • руль: {s.steer:+.0f}°\n"
                f"  • скорость установленная: {c.move_speed}%\n"
                f"  • скорость текущая: {cur_lbl}\n"
                f"  • дальномер: {laser_lbl}\n"
                f"  • зарядка: {s.battery:.0f}%"
            )
        elif intent == "light_on":
            s.light_color = LIGHT_DEFAULT_COLOR
            await self.robot.set_rgb(LIGHT_INDEX, LIGHT_DEFAULT_COLOR, LIGHT_COUNT)
            msg = "Свет включен."
        elif intent == "light_off":
            s.light_color = (0, 0, 0)
            await self.robot.set_rgb(LIGHT_INDEX, (0, 0, 0), LIGHT_COUNT)
            msg = "Свет выключен."
        elif intent == "light_color":
            color = nlu.extract_color(raw) or LIGHT_DEFAULT_COLOR
            s.light_color = color
            await self.robot.set_rgb(LIGHT_INDEX, color, LIGHT_COUNT)
            msg = f"Свет установлен: {color}."
        elif intent == "recharge":
            await self._run_recharge()
            msg = "Батарея заряжена."
        else:
            msg, ok = "Команда не выполнена.", False

        # Для воспроизведения — Python-код не отправляем (textarea не дублируется)
        if not cmd.playback:
            python_desc, python_code = self._python_code_for_cmd(cmd)
        else:
            python_desc, python_code = None, None

        if db and self._db_session_id:
            db.add(CommandLog(session_id=self._db_session_id,
                              raw_text=raw, intent=intent, success=ok))
            db.add(PathPoint(session_id=self._db_session_id,
                             x=s.x, y=s.y, heading=s.heading))
            db.commit()

        self.world.add_path(s.x, s.y)
        await self.push_state()
        if msg:
            await self.push_message(msg, "success" if ok else "warning",
                                    code=python_code if ok else None,
                                    description=python_desc if ok else None)

    # ── Фоновый исполнитель очереди ───────────────────────────────────────────

    async def _queue_runner(self):
        while True:
            if self._pending:
                cmd = self._pending.pop(0)
                self._executing = cmd
                await self.push_queue()
                db = SessionLocal()
                success = False
                try:
                    self._exec_task = asyncio.create_task(self._dispatch(cmd, db))
                    try:
                        await self._exec_task
                        success = True
                    except asyncio.CancelledError:
                        self.robot_state.speed = 0
                        self.robot_state.dist_left = 0
                        try:
                            await self.robot.stop()
                            await self.robot.set_servo_center()
                        except Exception:
                            pass
                    except Exception as exc:
                        log.error("[user %d] dispatch error %s: %s", self.user_id, cmd.intent, exc)
                finally:
                    db.close()
                    self._executing = None
                    self._exec_task = None
                    await self.push_queue()
                    # Фиксируем заряд в БД после каждой команды — на случай
                    # внезапного рестарта сервера. Зарядка переживает рестарты.
                    self._save_battery_pct()

                if success and not cmd.playback and cmd.intent not in _NO_RECORD:
                    if cmd.intent != "reset":
                        self._program.append(cmd)
                        self._save_program()
                    # push_program НЕ вызываем — textarea наполняется
                    # через message.code (см. _dispatch). Иначе — двойная
                    # отправка и перезатирание накопленного.
            await asyncio.sleep(0.05)

    # ── Запуск записанной программы ───────────────────────────────────────────

    async def _run_program(self):
        """Внутренний запуск self._program (без перепарсинга текста)."""
        if not self._program:
            await self.push_message(
                "Программа пуста. Используйте кнопки управления или голосовые команды, "
                "чтобы записать программу, затем нажмите Запуск.", "warning")
            return
        await self._do_stop()
        reset_cmd = self._build_cmd("reset", "Вега новое поле")
        if reset_cmd:
            reset_cmd.playback = True
            self._pending.append(reset_cmd)
        for recorded in self._program:
            if recorded.intent == "reset":
                continue
            self._pending.append(RobotCmd(
                id=next(self._cmd_counter),
                intent=recorded.intent, label=recorded.label,
                code=recorded.code, raw=recorded.raw,
                playback=True,
            ))
        brake_cmd = self._build_cmd("brake", "Вега тормоз")
        if brake_cmd:
            brake_cmd.playback = True
            self._pending.append(brake_cmd)
        await self.push_queue()
        await self.push_message(f"▶ Воспроизведение: {len(self._program)} команд.", "info")

    # ── Парсинг textarea (DSL) и запуск ──────────────────────────────────────

    _DSL_LINE   = re.compile(r'^\s*([a-z_]+)\s*\(\s*([^)]*)\s*\)\s*$')
    _CMD_MARKER = re.compile(r'^\s*#\s*CMD\s*:\s*([a-z_]+)\s*\(\s*([^)]*)\s*\)')

    @staticmethod
    def _parse_dsl_line(fn: str, args: str) -> Optional[tuple[str, str]]:
        """Преобразует вызов DSL в (intent, raw_text для NLU). None если неизвестен.

        Принимает как короткую форму DSL (`forward(100)`, `mark_danger(0,0,10)`),
        так и Python-инвокации тел `cmd_X(args)` / `cmd_X_here()`, которые
        генерируются для реального робота. Это позволяет редактировать тело
        кода в textarea (копировать `cmd_mark_danger(...)` с новыми
        координатами) и видеть изменения после «▶ Запуск»."""
        # ── Нормализация имени: cmd_X → X, cmd_X_here → X (без аргументов).
        if fn.startswith("cmd_"):
            fn = fn[4:]
            if fn.endswith("_here"):
                fn = fn[:-5]
                args = ""    # _here-варианты вызываются без координат
        args = args.strip()
        if fn == "reset":            return ("reset", "Вега новое поле")
        if fn == "brake":            return ("brake", "Вега тормоз")
        if fn == "stop":             return ("stop",  "Вега стоп")
        if fn == "forward":
            if args.lstrip("+-").isdigit():
                return ("forward", f"Вега вперёд {int(args)} см")
            return ("forward", "Вега вперёд")
        if fn == "back":
            if args.lstrip("+-").isdigit():
                return ("back", f"Вега назад {int(args)} см")
            return ("back", "Вега назад")
        if fn == "forward_to_wall":  return ("forward_to_wall", "Вега вперёд до упора")
        if fn == "backward_to_wall": return ("backward_to_wall", "Вега назад до упора")
        if fn == "steer":
            if args.lstrip("+-").isdigit():
                n = int(args)
                if n == 0:  return ("steer_center", "Вега руль прямо")
                if n > 0:   return ("steer_right",  f"Вега направо {n}")
                return ("steer_left", f"Вега налево {-n}")
            return None
        if fn == "set_speed":
            if args.isdigit():
                return ("set_speed", f"Вега скорость {int(args)} процентов")
            return None
        if fn == "turn_around":      return ("turn_around", "Вега разворот")
        if fn == "turn_around_place":
            steps = int(args) if args.isdigit() else 3
            return ("turn_around_place", f"Вега разворот на месте {steps} шагов")
        if fn == "circle":           return ("circle", "Вега вокруг")
        if fn == "figure_eight":     return ("figure_eight", "Вега восьмёрка")
        if fn == "spiral_out":       return ("spiral_out", "Вега спираль наружу")
        if fn == "spiral_in":        return ("spiral_in", "Вега спираль внутрь")
        if fn == "bypass_right":     return ("bypass_right", "Вега объезд справа")
        if fn == "bypass_left":      return ("bypass_left",  "Вега объезд слева")
        if fn == "home":             return ("home",         "Вега домой")
        if fn == "goto":
            # goto(X, Y) — поддерживаем оба разделителя
            parts = re.split(r'[,\s]+', args.strip())
            try:
                tx, ty = float(parts[0]), float(parts[1])
                return ("goto", f"Вега в точку {tx:g} {ty:g}")
            except (ValueError, IndexError):
                return None
        if fn == "face_n":           return ("face_n",  "Вега на север")
        if fn == "face_ne":          return ("face_ne", "Вега на северо-восток")
        if fn == "face_e":           return ("face_e",  "Вега на восток")
        if fn == "face_se":          return ("face_se", "Вега на юго-восток")
        if fn == "face_s":           return ("face_s",  "Вега на юг")
        if fn == "face_sw":          return ("face_sw", "Вега на юго-запад")
        if fn == "face_w":           return ("face_w",  "Вега на запад")
        if fn == "face_nw":          return ("face_nw", "Вега на северо-запад")
        if fn == "set_course":
            if args.lstrip("-").isdigit():
                return ("set_course", f"Вега курс {int(args)}")
            return None
        if fn == "mark_danger":
            args = args.strip()
            if not args:
                return ("mark_danger", "Вега опасная зона")
            parts = re.split(r'[,\s]+', args)
            try:
                tx, ty = float(parts[0]), float(parts[1])
            except (ValueError, IndexError):
                return ("mark_danger", "Вега опасная зона")
            extra = ""
            if len(parts) >= 3:
                try:
                    r = float(parts[2])
                    extra = f" радиус {r:g}"
                except ValueError:
                    pass
            return ("mark_danger",
                    f"Вега опасная зона {tx:g} {ty:g}{extra}")
        if fn == "pause":
            try:
                secs = float(args) if args else 1.0
            except ValueError:
                secs = 1.0
            return ("pause", f"Вега пауза {secs:g}")
        if fn == "set_algorithm_zone":
            parts = re.split(r'[,\s]+', args.strip())
            try:
                tx, ty = float(parts[0]), float(parts[1])
            except (ValueError, IndexError):
                return None
            extra = ""
            if len(parts) >= 3:
                try:
                    r = float(parts[2])
                    extra = f" радиус {r:g}"
                except ValueError:
                    pass
            return ("set_algorithm_zone",
                    f"Вега установи зону {tx:g} {ty:g}{extra}")
        if fn == "remove_zone":
            args = args.strip()
            if not args:
                return ("remove_zone", "Вега убрать зону")
            parts = re.split(r'[,\s]+', args)
            try:
                tx, ty = float(parts[0]), float(parts[1])
                return ("remove_zone", f"Вега убрать зону {tx:g} {ty:g}")
            except (ValueError, IndexError):
                return None
        if fn == "path_show":        return ("path_show", "Вега показать путь")
        if fn == "path_hide":        return ("path_hide", "Вега скрыть путь")
        if fn == "report_pos":       return ("report_pos", "Вега где ты")
        if fn == "report_status":    return ("report_status", "Вега статус робота")
        if fn == "light_on":         return ("light_on", "Вега включи свет")
        if fn == "light_off":        return ("light_off", "Вега выключи свет")
        if fn == "recharge":         return ("recharge", "Вега зарядить")
        if fn == "mode":
            if "inspector" in args.lower(): return ("mode_inspector", "Вега инспектор")
            if "cautious"  in args.lower(): return ("mode_cautious",  "Вега осторожно")
            return None
        return None

    @staticmethod
    def _norm_args(s: str) -> str:
        """Нормализует строку аргументов для сравнения (убирает пробелы)."""
        return re.sub(r'\s+', '', s)

    def _parse_program_text(self, text: str) -> list[RobotCmd]:
        """Парсит команды из textarea.
        Распознаёт три формата (в порядке приоритета):
          1) Маркер `# CMD: <dsl>` — основной источник истины.
          2) Тело Python-инвокации `cmd_X(args)` / `cmd_X_here()` —
             пользователь может править/копировать вызовы для добавления
             новых команд (например, ещё одна `cmd_mark_danger(10,10,30)`).
          3) Голая DSL-строка `forward(100)` — для ручного редактирования.
        reset/brake/stop пропускаем — они оборачивают воспроизведение автоматически.

        Дедупликация: если СРАЗУ за `# CMD: foo(args)` идёт `cmd_foo(args)` с
        теми же аргументами — это родная пара «маркер + тело», тело не считаем
        второй раз. Если же тело отличается (отредактированные координаты) или
        идёт без маркера — считаем как самостоятельную команду."""
        cmds: list[RobotCmd] = []
        # (fn, normalized_args) — последний маркер, ещё не «погашенный» телом
        pending: Optional[tuple[str, str]] = None

        for line in text.split("\n"):
            # 1) Маркер из богатого Python — приоритет
            mm = self._CMD_MARKER.match(line)
            if mm:
                fn, args = mm.group(1), mm.group(2)
                parsed = self._parse_dsl_line(fn, args)
                if parsed:
                    intent, raw = parsed
                    if intent not in ("reset", "brake", "stop"):
                        cmd = self._build_cmd(intent, raw)
                        if cmd:
                            cmds.append(cmd)
                            pending = (fn, self._norm_args(args))
                            continue
                pending = None
                continue
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            md = self._DSL_LINE.match(stripped)
            if not md:
                continue
            fn_raw, args_raw = md.group(1), md.group(2)
            # 2) Тело cmd_X(args) или 3) голая DSL-строка X(args).
            # Нормализуем имя для сравнения с pending-маркером:
            fn_for_cmp = fn_raw[4:] if fn_raw.startswith("cmd_") else fn_raw
            if fn_for_cmp.endswith("_here"):
                fn_for_cmp = fn_for_cmp[:-5]
                args_for_cmp = ""
            else:
                args_for_cmp = self._norm_args(args_raw)
            # Дедуп маркер ↔ тело: совпало имя функции И ЛИБО аргументы
            # тоже совпали, ЛИБО маркер был без аргументов (так пишутся
            # circle/figure_eight/turn_around — направление хранится в raw).
            # Это критично: иначе одна восьмёрка превращается в две команды
            # (маркер + неотсеянное тело cmd_figure_eight(-1)).
            if pending is not None and pending[0] == fn_for_cmp and (
               pending[1] == args_for_cmp or pending[1] == ""):
                pending = None
                continue
            parsed = self._parse_dsl_line(fn_raw, args_raw)
            if not parsed:
                continue
            intent, raw = parsed
            if intent in ("reset", "brake", "stop"):
                continue
            cmd = self._build_cmd(intent, raw)
            if cmd:
                cmds.append(cmd)
            pending = None
        return cmds

    async def run_textarea_program(self, text: str):
        """Парсит текст из textarea, перестраивает self._program и запускает.
        Это путь по которому нажатие «▶ Запуск» уважает редактирование пользователя.

        НЕ вызываем push_program() после парсинга: иначе текстарея перерисуется
        из self._program и пользовательские правки в теле кода (например,
        дополнительные `cmd_mark_danger(10,10,30)`) исчезнут. Текстарея —
        master, self._program — её зеркало для физики."""
        new_program = self._parse_program_text(text)
        if not new_program:
            await self.push_message(
                "В текстовом поле не найдено команд. "
                "Каждая команда — отдельная строка вида forward(100), steer(+13), brake() и т.п.",
                "warning")
            return
        # Перестраиваем _program под текст. push_program НЕ вызываем —
        # сохраняем визуальный layout пользователя.
        self._program = new_program
        self._save_program()
        # И запускаем
        await self._run_program()

    async def load_published_code(self, code: str, source_label: str = "опубликованный маршрут"):
        """Загружает чужой опубликованный код в свою программу.
        Парсит # CMD маркеры из текста, заменяет self._program и push'ит textarea.
        НЕ запускает автоматически — пользователь сам нажмёт ▶ Запуск."""
        new_program = self._parse_program_text(code)
        if not new_program:
            await self.push_message(
                f"В коде «{source_label}» не найдено CMD-команд. Загрузка отменена.",
                "warning")
            return False
        await self._do_stop()
        self._program = new_program
        self._save_program()
        await self.push_program()
        await self.push_message(
            f"📥 Загружено: {source_label} ({len(new_program)} команд). "
            f"Можно отредактировать и нажать ▶ Запуск.",
            "success")
        return True

    # ── Точка входа для команды ──────────────────────────────────────────────

    async def handle_command(self, raw_text: str, db: Session = None):
        intent, conf = nlu.predict(raw_text)
        log.info("[user %d] CMD %r → intent=%s conf=%.2f", self.user_id, raw_text, intent, conf)

        if intent == "stop":
            await self._do_stop()
            await self.push_state()
            await self.push_queue()
            cmd = self._build_cmd(intent, raw_text)
            description, code = self._python_code_for_cmd(cmd) if cmd else (None, None)
            await self.push_message("Стоп!", "success", code=code, description=description)
            return

        # Зарядка — мгновенное действие, не должна ждать в очереди манёвров.
        if intent == "recharge":
            await self._run_recharge()
            await self.push_state()
            return

        # Запросы статуса — тоже мгновенные, без очереди.
        if intent in ("report_pos", "report_status"):
            cmd = self._build_cmd(intent, raw_text)
            # Прогоняем через _dispatch напрямую — он сформирует текст отчёта.
            db_local = SessionLocal()
            try:
                await self._dispatch(cmd, db_local)
            finally:
                db_local.close()
            return

        cmd = self._build_cmd(intent, raw_text)
        if cmd is None:
            msg = "Команда не распознана." if not intent else f"Намерение «{intent}» не поддерживается."
            await self.push_message(msg, "warning")
            return

        self._pending.append(cmd)
        await self.push_queue()
        await self.push_message(f"→ {cmd.label}", "info")

    # ── Физика ───────────────────────────────────────────────────────────────

    @staticmethod
    def _battery_factor(charge: float) -> float:
        """Доля от номинальной скорости при текущем заряде батареи (0–100%).
        Имитирует поведение LiPo: до ~60% — без потерь, дальше плавный спад,
        при 0% мотор не вращается.

        Границы соответствуют цвету шкалы заряда в карточке состояния:
          ≥ 60% (зелёный):           1.00 — без потерь
          30–60% (жёлто-оливковый):  0.70 → 1.00 (линейно)
          15–30% (оранжевый):        0.40 → 0.70
          0–15% (красный):           0.00 → 0.40
          0%:                        мотор не крутится"""
        if charge >= 60.0: return 1.0
        if charge >= 30.0: return 0.70 + 0.30 * (charge - 30.0) / 30.0
        if charge >= 15.0: return 0.40 + 0.30 * (charge - 15.0) / 15.0
        if charge >  0.0:  return 0.40 * charge / 15.0
        return 0.0

    async def update_physics(self):
        c = self.cfg
        last = time.monotonic()
        while True:
            await asyncio.sleep(0.1)
            now = time.monotonic()
            dt  = now - last
            last = now

            s = self.robot_state

            # ── Расход батареи.
            # «battery_minutes» = минуты НЕПРЕРЫВНОЙ езды от полной зарядки до 0.
            # В простое расход — 1/10 от активного (электроника платы потребляет
            # на порядок меньше, чем мотор). Таймер привязан к кнопке «🔋 Зарядить»:
            # перезагрузки страницы и рестарт сервера сохраняют текущий заряд.
            if s.battery > 0.0:
                rate_per_s = 100.0 / max(60.0, c.battery_minutes * 60.0)
                idle_factor = 0.1                              # 1/10 от активного
                drain = rate_per_s * dt * (1.0 if s.speed != 0 else idle_factor)
                s.battery = max(0.0, s.battery - drain)

            # ── Если заряд сел — двигатель не крутится.
            if s.battery <= 0.0 and s.speed != 0:
                s.speed     = 0
                s.dist_left = 0
                try:    await self.robot.move(0)
                except Exception: pass
                await self.push_message("🪫 Батарея разряжена — робот остановлен.", "warning")

            sign = 1 if s.speed >= 0 else -1

            if s.speed == 0:
                if isinstance(self.robot, SimDriver) and c.sensor_type == "laser":
                    d = self._wall_dist_for_robot()
                    s.laser_dist = d
                    self.robot._laser_cm = d
                continue

            pct          = abs(s.speed) / 100.0
            batt_factor  = self._battery_factor(s.battery)
            cm_per_s     = pct * c.speed_at_100 * batt_factor
            if cm_per_s <= 0.0:
                # факт: батарея не тянет — стоит на месте этот тик
                continue
            dist_cm  = cm_per_s * dt

            if s.dist_left > 0:
                target_dist = min(dist_cm, s.dist_left)
            else:
                target_dist = dist_cm

            if isinstance(self.robot, SimDriver) and s.laser_stop:
                # Дальномер измеряет до центральной линии стены ±halfW.
                # Стена нарисована с обводкой width=wall_thickness_cm, центрированной
                # на ±halfW, т. е. ВНУТРЕННЯЯ кромка стены — на (halfW - wt/2).
                # Запас остановки перед стеной = 2 × wall_thickness_cm.
                # Угол робота должен остановиться на этом запасе ОТ внутренней
                # кромки, т. е. на расстоянии (wt/2 + 2·wt) = 2.5·wt от центра.
                sensor_heading_pre = s.heading if sign > 0 else (s.heading + 180) % 360
                laser_pre  = self._wall_dist_for_robot(sensor_heading_pre)
                stop_margin_cm = c.wall_thickness_cm * 2.0
                inner_gap  = c.wall_thickness_cm / 2.0 + stop_margin_cm
                safe_step  = max(0.0, laser_pre - inner_gap)
                target_dist = min(target_dist, safe_step)

            actual_dist = target_dist
            actual_dt   = actual_dist / cm_per_s if cm_per_s > 0 else dt

            if s.steer != 0:
                steer_ratio   = s.steer / 45.0
                trf           = self._turn_rate_factor(s.speed)
                heading_delta = (actual_dist / c.wheel_circ_cm) * c.heading_per_rot * steer_ratio * trf * sign
                s.heading = (s.heading + heading_delta) % 360

            h_rad_pre = math.radians(s.heading)
            rx_pre = s.x - c.robot_length_cm * math.sin(h_rad_pre)
            ry_pre = s.y - c.robot_length_cm * math.cos(h_rad_pre)

            update_position_dead_reckoning(s, actual_dt, sign * cm_per_s)

            # Внутренняя кромка стены = halfW - wall_thickness/2.
            # Корпус робота не должен заезжать ЗА эту кромку.
            wt2 = c.wall_thickness_cm / 2.0
            hw2 = self.world.width  / 2 - wt2
            hh2 = self.world.height / 2 - wt2
            hit_wall = False
            if sign < 0:
                h_rad = math.radians(s.heading)
                rx = s.x - c.robot_length_cm * math.sin(h_rad)
                ry = s.y - c.robot_length_cm * math.cos(h_rad)
                if rx > hw2 and rx_pre <= hw2:
                    s.x -= (rx - hw2); hit_wall = True
                elif rx < -hw2 and rx_pre >= -hw2:
                    s.x -= (rx + hw2); hit_wall = True
                if ry > hh2 and ry_pre <= hh2:
                    s.y -= (ry - hh2); hit_wall = True
                elif ry < -hh2 and ry_pre >= -hh2:
                    s.y -= (ry + hh2); hit_wall = True
                if not hit_wall and (rx > hw2 or rx < -hw2 or ry > hh2 or ry < -hh2):
                    hit_wall = True
            else:
                if s.x > hw2:
                    s.x = hw2; hit_wall = True
                elif s.x < -hw2:
                    s.x = -hw2; hit_wall = True
                if s.y > hh2:
                    s.y = hh2; hit_wall = True
                elif s.y < -hh2:
                    s.y = -hh2; hit_wall = True
            if hit_wall:
                s.speed     = 0
                s.dist_left = 0
                await self.robot.move(0)

            self.world.add_path(s.x, s.y)

            if isinstance(self.robot, SimDriver):
                # Порог стопа: внутренняя кромка стены + запас (2·wall_thickness).
                # Тот же запас, что в per-step capping — wt/2 (до кромки) + 2·wt.
                stop_thresh = c.wall_thickness_cm / 2.0 + c.wall_thickness_cm * 2.0
                sensor_heading = s.heading if sign > 0 else (s.heading + 180) % 360
                if c.sensor_type == "sonar":
                    interval_s = c.sonar_interval_ms / 1000.0
                    if now - self._sonar_state["last_fire"] >= interval_s:
                        self._sonar_state["last_fire"] = now
                        raw = self._wall_dist_for_robot(sensor_heading)
                        echo_time   = 2.0 * raw / SOUND_SPEED_CM_S
                        v_cm_s      = abs(s.speed) / 100.0 * c.speed_at_100
                        moved_echo  = v_cm_s * echo_time
                        moved_pulse = v_cm_s * interval_s
                        effective   = max(0.0, round(raw - moved_echo - moved_pulse, 1))
                        s.laser_dist = effective
                        self.robot._laser_cm = effective
                        if s.laser_stop and effective <= stop_thresh:
                            s.laser_stop = False
                            s.speed = 0; s.dist_left = 0
                            await self.robot.move(0)
                else:
                    new_laser = self._wall_dist_for_robot(sensor_heading)
                    s.laser_dist = new_laser
                    self.robot._laser_cm = new_laser
                    if s.laser_stop and new_laser <= stop_thresh:
                        s.laser_stop = False
                        s.speed = 0; s.dist_left = 0
                        await self.robot.move(0)

            if not hit_wall and s.dist_left > 0:
                s.dist_left -= actual_dist
                if s.dist_left <= 0:
                    s.dist_left = 0
                    s.speed = 0
                    await self.robot.move(0)
                    await self.push_message("Готово.", "success")

            await self.push_state()


# ═══════════════════════════════════════════════════════════════════════════════
# Реестр сессий
# ═══════════════════════════════════════════════════════════════════════════════

SESSIONS: dict[int, UserSession] = {}


async def get_or_create_session(user_id: int, db: Session) -> UserSession:
    """Возвращает сессию пользователя; создаёт и стартует, если нет."""
    sess = SESSIONS.get(user_id)
    if sess is not None:
        return sess
    settings_row = _ensure_user_settings(db, user_id)
    cfg = UserCfg.from_row(settings_row)
    sess = UserSession(user_id, cfg)
    SESSIONS[user_id] = sess
    await sess.start()
    return sess


def get_session(user_id: int) -> Optional[UserSession]:
    """Просто получить существующую сессию (без создания)."""
    return SESSIONS.get(user_id)


async def stop_all_sessions():
    for sess in list(SESSIONS.values()):
        try:
            await sess.stop()
        except Exception:
            pass
    SESSIONS.clear()
