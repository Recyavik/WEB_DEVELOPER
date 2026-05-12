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
import json
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

# Интенты, которые не записываются в программу — это UI-команды
# (переключение интерфейсного режима, показ/скрытие путей, отчёты),
# а не часть алгоритма. Если пользователь явно впишет `mode(cautious)`
# в Python-код, парсер всё равно его распознает и выполнит.
_NO_RECORD = {"report_pos", "report_status", "path_show", "path_hide",
              "recharge", "mode_inspector", "mode_cautious"}


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
    # Если True — команда не записывается в self._program и в textarea.
    # Используется для взаимного гашения «поставил → удалил» опасную зону:
    # обе команды пропадают из программы, как будто их и не было.
    skip_record: bool = False
    # Позиция робота ПОСЛЕ исполнения команды. Заполняется в _dispatch.
    # Используется при сохранении кастомной миссии: waypoints =
    # endpoints команд движения (а не сэмплы path_history).
    end_x:       Optional[float] = None
    end_y:       Optional[float] = None
    end_heading: Optional[float] = None


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
    path_cell_size_cm:  int  = 10
    # Следование за рассчитанным путем в режиме «осторожно»
    cautious_follow_algo: str  = "pure_pursuit"   # "pure_pursuit" | "stanley"
    cautious_slow_curves: bool = True
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
            path_cell_size_cm  = max(2, int(row.path_cell_size_cm or 10)),
            cautious_follow_algo = (row.cautious_follow_algo or "pure_pursuit"),
            cautious_slow_curves = bool(row.cautious_slow_curves
                                        if row.cautious_slow_curves is not None else True),
        )


def _ensure_user_settings(db: Session, user_id: int) -> UserSettings:
    """Возвращает UserSettings пользователя; создает с дефолтами если нет."""
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

        # Активная миссия пользователя (если есть). См. mission_state.py.
        # None пока пользователь не нажал «Пройти» на какой-либо миссии.
        self._mission = None

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

    # ── Миссии ──────────────────────────────────────────────────────────

    async def start_mission(self, mission_id: int) -> bool:
        """Активировать миссию для текущей сессии.

        Поведение «как новое поле + загрузка миссии»:
          1. Загружаем миссию из БД, создаём MissionRun.
          2. Сбрасываем поле (_run_reset): робот в стартовую точку,
             зоны/путь очищаются, _program пустеет.
          3. Опасные зоны миссии превращаются в начальные команды
             mark_danger_cmd(...) в _program — пользователь видит их
             как обычные команды программы (можно изучать/править/
             запускать ▶).
          4. Широковещаем mission_active + новое состояние world/program.
        """
        from mission_state import from_mission_row
        from models import Mission as MissionRow, MissionRun
        db = SessionLocal()
        try:
            row = db.query(MissionRow).filter(MissionRow.id == mission_id).first()
            if row is None:
                await self.push_message(
                    f"Миссия #{mission_id} не найдена.", "error")
                return False
            # Стартовая точка миссии = path[0] (генератор кладёт туда
            # start_x/start_y использованного WorldGeom). Если она отличается
            # от текущей настройки пользователя — обновляем UserSettings,
            # чтобы не лезть в /settings руками каждый раз при смене миссии.
            path = json.loads(row.path or "[]")
            if path:
                new_sx = float(path[0][0])
                new_sy = float(path[0][1])
                if (abs(new_sx - self.cfg.start_x_cm) > 0.1 or
                        abs(new_sy - self.cfg.start_y_cm) > 0.1):
                    self.cfg.start_x_cm = new_sx
                    self.cfg.start_y_cm = new_sy
                    us = (db.query(UserSettings)
                            .filter(UserSettings.user_id == self.user_id).first())
                    if us is not None:
                        us.start_x_cm = new_sx
                        us.start_y_cm = new_sy
                        db.commit()
                    await self.push_message(
                        f"📍 Стартовая точка миссии: ({new_sx:.0f}, {new_sy:.0f}).",
                        "info")
            # Сброс поля — теперь робот встанет в обновлённую стартовую точку,
            # зоны сбрасываются, _program пустеет.
            # keep_mode=True — режим «осторожно» сохраняется через активацию,
            # иначе action-кнопки моргают между синим и жёлтым.
            await self._run_reset(db, keep_mode=True)
            run = MissionRun(mission_id=row.id, user_id=self.user_id)
            db.add(run); db.commit(); db.refresh(run)
            self._mission = from_mission_row(
                row, user_id=self.user_id,
                start_x=self.robot_state.x, start_y=self.robot_state.y,
                run_id=run.id,
            )
            # Опасные зоны миссии → начальные команды mark_danger_cmd
            # в _program. В world они НЕ кладутся — появятся в физике,
            # только когда пользователь запустит ▶ Запустить код.
            for (zx, zy, zr) in self._mission.danger_zones:
                raw = f"Вега опасная зона {int(zx)} {int(zy)} {int(zr)}"
                cmd = self._build_cmd("mark_danger", raw)
                if cmd is not None:
                    self._program.append(cmd)
            self._save_program()
        finally:
            db.close()
        await self.push_message(
            f"🎯 Миссия «{self._mission.title}» активирована. "
            f"Точек: {len(self._mission.waypoints)}, "
            f"действий: {len(self._mission.actions_required)}.",
            "info")
        await self.broadcast({
            "type":    "mission_active",
            "mission": self._mission.to_client_dict(),
        })
        await self.push_program()
        await self.push_state()
        return True

    async def run_check(self) -> bool:
        """Прогон программы для активной миссии — без автозавершения.

        Сбрасываем счётчики прохождения (каждый прогон оценивает только
        ПОСЛЕДНЕЕ исполнение по точкам и точности — иначе старый успех
        даёт звёзды даже если код испортили), запускаем _program через
        очередь, ждём опустошения, замеряем время и НАКАПЛИВАЕМ его в
        last_algo_duration_sec — суммарное время алгоритма по всем
        прогонам в этой миссии. Это лёгкий штраф за «попытки методом
        подбора»: чем больше раз запустил, тем хуже метрика эффективности.

        Финал — отдельным действием через finalize_mission (кнопка
        «🏁 Проверка задания»). До тех пор миссия активна, можно жать ▶
        повторно, править код, делать новые прогоны."""
        if self._mission is None:
            return False
        if not self._program:
            await self.push_message(
                "Программа пуста — нечего запускать.", "warning")
            return False
        m = self._mission
        m.waypoints_visited.clear()
        m.actions_done.clear()
        m.coefficient        = 1.0
        m.deviations         = 0
        m.last_in_margin     = True
        m.last_robot_pos     = None
        algo_start_t = time.monotonic()
        await self._run_program()
        while self._pending or self._executing is not None:
            await asyncio.sleep(0.1)
        run_dur = time.monotonic() - algo_start_t
        if self._mission is not None:
            cumulative = (self._mission.last_algo_duration_sec or 0.0) + run_dur
            self._mission.last_algo_duration_sec = round(cumulative, 2)
            wp_done = len(self._mission.waypoints_visited)
            wp_total = len(self._mission.waypoints)
            prec = int(round(self._mission.coefficient * 100))
            await self.push_message(
                f"✓ Прогон завершён за {run_dur:.1f} с "
                f"(всего {cumulative:.1f} с). "
                f"Точки {wp_done}/{wp_total}, точность {prec}%. "
                f"Жми 🏁 Проверка задания для финала.",
                "info")
        return True

    async def finalize_mission(self) -> bool:
        """Финальная оценка миссии — фиксирует результат последнего прогона.
        Если все цели достигнуты → success. Иначе → не выполнено (0 звёзд).
        Время задания (с активации) и накопленное время алгоритма сохраняются."""
        if self._mission is None:
            return False
        await self.stop_mission(success=self._mission.is_complete())
        return True

    def _mission_check_action(self, action_type: str,
                              x: float, y: float) -> None:
        """Если идёт миссия — проверить, не удовлетворяет ли это действие
        одному из обязательных actions_required (place/remove зон).
        No-op если миссии нет."""
        if self._mission is None:
            return
        idx = self._mission.try_match_action(action_type, x, y)
        if idx is not None:
            # Уведомим пользователя, что засчитали действие миссии.
            done = len(self._mission.actions_done)
            total = len(self._mission.actions_required)
            asyncio.create_task(self.push_message(
                f"✓ Действие миссии засчитано ({action_type}). "
                f"Прогресс: {done}/{total}.",
                "success"))

    async def stop_mission(self, success: Optional[bool] = None) -> None:
        """Завершить активную миссию. Сохраняет финальный MissionRun
        с count'ами и звёздами. Если success не указан — определяется
        по is_complete()."""
        if self._mission is None:
            return
        from datetime import datetime
        from models import MissionRun
        m = self._mission
        if success is None:
            success = m.is_complete()
        ended_at = datetime.utcnow()
        duration_sec = max(0.0, (ended_at - m.started_at).total_seconds())
        # Финальная разбивка звёзд: факт + точность + скорость (только при успехе).
        fact_stars  = m.fact_stars()
        track_stars = m.track_bonus_stars()
        time_stars  = m.time_bonus_stars(duration_sec) if success else 0
        stars = (fact_stars + track_stars + time_stars) if success else 0
        precision_pct = int(round(m.coefficient * 100))
        algo_duration = round(m.last_algo_duration_sec or 0.0, 2)
        if m.run_id is not None:
            db = SessionLocal()
            try:
                run = db.query(MissionRun).filter(MissionRun.id == m.run_id).first()
                if run:
                    run.completed_at      = ended_at
                    run.stars             = stars
                    run.coefficient       = round(m.coefficient, 4)
                    run.deviations        = m.deviations
                    run.duration_sec      = round(duration_sec, 2)
                    run.algo_duration_sec = algo_duration
                    run.waypoints_visited = json.dumps(sorted(m.waypoints_visited))
                    run.actions_done      = json.dumps(sorted(m.actions_done))
                    run.success           = bool(success)
                    db.commit()
            finally:
                db.close()
        # Маркер миссии: предпочитаем title (он уже включает #ID
        # в fallback-варианте «Миссия #N» из /missions/save), иначе #ID.
        # Так избегаем дубля «#2 «Миссия #2»».
        if m.title:
            mission_label = f"«{m.title}»"
        else:
            mission_label = f"#{m.mission_id}"
        await self.broadcast({
            "type":        "mission_finished",
            "mission_id":  m.mission_id,
            "title":       m.title or "",
            "success":     bool(success),
            "stars":       stars,
            "stars_fact":  fact_stars,
            "stars_track": track_stars,
            "stars_time":  time_stars,
            "coefficient": round(m.coefficient, 3),
            "precision_pct": precision_pct,
            "deviations":  m.deviations,
            "duration_sec": round(duration_sec, 1),
            "algo_duration_sec": algo_duration,
        })
        # Форматирование времени для журнала.
        def _fmt(sec):
            mm = int(sec // 60); ss = int(sec % 60)
            return f"{mm:02d}:{ss:02d}"
        time_str = _fmt(duration_sec)
        algo_str = _fmt(algo_duration) if algo_duration > 0 else "—"
        await self.push_message(
            f"🏁 Задание {mission_label} завершено: "
            f"{'✓ успех' if success else '✗ не выполнено'}. "
            f"⭐ {stars} (точки {fact_stars} + точность {track_stars} "
            f"+ скорость {time_stars}), "
            f"точность {precision_pct}%, время задания {time_str}, "
            f"время алгоритма {algo_str}, отклонений: {m.deviations}.",
            "success" if success else "warning")
        self._mission = None

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

    def _cancel_matching_mark_danger(self, rx: float, ry: float) -> bool:
        """Ищет в self._program последнюю команду mark_danger (КРАСНУЮ зону),
        чьё кольцо содержит точку (rx, ry), и удаляет её из программы.

        Используется при «взаимном гашении»: если пользователь поставил
        опасную зону, а потом её удалил (мышью в режиме зон или командой
        «убрать зону»), обе команды убираются из программы — как будто
        их и не было. Применимо только к зонам, поставленным в этой же
        сессии и записанным в _program; внешние зоны (расставленные
        генератором заданий и т.п.) не задеваются.

        ⚠ ЖЁЛТЫЕ зоны (set_algorithm_zone) НЕ гасятся, даже если попадают
        под точку удаления — они создаются алгоритмом по условию
        (радиация/температура и т.п.), и их история ВАЖНА для понимания
        работы алгоритма. Поэтому фильтр явно по `intent == "mark_danger"`,
        а не «любая зональная команда».

        Возвращает True если что-то удалили."""
        for idx in range(len(self._program) - 1, -1, -1):
            prev = self._program[idx]
            if prev.intent != "mark_danger":
                continue
            prev_xy = nlu.extract_coordinates(prev.raw)
            if prev_xy is None:
                continue
            prev_r = nlu.extract_radius(prev.raw)
            if prev_r is None:
                prev_r = float(self.cfg.danger_zone_radius)
            px, py = prev_xy
            if math.hypot(rx - px, ry - py) <= prev_r:
                self._program.pop(idx)
                self._save_program()
                return True
        return False

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
        # Явный сигнал о статусе миссии: либо активная (с данными), либо
        # «нет миссии». Это нужно, чтобы клиент после рестарта сервера
        # не зависал в «миссия есть, а на сервере её нет» и понятно сбросил UI.
        if self._mission is not None:
            await ws.send_json({
                "type":    "mission_active",
                "mission": self._mission.to_client_dict(),
            })
        else:
            await ws.send_json({"type": "mission_inactive"})

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

    def _program_text(self) -> str:
        """Полный текст программы для textarea:
          константы → сентинель → объединение def-блоков, нужных всем командам
          (с транзитивными зависимостями) → блоки вызовов команд.
        Пустая программа → только константы, без cmd_*-функций."""
        parts = [self._python_code_preamble(self._program).rstrip()]
        if not self._program:
            parts.append("")
            parts.append("# (программа пуста — выполни команды кнопками управления)")
            return "\n".join(parts) + "\n"
        for cmd in self._program:
            parts.append("")
            # Только маркер + вызов; def-блоки уже в преамбуле (один раз).
            parts.append("\n".join(self._python_call_lines_for_cmd(cmd)))
        return "\n".join(parts) + "\n"

    def _state_dict_with_effective(self) -> dict:
        """state_to_dict + поле effective_speed_pct: скорость, фактически
        выдаваемая мотором с учетом просадки батареи."""
        d = state_to_dict(self.robot_state)
        factor = self._battery_factor(self.robot_state.battery)
        d["effective_speed_pct"] = self.robot_state.speed * factor
        d["battery_factor"]      = factor
        return d

    async def push_state(self):
        # Mission tracking: на каждый push_state — обновляем коэффициент и
        # отмечаем посещённые waypoints. Если миссия завершилась — авто-стоп.
        # Во время K-turn (s.turning_in_place) робот съезжает с прямой
        # waypoint→waypoint по геометрии Reeds-Shepp — игнорируем эти
        # кадры, чтобы развороты на месте не штрафовали оценку миссии.
        mission_progress = None
        if self._mission is not None and not self.robot_state.turning_in_place:
            s = self.robot_state
            self._mission.update_coefficient(s.x, s.y)
            new_visits = self._mission.mark_waypoint_visits(s.x, s.y)
            for idx in new_visits:
                wp = self._mission.waypoints[idx]
                await self.push_message(
                    f"✓ Точка {idx + 1} ({wp[0]:.0f}, {wp[1]:.0f}) посещена.",
                    "success")
            mission_progress = self._mission.progress_dict()
            # Авто-стоп при is_complete УБРАН: финал миссии теперь только
            # через явное действие пользователя — кнопка «🏁 Проверка
            # задания» или «⏹ Стоп миссия». Это позволяет пробовать
            # программу несколько раз без потери активного состояния.
        payload = {
            "type":  "state",
            "robot": self._state_dict_with_effective(),
            "path":  self.world.path_history,
        }
        if mission_progress is not None:
            payload["mission_progress"] = mission_progress
        await self.broadcast(payload)

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
        self.robot_state.thinking  = "idle"
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

    # ── Маневр K-turn ────────────────────────────────────────────────────────

    async def _k_turn_n(self, direction: int = 1, steps: int = 3):
        """Разворот на 180° в N приемов (steps — пользовательский параметр,
        задает ритм видимых пар forward-back). Та же 3-фазная схема, что
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
                await self.push_message(f"Разворот {i+1}/{steps}: вперед…", "info")
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
        # Если дальномер включен — физика остановит у стены, не доезжая
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

    # ── Сложные маневры (круг / восьмерка / спираль / синусоида) ────────────

    async def _arc_at_steer(self, steer_deg: float, sweep_deg: float, spd: int,
                              backward: bool = False):
        """Едет дугой при заданном угле руля до изменения курса на sweep_deg.
        backward=True — едет ЗАДОМ (steer тот же, скорость противоположная).

        Дальномер ВКЛЮЧЕН — если корпус подходит близко к стене, дуга
        прервется для безопасности. K-turn внутри `_run_goto` потом сам
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
        """Восьмерка: первый круг ПРОТИВ часовой (CCW), второй — по часовой.
        direction=+1 — наоборот, начать по часовой."""
        s = self.robot_state
        spd = self.cfg.move_speed
        steer = self.cfg.turn_angle
        try:
            await self.push_message("Восьмерка: круг 1/2…", "info")
            await self._arc_at_steer(direction * steer, 360.0, spd)
            await asyncio.sleep(0.2)
            await self.push_message("Восьмерка: круг 2/2…", "info")
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
        Прогресс отслеживаем по реально набранной развертке курса (не по времени)."""
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
                # Накопление развертки курса (модуль, без учета направления вращения)
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
                # курс не меняется → sweep_abs не растет. Выходим через таймаут.
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
        """Точка входа goto. В режиме «осторожно» может включить планировщик
        обхода зон и выполнить ломаный маршрут. Иначе — обычный заход в точку."""
        s = self.robot_state
        if s.cautious:
            ok = await self._run_goto_cautious(target_x, target_y)
            if ok:
                return
            # cautious не нашел/не выполнил — выходим, не дергаем direct
            return
        await self._run_goto_direct(target_x, target_y)

    async def _run_goto_cautious(self, target_x: float, target_y: float) -> bool:
        """Goto в режиме «осторожно»: проверяет прямой путь на коллизии с
        зонами, и если надо — запускает A*-планировщик обхода.

        Возвращает True, если маршрут выполнен (или прямая свободна).
        False — если решение не найдено: робот не двигается, требуется
        ручное вмешательство."""
        s   = self.robot_state
        cfg = self.cfg
        # Сбрасываем «failed» от предыдущих попыток, если был.
        if s.thinking == "failed":
            s.thinking = "idle"
        # Импорт локально — модуль может отсутствовать в редких сборках.
        import path_planner as pp

        zones = [pp.Obstacle(z.x, z.y, z.radius)
                 for z in self.world.danger_zones]
        if not zones:
            await self._run_goto_direct(target_x, target_y)
            return True

        # Раздутие = корпус + небольшой запас на K-turn-свинг.
        # Между waypoint'ами _run_goto_direct может выписывать дуги, которые
        # отклоняются от прямой линии. Полная компенсация (≈ robot_length)
        # делает проходы между близкими зонами вообще непроходимыми, поэтому
        # берем половину — баланс между свободой и безопасностью.
        robot_inflation = (max(cfg.robot_length_cm, cfg.robot_width_cm) / 2.0
                           + cfg.robot_length_cm / 2.0)
        safety = cfg.wall_thickness_cm   # «Запас безопасности» = wall_thickness

        # Прямая свободна? Тогда планировщик не нужен.
        if not pp.line_hits_zones((s.x, s.y), (target_x, target_y),
                                   zones, robot_inflation, safety):
            await self._run_goto_direct(target_x, target_y)
            return True

        # Нужен обход — запускаем A*.
        await self.push_message(
            "⚠ Прямой путь к цели пересекает опасную зону. Ищу обход…",
            "warning")
        s.thinking = "planning"
        await self.push_state()
        try:
            # Прерываем выполнение управления — пока думаем, ничего не двигаем.
            await asyncio.sleep(0)   # give scheduler a tick — UI получит spinner
            waypoints = pp.plan_path(
                (s.x, s.y), (float(target_x), float(target_y)),
                zones,
                world_w=self.world.width,
                world_h=self.world.height,
                wall_thickness=cfg.wall_thickness_cm,
                robot_inflation=robot_inflation,
                cell_size=float(cfg.path_cell_size_cm),
                safety_margin=safety,
            )
        finally:
            # thinking сбросим ниже — после успеха/провала
            pass

        if not waypoints:
            s.thinking = "failed"
            await self.push_state()
            await self.push_message(
                f"🖥 Решение не найдено: между опасными зонами нет прохода "
                f"к ({target_x:.0f}, {target_y:.0f}). "
                f"Перейдите в ручной режим или измените зоны.",
                "error")
            return False

        # Сглаживаем ломаную в дугообразную кривую (Чайкин 3 итерации)
        # и пересэмплируем равномерно — pure-pursuit нужен плотный путь.
        smooth = pp.chaikin_smooth(waypoints, iterations=3)
        dense  = pp.resample_curve(smooth, step_cm=5.0)

        # Фиксируем сегмент для фиолетовой подсветки на canvas.
        self.world.add_auto_segment(smooth)
        s.thinking = "idle"
        await self.push_world()  # сегмент должен появиться сразу
        await self.push_state()

        # Pure-pursuit / Stanley follow: робот непрерывно крутит рулем
        # к точке впереди, без K-turn'ов и резких разворотов.
        ok = await self._follow_curve(dense)

        # Финальная доводка: следящий алгоритм почти всегда заканчивает
        # с небольшим отклонением (5–30 см) — pure-pursuit срезает углы,
        # Stanley может оставить боковую ошибку. Если прямая от текущей
        # позиции до цели УЖЕ свободна от зон — добиваем через
        # _run_goto_direct (умеет K-turn + прямую) и считаем успехом.
        remaining = math.hypot(target_x - s.x, target_y - s.y)
        if remaining > 5.0:
            line_clear = not pp.line_hits_zones(
                (s.x, s.y), (target_x, target_y),
                zones, robot_inflation, safety)
            if line_clear:
                await self._run_goto_direct(float(target_x), float(target_y))
                remaining = math.hypot(target_x - s.x, target_y - s.y)

        if remaining < 10.0:
            return True
        if not ok:
            await self.push_message(
                "⚠ Не доехал до цели по запланированному пути. "
                "Возможно касание зоны или стены.", "warning")
            return False
        return True

    @staticmethod
    def _curvature_speed_factor(points: list[tuple[float, float]],
                                 idx: int,
                                 lookahead_pts: int = 10) -> float:
        """Доля от номинальной скорости в зависимости от кривизны пути впереди.
        Чем круче поворот в окне `lookahead_pts` — тем больше замедление.
        Возвращает 0.4..1.0."""
        n = len(points)
        if idx >= n - 2:
            return 1.0
        end = min(idx + lookahead_pts, n - 1)
        if end - idx < 2:
            return 1.0
        total_turn = 0.0
        prev_a = None
        for i in range(idx, end):
            dx = points[i + 1][0] - points[i][0]
            dy = points[i + 1][1] - points[i][1]
            if dx * dx + dy * dy < 1e-6:
                continue
            a = math.atan2(dx, dy)
            if prev_a is not None:
                diff = (a - prev_a + 3 * math.pi) % (2 * math.pi) - math.pi
                total_turn += abs(diff)
            prev_a = a
        # 0 рад → 1.0; π/2 рад (90°) → ~0.4
        factor = max(0.4, 1.0 - total_turn * 0.6)
        return factor

    @staticmethod
    def _approach_speed_factor(points: list[tuple[float, float]],
                                idx: int,
                                robot_x: float,
                                robot_y: float,
                                slow_zone_cm: float = 60.0,
                                min_factor:   float = 0.20) -> float:
        """Замедление на финише: чем ближе конец пути, тем медленнее.
        Учитывает И оставшуюся длину пути от ближайшей точки, И прямое
        расстояние от робота до цели (если робот срезал угол и оказался
        близко к цели «по воздуху», тоже надо тормозить).

        Линейно: за `slow_zone_cm` до конца — full, у самого конца —
        `min_factor`. Берется минимум из двух метрик."""
        n = len(points)
        if n == 0:
            return 1.0
        # 1) Длина оставшегося пути от idx до конца
        remaining_path = 0.0
        if idx < n - 1:
            for i in range(idx, n - 1):
                dx = points[i + 1][0] - points[i][0]
                dy = points[i + 1][1] - points[i][1]
                remaining_path += math.hypot(dx, dy)
                if remaining_path >= slow_zone_cm:
                    remaining_path = slow_zone_cm
                    break
        # 2) Прямое расстояние до цели от текущей позиции робота
        gx, gy = points[-1]
        direct = math.hypot(gx - robot_x, gy - robot_y)
        # Берем более жесткое (меньшее) из двух
        eff = min(remaining_path, direct)
        if eff >= slow_zone_cm:
            return 1.0
        t = eff / max(1.0, slow_zone_cm)
        return min_factor + (1.0 - min_factor) * t

    async def _follow_curve(self, points: list[tuple[float, float]]) -> bool:
        """Диспетчер следования за кривой. Алгоритм выбирается из настроек."""
        algo = self.cfg.cautious_follow_algo or "pure_pursuit"
        if algo == "stanley":
            return await self._follow_stanley(points)
        return await self._follow_pure_pursuit(points)

    async def _align_to_path_start(self,
                                    points: list[tuple[float, float]]) -> None:
        """Если робот смотрит сильно мимо начала пути — сначала развернемся
        K-turn'ом, чтобы pure-pursuit не уводило в круг минимального радиуса."""
        s = self.robot_state
        if len(points) < 2:
            return
        dx = points[1][0] - points[0][0]
        dy = points[1][1] - points[0][1]
        if dx * dx + dy * dy < 1.0:
            return
        path_heading = math.degrees(math.atan2(dx, dy))
        diff = (path_heading - s.heading + 540.0) % 360.0 - 180.0
        # Порог 50° — если больше, мы заведомо не «поймаем» цель рулем
        if abs(diff) > 50.0:
            await self._k_turn_to_heading(path_heading)

    def _direct_to_goal_clear(self, gx: float, gy: float) -> bool:
        """True если прямая от текущей позиции робота до (gx, gy) свободна
        от опасных зон (с учетом раздутия по корпусу + safety_margin).
        Используется follower'ом для динамической перепланировки:
        как только препятствия пройдены — выходим и доводим прямой."""
        if not self.world.danger_zones:
            return True
        import path_planner as pp
        s = self.robot_state
        zones = [pp.Obstacle(z.x, z.y, z.radius) for z in self.world.danger_zones]
        infl = (max(self.cfg.robot_length_cm, self.cfg.robot_width_cm) / 2.0
                + self.cfg.robot_length_cm / 2.0)
        return not pp.line_hits_zones((s.x, s.y), (gx, gy),
                                       zones, infl, self.cfg.wall_thickness_cm)

    async def _follow_pure_pursuit(self, points: list[tuple[float, float]]) -> bool:
        """Pure-pursuit: руль крутится к точке на расстоянии LOOKAHEAD впереди.
        Плавно срезает углы. Подходит большинству сцен.

        Дополнительно: каждые ~0.5 сек проверяет, свободна ли прямая до
        цели — если да, выходит из кривой (наружный wrapper доведет прямой)."""
        if not points or len(points) < 2:
            return True
        s   = self.robot_state
        cfg = self.cfg
        base_spd  = cfg.move_speed
        MAX_STEER = float(cfg.turn_angle)
        LOOKAHEAD = max(30.0, cfg.robot_length_cm * 1.5)
        TOL_CM    = 5.0
        TIMEOUT_S = 90.0
        # Если стоим лицом не туда — сначала разворачиваемся
        await self._align_to_path_start(points)

        await self.robot.set_angle(0)
        await self.robot.move(base_spd)
        s.speed      = float(base_spd)
        s.dist_left  = 0.0
        s.laser_stop = bool(cfg.laser_enabled)

        gx, gy = points[-1]
        last_idx = 0
        deadline = time.monotonic() + TIMEOUT_S
        tick     = 0      # счетчик для динамической перепланировки
        # Антизалипание: засчитываем прогресс ЛИБО продвижение по индексу
        # пути, ЛИБО приближение к цели «по воздуху». Это защищает от двух
        # сценариев: вращение в круге (нет ни того, ни другого) И срезание
        # угла (индекс не растет, но дистанция до цели падает).
        last_progress_idx  = 0
        last_progress_dist = math.hypot(gx - s.x, gy - s.y)
        last_progress_time = time.monotonic()
        STUCK_S = 6.0

        try:
            while True:
                if math.hypot(gx - s.x, gy - s.y) < TOL_CM:
                    return True
                if s.speed == 0:
                    return False
                now = time.monotonic()
                if now > deadline:
                    return False

                # Динамическая перепланировка: каждые ~0.5 сек проверяем,
                # свободна ли уже прямая до цели. Если да — выходим, наружный
                # wrapper доведет через _run_goto_direct (он умеет K-turn).
                tick += 1
                if tick % 5 == 0 and self._direct_to_goal_clear(gx, gy):
                    return True

                # Ближайшая точка на пути от прошлого индекса
                best_i = last_idx
                best_d2 = float('inf')
                for i in range(last_idx, len(points)):
                    dx = points[i][0] - s.x
                    dy = points[i][1] - s.y
                    d2 = dx * dx + dy * dy
                    if d2 < best_d2:
                        best_d2 = d2
                        best_i  = i
                    elif d2 > best_d2 + 100.0:
                        break
                last_idx = best_i

                # Антизалипание (круг)
                # Прогресс — это ЛИБО продвижение по индексу пути, ЛИБО
                # сокращение прямого расстояния до цели хотя бы на 5 см.
                # Без второй метрики антизалипание ложно срабатывало,
                # когда робот срезает угол и идет к цели «по воздуху».
                cur_dist = math.hypot(gx - s.x, gy - s.y)
                if best_i > last_progress_idx or cur_dist < last_progress_dist - 5.0:
                    last_progress_idx  = best_i
                    last_progress_dist = cur_dist
                    last_progress_time = now
                elif now - last_progress_time > STUCK_S:
                    return False

                # Точка-цель на LOOKAHEAD впереди
                target_i = best_i
                accumulated = 0.0
                for i in range(best_i, len(points) - 1):
                    seg = math.hypot(points[i + 1][0] - points[i][0],
                                     points[i + 1][1] - points[i][1])
                    accumulated += seg
                    if accumulated >= LOOKAHEAD:
                        target_i = i + 1
                        break
                else:
                    target_i = len(points) - 1
                tx, ty = points[target_i]

                bx, by = tx - s.x, ty - s.y
                if bx * bx + by * by < 1.0:
                    tx, ty = points[-1]
                    bx, by = tx - s.x, ty - s.y
                bearing = math.degrees(math.atan2(bx, by))
                heading_diff = (bearing - s.heading + 540.0) % 360.0 - 180.0

                # Меньший коэффициент — мягче руль, нет «закусывания» в круг
                desired_steer = max(-MAX_STEER,
                                    min(MAX_STEER, heading_diff * 0.7))
                if abs(desired_steer - s.steer) > 0.5:
                    await self.robot.set_angle(int(desired_steer))
                    s.steer = float(desired_steer)

                # Замедление: на поворотах И на подходе к концу пути.
                # approach_speed_factor смотрит и на оставшийся путь, и на
                # прямое расстояние до цели — берет более жесткое.
                approach = self._approach_speed_factor(points, best_i, s.x, s.y)
                if cfg.cautious_slow_curves:
                    curvy = self._curvature_speed_factor(points, best_i)
                    factor = min(approach, curvy)
                else:
                    factor = approach
                target_spd = base_spd * factor
                if abs(target_spd - s.speed) > 1.5:
                    await self.robot.move(target_spd)
                    s.speed = float(target_spd)

                await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            return False
        finally:
            s.speed      = 0
            s.steer      = 0.0
            s.dist_left  = 0
            s.laser_stop = False
            try:
                await self.robot.move(0)
                await self.robot.set_servo_center()
            except Exception:
                pass

    async def _follow_stanley(self, points: list[tuple[float, float]]) -> bool:
        """Stanley controller (Stanford / DARPA Grand Challenge):
            steer = heading_error + atan(K · cross_track_error / velocity)

        Учитывает не только направление, но и боковое смещение от пути —
        активно стягивает робота обратно на линию. Точнее pure-pursuit
        на длинных кривых, но может быть резче на тугих поворотах."""
        if not points or len(points) < 2:
            return True
        s   = self.robot_state
        cfg = self.cfg
        base_spd  = cfg.move_speed
        MAX_STEER = float(cfg.turn_angle)
        # K_CROSS: чем больше — тем активнее тянет на путь, но тем неустойчивей.
        # 0.6 дает мягкое доведение без раскачки.
        K_CROSS   = 0.6
        # В знаменателе используем НОМИНАЛЬНУЮ скорость, не текущую.
        # При slow_curves текущая скорость падает до 11 см/с — atan(K·cte/v)
        # тогда взрывается на любом боковом смещении и вызывает раскачку.
        # Стабильность Stanley важнее реакции на малой скорости.
        SOFTEN_V  = max(20.0, base_spd / 100.0 * cfg.speed_at_100)
        TOL_CM    = 5.0
        TIMEOUT_S = 90.0
        # Если стоим лицом не туда — сначала разворачиваемся
        await self._align_to_path_start(points)

        await self.robot.set_angle(0)
        await self.robot.move(base_spd)
        s.speed      = float(base_spd)
        s.dist_left  = 0.0
        s.laser_stop = bool(cfg.laser_enabled)

        gx, gy = points[-1]
        last_idx = 0
        deadline = time.monotonic() + TIMEOUT_S
        # См. комментарий выше про антизалипание (тот же подход, что в pure-pursuit).
        last_progress_idx  = 0
        last_progress_dist = math.hypot(gx - s.x, gy - s.y)
        last_progress_time = time.monotonic()
        STUCK_S = 6.0
        tick = 0     # счетчик для динамической перепланировки

        try:
            while True:
                if math.hypot(gx - s.x, gy - s.y) < TOL_CM:
                    return True
                if s.speed == 0:
                    return False
                now = time.monotonic()
                if now > deadline:
                    return False

                # Динамическая перепланировка: каждые ~0.5 сек проверяем,
                # свободна ли уже прямая до цели.
                tick += 1
                if tick % 5 == 0 and self._direct_to_goal_clear(gx, gy):
                    return True

                # Ближайшая точка на пути
                best_i = last_idx
                best_d2 = float('inf')
                for i in range(last_idx, len(points)):
                    dx = points[i][0] - s.x
                    dy = points[i][1] - s.y
                    d2 = dx * dx + dy * dy
                    if d2 < best_d2:
                        best_d2 = d2
                        best_i  = i
                    elif d2 > best_d2 + 100.0:
                        break
                last_idx = best_i

                # Антизалипание (круг)
                # Прогресс — это ЛИБО продвижение по индексу пути, ЛИБО
                # сокращение прямого расстояния до цели хотя бы на 5 см.
                # Без второй метрики антизалипание ложно срабатывало,
                # когда робот срезает угол и идет к цели «по воздуху».
                cur_dist = math.hypot(gx - s.x, gy - s.y)
                if best_i > last_progress_idx or cur_dist < last_progress_dist - 5.0:
                    last_progress_idx  = best_i
                    last_progress_dist = cur_dist
                    last_progress_time = now
                elif now - last_progress_time > STUCK_S:
                    return False

                px, py = points[best_i]

                # Касательная к пути в этой точке
                j = min(best_i + 1, len(points) - 1)
                if j == best_i and best_i > 0:
                    pp_prev = points[best_i - 1]
                    tx_dir = px - pp_prev[0]
                    ty_dir = py - pp_prev[1]
                else:
                    tx_dir = points[j][0] - px
                    ty_dir = points[j][1] - py
                t_len = math.hypot(tx_dir, ty_dir)
                if t_len < 1e-6:
                    tx_dir, ty_dir = 0.0, 1.0
                    t_len = 1.0
                tx_dir /= t_len
                ty_dir /= t_len

                # heading_error: разница между текущим heading и направлением пути
                path_heading = math.degrees(math.atan2(tx_dir, ty_dir))
                heading_diff = (path_heading - s.heading + 540.0) % 360.0 - 180.0

                # Cross-track error (signed). Используем нормаль СЛЕВА от tangent
                # = (-ty_dir, tx_dir). Положительный CTE = робот СЛЕВА от пути,
                # значит надо рулить ВПРАВО (положительный steer).
                # Для соответствия знакам нашего steering — формула стандартная.
                offset_x = s.x - px
                offset_y = s.y - py
                # Перпендикуляр от пути направо = (ty_dir, -tx_dir)
                # CTE = (right_normal · offset) — положителен когда робот справа.
                cte = ty_dir * offset_x - tx_dir * offset_y

                # Используем НОМИНАЛЬНУЮ скорость как «v» для Стэнли.
                # Так формула остается стабильной даже когда slow_curves
                # снизил реальную скорость почти до нуля у финиша.
                v_for_atan = SOFTEN_V

                # Stanley formula: положительный CTE справа → рулим ВЛЕВО
                # (отрицательный atan), отсюда МИНУС перед atan.
                cte_term_deg = -math.degrees(math.atan2(K_CROSS * cte, v_for_atan))
                desired_steer = heading_diff + cte_term_deg
                desired_steer = max(-MAX_STEER, min(MAX_STEER, desired_steer))

                if abs(desired_steer - s.steer) > 0.5:
                    await self.robot.set_angle(int(desired_steer))
                    s.steer = float(desired_steer)

                # Замедление: на поворотах И на подходе к концу пути.
                # approach_speed_factor теперь требует robot_x/y — учитывает
                # и оставшийся путь, и прямое расстояние до цели.
                approach = self._approach_speed_factor(points, best_i, s.x, s.y)
                if cfg.cautious_slow_curves:
                    curvy = self._curvature_speed_factor(points, best_i)
                    factor = min(approach, curvy)
                else:
                    factor = approach
                target_spd = base_spd * factor
                if abs(target_spd - s.speed) > 1.5:
                    await self.robot.move(target_spd)
                    s.speed = float(target_spd)

                await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            return False
        finally:
            s.speed      = 0
            s.steer      = 0.0
            s.dist_left  = 0
            s.laser_stop = False
            try:
                await self.robot.move(0)
                await self.robot.set_servo_center()
            except Exception:
                pass

    async def _run_goto_direct(self, target_x: float, target_y: float):
        """Заход в точку с проверкой и повторными попытками.

        Алгоритм:
          1) ПОВОРОТ: 3-дуговой симметричный K-turn — выставить курс на цель.
          2) ПРЯМАЯ: проехать distance см.
          3) ПРОВЕРКА: если робот не дошел (дальномер прервал движение или
             K-turn уперся в стену) — отъехать назад на 30 см, чтобы выйти
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
                        f"✓ Дошел до ({target_x:.0f}, {target_y:.0f}) "
                        f"с {attempt}-й попытки.", "success")
                else:
                    await self.push_message(
                        f"⚠ До ({target_x:.0f}, {target_y:.0f}) всего "
                        f"{distance:.1f} см — микро-движения короче "
                        f"{TOL_CM:.0f} см не реализованы. "
                        f"Команда не записана. Совет: отъедьте подальше или "
                        f"развернитесь и подойдите к точке заново.",
                        "warning")
                return

            target_heading = math.degrees(math.atan2(dx, dy)) % 360
            # Угол между текущим курсом и направлением на цель: ∈ [-180, 180]
            bearing = (target_heading - s.heading + 540.0) % 360.0 - 180.0
            STRAIGHT_TOL = 30.0    # ±30° считаем «почти по курсу»

            # Shortcut 1: цель почти ПЕРЕД носом → едем сразу вперед, без поворота.
            if abs(bearing) < STRAIGHT_TOL:
                if attempt == 1:
                    await self.push_message(
                        f"В точку ({target_x:.0f}, {target_y:.0f}): "
                        f"цель прямо ({bearing:+.0f}°), еду вперед {distance:.0f} см.",
                        "info")
                await self._run_forward(distance, self.cfg.move_speed)
                # переходим к проверке достижения ниже
                dx2 = float(target_x) - s.x
                dy2 = float(target_y) - s.y
                dist2 = math.hypot(dx2, dy2)
                if dist2 < TOL_CM:
                    return
                if attempt < MAX_ATTEMPTS:
                    await self._run_back(BACKOFF_CM, self.cfg.move_speed)
                continue

            # Shortcut 2: цель почти ЗА СПИНОЙ → едем задом, без 180° K-turn.
            if abs(bearing) > 180.0 - STRAIGHT_TOL:
                if attempt == 1:
                    await self.push_message(
                        f"В точку ({target_x:.0f}, {target_y:.0f}): "
                        f"цель за спиной ({bearing:+.0f}°), еду задом {distance:.0f} см.",
                        "info")
                await self._run_back(distance, self.cfg.move_speed)
                dx2 = float(target_x) - s.x
                dy2 = float(target_y) - s.y
                dist2 = math.hypot(dx2, dy2)
                if dist2 < TOL_CM:
                    return
                if attempt < MAX_ATTEMPTS:
                    await self._run_forward(BACKOFF_CM, self.cfg.move_speed)
                continue

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
                        f"✓ Дошел до ({target_x:.0f}, {target_y:.0f}).", "success")
                return

            # Не дошли — стена/препятствие. Отъезжаем на безопасную дистанцию,
            # чтобы при следующей попытке был свободный заход с новой позиции.
            if attempt < MAX_ATTEMPTS:
                await self.push_message(
                    f"📍 Не дошел до цели (отклонение {dist3:.0f} см). "
                    f"Отъезжаю назад на {BACKOFF_CM:.0f} см и пробую еще раз.",
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
        из настроек (`START_X`, `START_Y`)."""
        sx, sy = float(self.cfg.start_x_cm), float(self.cfg.start_y_cm)
        await self.push_message(f"🏠 Домой: точка ({sx:.0f}, {sy:.0f}).", "info")
        await self._run_goto(sx, sy)

    # ── Развернуться лицом к указанному курсу (на месте, K-turn) ────────────

    async def _k_turn_to_heading(self, target_deg: float):
        """Разворот на месте до target_deg с возвратом в исходную точку.

        Стратегия 3 фаз:
          Фаза 1: аналитический 3-дуговой K-turn (Reeds-Shepp) — даёт
                  курс, но из-за погрешностей физики симулятора может
                  оставить дрейф позиции до десятков см.
          Фаза 2: если позиция уехала больше TOL_POS — пропорциональный
                  регулятор довозит робот обратно в исходную (x, y).
                  При этом курс может слегка сбиться.
          Фаза 3: если курс сбился больше TOL_HDG — короткий доразворот
                  мелкими дугами (по 5..8° каждая), чтобы вернуть точный курс.
        Фазы 2+3 повторяются итеративно до сходимости либо MAX_ITER раз.

        Флаг `s.turning_in_place` поднимается на весь манёвр — оценка
        миссии (update_coefficient) игнорирует отклонения, пока он True,
        потому что K-turn по геометрии съезжает с прямой waypoint→waypoint."""
        s = self.robot_state
        target_deg = float(target_deg) % 360
        diff = (target_deg - s.heading + 540.0) % 360.0 - 180.0
        if abs(diff) < 3.0:
            s.heading = target_deg
            await self.push_state()
            return
        s.turning_in_place = True

        spd       = self.cfg.move_speed
        STEER     = 36
        direction = +1 if diff > 0 else -1
        # Сохраняем стартовую позицию — после K-turn вернёмся сюда.
        start_x, start_y = s.x, s.y
        TOL_POS = 1.5      # допуск по позиции (см)
        TOL_HDG = 1.5      # допуск по курсу (град)
        MAX_ITER = 5

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
            # ── Фаза 1: 3-дуговой K-turn ──────────────────────────────
            # Дуга 1: вперед, рулем в нужную сторону
            if alpha_deg > 0.5:
                await self._arc_at_steer(direction * STEER, alpha_deg, spd)
            # Дуга 2: назад, ОБРАТНЫЙ руль (но курс продолжает крутиться
            # в ту же сторону, что и в дуге 1, благодаря смене знака v и steer)
            await self._arc_at_steer(-direction * STEER, beta_deg, spd, backward=True)
            # Дуга 3: вперед, тот же руль, что дуга 1
            if alpha_deg > 0.5:
                await self._arc_at_steer(direction * STEER, alpha_deg, spd)

            # ── Фазы 2+3 итеративно: возврат в точку + коррекция курса ──
            for it in range(MAX_ITER):
                pos_err = math.hypot(start_x - s.x, start_y - s.y)
                hdg_err = (target_deg - s.heading + 540.0) % 360.0 - 180.0
                if pos_err < TOL_POS and abs(hdg_err) < TOL_HDG:
                    break
                # Фаза 2: вернуться в (start_x, start_y), если уехали
                if pos_err >= TOL_POS:
                    if it == 0:
                        await self.push_message(
                            f"Возврат в исходную точку (дрейф {pos_err:.1f} см)…",
                            "info")
                    await self._drive_to_point(start_x, start_y, TOL_POS, spd)
                # Фаза 3: при возврате курс мог сбиться — корректируем
                hdg_err = (target_deg - s.heading + 540.0) % 360.0 - 180.0
                if abs(hdg_err) >= TOL_HDG:
                    await self.push_message(
                        f"Коррекция курса: {hdg_err:+.1f}° (итерация {it+1})",
                        "info")
                    per_arc = 8.0 if it == 0 else 5.0
                    await self._k_turn_arcs(hdg_err, STEER, spd, per_arc_deg=per_arc)
        except asyncio.CancelledError:
            pass
        finally:
            s.speed     = 0
            s.steer     = 0.0
            s.dist_left = 0
            s.turning_in_place = False
            await self.robot.stop()
            await self.robot.set_servo_center()

        # Финальный мягкий снэп ТОЛЬКО курса (позиция уже корректирована Фазой 2).
        s.heading = target_deg
        # При сбросе флага последняя позиция в mission_state могла
        # «застрять» где-то на дуге K-turn. Без сброса первая же проверка
        # отрезка (last_pos → новая позиция) пересечёт всю кривую и могла
        # бы засчитать ложное отклонение или waypoint. Сбрасываем
        # last_robot_pos — следующая проверка пойдёт от текущей позиции.
        if self._mission is not None:
            self._mission.last_robot_pos = None
            self._mission.last_in_margin = True
        await self.push_state()

    async def _k_turn_arcs(self, total_deg: float, STEER: int, spd: int,
                            per_arc_deg: float = 10.0):
        """Серия коротких пар дуг (forward+back) для разворота на total_deg.
        Каждая дуга — per_arc_deg градусов курса; пары симметричны и почти
        не двигают позицию. Знак total_deg задает направление вращения."""
        s = self.robot_state
        if abs(total_deg) < 1.0:
            return
        direction = 1 if total_deg > 0 else -1
        # Делим на четное число дуг, чтобы каждая пара была симметричной
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
        ВПЕРЕД если цель находится впереди (в направлении носа), НАЗАД если сзади.
        Это важно для финальной точной подстановки в исходную точку: если К-turn
        оставил нас слегка впереди старта, разумнее сдать назад, не разворачиваясь.
        timeout — максимальное время поездки (по умолчанию 15 с; для дальних
        перемещений вызывающий код передает пропорциональное расстоянию значение)."""
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
        # Защита от перелета мимо цели: если расстояние начало РАСТИ —
        # значит проехали мимо, надо остановиться и перерасчитать.
        prev_dist  = math.hypot(dx0, dy0)
        growing_ticks = 0
        while loop.time() < deadline:
            dx = tx - s.x
            dy = ty - s.y
            cur_dist = math.hypot(dx, dy)
            if cur_dist < tol_cm:
                break
            # Защита от перелета: расстояние растет 4 тика подряд НА ОЩУТИМУЮ
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
            elif s.speed == 0:                        # уперся в стену — толкнем еще раз
                s.speed = target_speed
                await self.robot.move(int(target_speed))

            if sgn > 0:
                # ВПЕРЕД: курс должен указывать НА цель
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

    def _kturn_forward_clearance_needed(self, delta_deg: float) -> float:
        """Сколько см свободного пространства нужно ВПЕРЁД для K-turn на delta_deg.
        Берётся из геометрии Reeds-Shepp: после первой forward-дуги α робот
        смещается на R·sin(α) вперёд от старта (это максимум forward-сдвига
        за всю последовательность 3 дуг)."""
        delta = abs(delta_deg)
        if delta < 1.0:
            return 0.0
        # Минимальный радиус поворота (см)
        steer_ratio = 36.0 / 45.0   # STEER / max
        if steer_ratio < 1e-6:
            return 0.0
        R = (self.cfg.wheel_circ_cm * 360.0) / (
            2.0 * math.pi * self.cfg.heading_per_rot * steer_ratio)
        # α по формулам K-turn: 2α + β = |Δh|, sin(β/2) = sin(|Δh|/2)/2
        half = math.radians(delta / 2.0)
        s_half = math.sin(half)
        if abs(s_half / 2.0) > 1.0:
            return 0.0
        beta = 2.0 * math.degrees(math.asin(s_half / 2.0))
        alpha = (delta - beta) / 2.0
        return R * math.sin(math.radians(alpha))

    async def _ensure_kturn_clearance(self, target_deg: float) -> bool:
        """Проверяет, помещается ли K-turn на target_deg в свободном пространстве.
        Если ВПЕРЁДНОЙ свободы недостаточно, отъезжает назад на нужную величину
        (в пределах того, сколько места есть СЗАДИ).

        Возвращает True, если был отъезд, False — если уже было достаточно места.
        Курс не меняет."""
        s = self.robot_state
        diff = (float(target_deg) - s.heading + 540.0) % 360.0 - 180.0
        if abs(diff) < 3.0:
            return False
        forward_need  = self._kturn_forward_clearance_needed(abs(diff))
        forward_need += 20.0      # запас на безопасность
        forward_have  = self._wall_dist_cm(s.heading) - self.cfg.wall_thickness_cm
        if forward_have >= forward_need:
            return False          # места хватает, ничего не делаем
        # Не помещается — нужно отъехать назад. Сколько можно?
        bwd_heading  = (s.heading + 180.0) % 360.0
        backward_have = self._wall_dist_cm(bwd_heading) - self.cfg.wall_thickness_cm - 5.0
        backup_need  = forward_need - forward_have
        backup       = min(backup_need, backward_have)
        if backup < 5.0:
            await self.push_message(
                f"⚠ Места для разворота мало (нужно {forward_need:.0f} см впереди, "
                f"есть {forward_have:.0f}; сзади тоже только {backward_have:.0f}). "
                f"Попытаюсь развернуться как есть.", "warning")
            return False
        await self.push_message(
            f"⚠ Для разворота нужно {forward_need:.0f} см впереди, "
            f"а есть только {forward_have:.0f}. Отъезжаю назад на {backup:.0f} см.",
            "warning")
        await self._run_back(backup, self.cfg.move_speed)
        return True

    async def _run_face_cardinal(self, deg: float, label: str):
        await self.push_message(
            f"🧭 Развернуться лицом к {label} (курс {deg:.0f}°).", "info")
        # Если впереди стена ближе, чем требует геометрия K-turn — отъезжаем назад.
        await self._ensure_kturn_clearance(deg)
        await self._k_turn_to_heading(deg)

    def _has_danger_zone_at(self, x: float, y: float, radius: float,
                            tol_cm: float = 1.0) -> bool:
        """Уже ли есть опасная зона (kind="danger") в (x, y) того же радиуса?
        Используется для идемпотентности `mark_danger_cmd` при replay."""
        for z in self.world.danger_zones:
            if getattr(z, "kind", "danger") != "danger":
                continue
            if math.hypot(z.x - x, z.y - y) <= tol_cm and abs(z.radius - radius) <= tol_cm:
                return True
        return False

    async def _run_mark_danger(self,
                                target_x: Optional[float] = None,
                                target_y: Optional[float] = None,
                                radius:   Optional[float] = None,
                                db: Session = None):
        """Поставить опасную зону обстановки.
        Если координаты не заданы — берется текущая позиция робота
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
        """ПРОГРАММНОЕ удаление зоны (красной или жёлтой), в которую
        попадает точка (X, Y). Если координаты не заданы — берётся текущая
        позиция робота.

        Требование: робот ДОЛЖЕН находиться внутри удаляемой зоны.
        Это runtime-сценарий из алгоритма (например, «зона внимания
        больше не актуальна — робот в ней — снимаем»). Если робота в зоне нет —
        отказ с сообщением в журнал.

        Для UI-удаления опасных зон мышью используется отдельный путь
        `_run_remove_danger_zone_at_point` (без проверки робота).

        Возвращает число удалённых зон."""
        s = self.robot_state
        x = float(target_x) if target_x is not None else s.x
        y = float(target_y) if target_y is not None else s.y
        # Проверка: робот сам находится внутри зоны, которую собираемся удалить.
        # Если координаты совпадают с позицией робота (clear_here_cmd) —
        # проверка тривиально пройдёт, т.к. (x,y) и есть позиция робота.
        zone_at_point = self.world.zone_at(x, y)
        if zone_at_point is None:
            await self.push_message(
                f"В точке ({x:.0f}, {y:.0f}) зон не найдено.", "warning")
            return 0
        robot_inside = (math.hypot(s.x - zone_at_point.x, s.y - zone_at_point.y)
                        <= zone_at_point.radius)
        if not robot_inside:
            await self.push_message(
                f"⚠ Программное удаление зоны требует чтобы робот был ВНУТРИ "
                f"неё. Робот ({s.x:.0f}, {s.y:.0f}), зона в "
                f"({zone_at_point.x:.0f}, {zone_at_point.y:.0f}) "
                f"r={zone_at_point.radius:.0f}.", "warning")
            return 0
        removed = self.world.remove_zones_at(x, y)
        if db:
            ids = [z.db_id for z in removed if z.db_id is not None]
            if ids:
                (db.query(DangerZone)
                   .filter(DangerZone.id.in_(ids))
                   .update({"active": False}, synchronize_session=False))
                db.commit()
        await self.push_world()
        return len(removed)

    async def _run_remove_danger_zone_at_point(self,
                                                target_x: float, target_y: float,
                                                db: Session = None) -> int:
        """UI-удаление ОПАСНОЙ зоны мышью (⛯ Режим зон + ПКМ).
        Снимает только зоны типа `kind="danger"`, в которые попадает точка.
        Положение робота не проверяется — это pre-flight чистка карты.
        Зоны внимания (kind="algorithm") не задеваются — они контролируются
        алгоритмом, а не пользователем."""
        x, y = float(target_x), float(target_y)
        removed = self.world.remove_zones_at(x, y, kind="danger")
        if not removed:
            await self.push_message(
                f"В точке ({x:.0f}, {y:.0f}) опасных зон не найдено.",
                "warning")
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
        """Робот доезжает до (X, Y) и помечает там зону внимания (жёлтая
        пунктирная) — это часть алгоритма (не зона обстановки).
        Радиус по умолчанию из cfg."""
        s = self.robot_state
        await self.push_message(
            f"📍 Установить зону внимания в ({target_x:.0f}, {target_y:.0f}).",
            "info")
        # 1) Доехать до точки
        await self._run_goto(float(target_x), float(target_y))
        # 2) Поставить зону внимания в текущей позиции робота
        r = float(radius) if radius is not None else float(self.cfg.danger_zone_radius)
        zone = self.world.add_danger_zone(s.x, s.y,
                                          radius=r,
                                          label="Зона внимания",
                                          kind="algorithm")
        if db:
            dz = DangerZone(user_id=self.user_id, label=zone.label,
                            x=zone.x, y=zone.y, radius=zone.radius,
                            kind="algorithm")
            db.add(dz)
            db.commit()
            zone.db_id = dz.id
        await self.push_world()

    async def _run_reset(self, db: Session = None, keep_mode: bool = False):
        """Сброс поля.

        keep_mode=False (по умолчанию, явная команда «↺ Поле») — полный
        сброс, включая режим (инспектор / осторожно).
        keep_mode=True — частичный сброс для replay ▶ Запуск кода и
        для активации миссии: пользовательский выбор «осторожно» должен
        сохраниться через перезапуск, иначе action-кнопки моргают."""
        s = self.robot_state
        self.world.clear_danger_zones()
        self.world.clear_path()
        self.world.clear_auto_segments()
        s.x         = float(self.cfg.start_x_cm)
        s.y         = float(self.cfg.start_y_cm)
        s.heading   = float(self.cfg.start_heading_deg) % 360
        s.speed     = 0
        s.steer     = 0.0
        s.dist_left = 0
        if not keep_mode:
            s.mode     = "normal"
            s.cautious = False
        s.thinking  = "idle"
        s.light_color = (0, 0, 0)
        # Очищаем накопленную программу — после reset поле «как новое»,
        # старые команды уже не отражают актуальное состояние робота.
        # Replay (▶ Запуск) дёргает reset первым, потом дозаписывает
        # выполняемые команды обратно в _program с актуальными end_x/y —
        # тогда save_custom видит то, что только что выполнилось.
        self._program = []
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
            label = f"Вперед {dist} см ({spd}%)" if dist else f"Вперед ({spd}%)"
        elif intent == "back":
            dist  = nlu.extract_distance(raw)
            spd   = nlu.extract_speed(raw, c.move_speed)
            code  = f"back({dist})" if dist else "back()"
            label = f"Назад {dist} см ({spd}%)" if dist else f"Назад ({spd}%)"
        elif intent == "forward_to_wall":
            spd   = nlu.extract_speed(raw, c.move_speed)
            code  = "forward_to_wall()"
            label = f"Вперед до упора ({spd}%)"
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
            code, label = "figure_eight()", "Восьмерка"
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
        elif intent == "face_to":
            deg = nlu.extract_face_angle(raw)
            if deg is None: return None
            code, label = f"face_cmd({deg})", f"Поворот на {deg}°"
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
                label = f"📍 Зона внимания ({tx:.0f}, {ty:.0f}) r={r:.0f}"
            else:
                code  = f"set_algorithm_zone({tx:.0f}, {ty:.0f})"
                label = f"📍 Зона внимания ({tx:.0f}, {ty:.0f})"
        elif intent == "remove_zone":
            xy = nlu.extract_coordinates(raw)
            if xy is not None:
                tx, ty = xy
                code  = f"remove_zone({tx:.0f}, {ty:.0f})"
                label = f"✕ Убрать зону в ({tx:.0f}, {ty:.0f})"
            else:
                code  = "remove_zone()"
                label = "✕ Убрать зону под роботом"
        elif intent == "remove_danger_zone":
            # UI-команда из «⛯ Режим зон» (ПКМ): pre-flight-чистка карты
            # мышью. В Python-код программы НЕ пишется (cmd.skip_record
            # выставит _dispatch). Координаты обязательны.
            xy = nlu.extract_coordinates(raw)
            if xy is None:
                return None
            tx, ty = xy
            code  = f"# UI: убрать опасную зону в ({tx:.0f}, {ty:.0f})"
            label = f"✕ Убрать опасную зону в ({tx:.0f}, {ty:.0f})"
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

    # Реалистичная реализация маневров через примитивы 1T REX. Эта
    # raw-строка не подвергается %-форматированию (поэтому `% 360.0`
    # в коде не интерпретируется как format-спецификатор).
    # ── Реестр Python-помощников ────────────────────────────────────────
    # Каждый помощник — самостоятельный def-блок. В преамбулу попадают
    # только те, что нужны командам в текущей программе (плюс их зависимости).
    # Ключи в _HELPER_DEPS должны совпадать с _HELPER_CODE; имя def внутри
    # кода — тоже с этим ключом (для дедупа на стороне клиента).
    _HELPER_DEPS = {
        "duration":         [],
        "Odometry":                      [],
        "face_cmd":             ["Odometry", "duration"],
        "drive_loop":    ["Odometry"],
        "goto_cmd":                      ["Odometry", "duration",
                                          "face_cmd", "drive_loop"],
        "home_cmd":                      ["goto_cmd"],
        "follow_path_cmd":        ["Odometry"],
        "to_wall_cmd":    [],
        "back_to_wall_cmd":   [],
        "turn_around_cmd":        [],
        "kturn_cmd":  [],
        "circle_cmd":             [],
        "figure_eight_cmd":       [],
        "spiral_cmd":             [],
        "bypass_cmd":             [],
        "course_cmd":         [],
        "mark_danger_cmd":        [],
        # У 1T REX нет GPS — для «здесь» (under-robot) хелперов нужна Odometry.
        "danger_here_cmd":   ["Odometry"],
        "attention_zone_cmd": ["goto_cmd"],   # goto_cmd транзитивно тянет Odometry
        "remove_zone_cmd":        [],
        "clear_here_cmd":   ["Odometry"],
    }

    # Каждый helper — пара (русский заголовок-комментарий, код).
    # При генерации преамбулы клиент видит обычный Python с комментарием
    # над функцией; дедупликация на стороне клиента — по имени def/class.
    _HELPER_CODE = {
        "duration": (
            "Расчёт длительности движения для нужной дистанции",
            r'''def duration(distance_cm, power_pct=DEFAULT_SPEED):
    speed_cm_s = SPEED_CM_PER_S_AT_100 * abs(power_pct) / 100.0
    return max(0.05, distance_cm / max(1.0, speed_cm_s))
'''),
        "Odometry": (
            "Класс одометрии — отслеживание (x, y, heading) робота",
            r'''class Odometry:
    def __init__(self, x=START_X, y=START_Y, heading=START_HEADING_DEG):
        self.x = float(x); self.y = float(y); self.heading = float(heading)
    def sync_heading(self):
        try: self.heading = float(robot.get_angle()['Z']) % 360.0
        except Exception: pass
    def move_step(self, power_pct, duration_sec, steer_deg=0):
        sign     = 1 if power_pct >= 0 else -1
        cm_per_s = SPEED_CM_PER_S_AT_100 * abs(power_pct) / 100.0
        dist     = cm_per_s * duration_sec
        if steer_deg != 0:
            steer_ratio = steer_deg / 45.0
            d_head = (dist / WHEEL_CIRC_CM) * HEADING_DEG_PER_ROT * steer_ratio * sign
            self.heading = (self.heading + d_head) % 360.0
        h_rad = math.radians(self.heading)
        self.x += sign * math.sin(h_rad) * dist
        self.y += sign * math.cos(h_rad) * dist
odo = Odometry()    # глобальный экземпляр одометрии
'''),
        "face_cmd": (
            "Поворот лицом к стороне света (3-дуговой K-turn)",
            r'''def face_cmd(target_deg, power_pct=DEFAULT_SPEED):
    odo.sync_heading()
    diff = (target_deg - odo.heading + 540.0) % 360.0 - 180.0
    if abs(diff) < 3.0:
        return
    direction = 1 if diff > 0 else -1
    half_rad  = math.radians(abs(diff) / 2.0)
    s_half    = math.sin(half_rad)
    if abs(s_half / 2.0) > 1.0:
        return
    beta_deg  = math.degrees(2.0 * math.asin(s_half / 2.0))
    alpha_deg = (abs(diff) - beta_deg) / 2.0
    steer_ratio = DEFAULT_TURN_ANGLE / 45.0
    def arc(sweep_deg, sign):
        arc_cm = sweep_deg * WHEEL_CIRC_CM / (HEADING_DEG_PER_ROT * steer_ratio)
        secs   = duration(arc_cm, power_pct)
        steer  = direction * DEFAULT_TURN_ANGLE * (-1 if sign < 0 else 1)
        robot.set_angle(int(steer))
        robot.move(sign * power_pct, secs)
        odo.move_step(sign * power_pct, secs, steer)
    arc(alpha_deg, +1)
    arc(beta_deg,  -1)
    arc(alpha_deg, +1)
    robot.stop()
    robot.set_servo_center()
    odo.heading = target_deg % 360.0
'''),
        "drive_loop": (
            "Прямой проезд в (x,y) с пошаговой коррекцией курса по гироскопу",
            r'''def drive_loop(target_x, target_y, power_pct, tolerance_cm,
                               step_sec=0.1, kp=0.7):
    # Едем к точке короткими шагами по step_sec секунд. На каждом шаге:
    #   1) читаем реальный курс с гироскопа,
    #   2) считаем bearing от текущей позиции к цели,
    #   3) подруливаем на угол ~ kp * (bearing - heading) с насыщением.
    # Это закрытая петля: если робота сбило (зацепился, толкнули, дрейф
    # гироскопа), на следующем такте курс будет скорректирован.
    sign = 1 if power_pct >= 0 else -1
    robot.move(power_pct)
    while True:
        odo.sync_heading()
        dx = target_x - odo.x; dy = target_y - odo.y
        dist = math.hypot(dx, dy)
        if dist < tolerance_cm:
            break
        bearing = math.degrees(math.atan2(dx, dy)) % 360.0
        if sign < 0:
            bearing = (bearing + 180.0) % 360.0   # для заднего хода целимся «зеркально»
        diff = (bearing - odo.heading + 540.0) % 360.0 - 180.0
        steer = max(-DEFAULT_TURN_ANGLE, min(DEFAULT_TURN_ANGLE, diff * kp))
        robot.set_angle(int(steer))
        odo.move_step(power_pct, step_sec, steer)
        time.sleep(step_sec)
    robot.stop()
    robot.set_servo_center()
'''),
        "goto_cmd": (
            "Перейти в точку (x, y) — поворот к цели + прямая с коррекцией курса",
            r'''def goto_cmd(target_x, target_y, power_pct=DEFAULT_SPEED, tolerance_cm=5):
    odo.sync_heading()
    dx = target_x - odo.x
    dy = target_y - odo.y
    distance = math.hypot(dx, dy)
    if distance < tolerance_cm:
        return
    target_heading = math.degrees(math.atan2(dx, dy)) % 360.0
    bearing = (target_heading - odo.heading + 540.0) % 360.0 - 180.0
    if abs(bearing) < 30.0:
        # Цель почти прямо — едем сразу с коррекцией курса по гироскопу.
        drive_loop(target_x, target_y, power_pct, tolerance_cm)
    elif abs(bearing) > 150.0:
        # Цель почти позади — едем задом с коррекцией курса.
        drive_loop(target_x, target_y, -power_pct, tolerance_cm)
    else:
        # Общий случай: поворот на месте, потом проезд с коррекцией.
        face_cmd(target_heading, power_pct)
        odo.sync_heading()
        drive_loop(target_x, target_y, power_pct, tolerance_cm)
'''),
        "home_cmd": (
            "Возврат в стартовую точку",
            r'''def home_cmd(power_pct=DEFAULT_SPEED):
    goto_cmd(START_X, START_Y, power_pct)
'''),
        "follow_path_cmd": (
            "Pure-pursuit — следование за списком waypoint'ов",
            r'''def follow_path_cmd(points, power_pct=DEFAULT_SPEED,
                    lookahead_cm=None, tolerance_cm=5):
    if not points or len(points) < 2: return
    if lookahead_cm is None:
        lookahead_cm = max(30.0, ROBOT_LENGTH_CM * 1.5)
    gx, gy = points[-1]
    last_idx = 0
    robot.set_angle(0)
    robot.move(power_pct)
    while True:
        odo.sync_heading()
        if math.hypot(gx - odo.x, gy - odo.y) < tolerance_cm: break
        best_i, best_d2 = last_idx, float('inf')
        for i in range(last_idx, len(points)):
            d2 = (points[i][0] - odo.x)**2 + (points[i][1] - odo.y)**2
            if d2 < best_d2: best_d2, best_i = d2, i
            elif d2 > best_d2 + 100: break
        last_idx = best_i
        target_i, accum = best_i, 0.0
        for i in range(best_i, len(points) - 1):
            accum += math.hypot(points[i+1][0] - points[i][0],
                                points[i+1][1] - points[i][1])
            if accum >= lookahead_cm:
                target_i = i + 1; break
        else:
            target_i = len(points) - 1
        tx, ty = points[target_i]
        bearing = math.degrees(math.atan2(tx - odo.x, ty - odo.y))
        diff = (bearing - odo.heading + 540.0) % 360.0 - 180.0
        steer = max(-DEFAULT_TURN_ANGLE, min(DEFAULT_TURN_ANGLE, diff * 0.7))
        robot.set_angle(int(steer))
        odo.move_step(power_pct, 0.1, steer)
        time.sleep(0.1)
    robot.stop()
    robot.set_servo_center()
'''),
        "to_wall_cmd": (
            "Движение вперёд до препятствия по дальномеру",
            r'''def to_wall_cmd(power_pct=DEFAULT_SPEED, stop_margin_cm=15):
    # robot.get_laser() возвращает расстояние в сантиметрах.
    robot.move(power_pct)
    while True:
        dist = robot.get_laser()
        if dist is None or dist <= stop_margin_cm:
            robot.stop()
            break
        time.sleep(0.05)
'''),
        "back_to_wall_cmd": (
            "Движение назад до препятствия по дальномеру",
            r'''def back_to_wall_cmd(power_pct=DEFAULT_SPEED, stop_margin_cm=15):
    # robot.get_laser() возвращает расстояние в сантиметрах.
    robot.move(-power_pct)
    while True:
        dist = robot.get_laser()
        if dist is None or dist <= stop_margin_cm:
            robot.stop()
            break
        time.sleep(0.05)
'''),
        "turn_around_cmd": (
            "Разворот на 180° (одна K-turn-пара: вперёд по дуге + назад по зеркальной дуге)",
            r'''def turn_around_cmd(direction=1, power_pct=DEFAULT_SPEED):
    # Один K-turn-шаг: робот возвращается в исходную точку,
    # курс развёрнут на 180°. Траектория — «петля» (teardrop).
    robot.set_angle(direction * DEFAULT_TURN_ANGLE)    # руль в сторону разворота
    robot.move(power_pct, 1.5)                         # вперёд по дуге
    robot.set_angle(-direction * DEFAULT_TURN_ANGLE)   # руль в зеркальную сторону
    robot.move(-power_pct, 1.5)                        # назад по зеркальной дуге
    robot.stop()
    robot.set_servo_center()
'''),
        "kturn_cmd": (
            "Разворот на месте (многошаговый K-turn)",
            r'''def kturn_cmd(steps, power_pct=DEFAULT_SPEED):
    for i in range(steps):
        robot.set_angle(DEFAULT_TURN_ANGLE)
        robot.move(power_pct, 0.5)
        robot.set_angle(-DEFAULT_TURN_ANGLE)
        robot.move(-power_pct, 0.5)
    robot.stop()
    robot.set_servo_center()
'''),
        "circle_cmd": (
            "Движение по окружности — один полный круг",
            r'''def circle_cmd(direction=-1, power_pct=DEFAULT_SPEED, full_turn_sec=4.0):
    robot.set_angle(direction * DEFAULT_TURN_ANGLE)
    robot.move(power_pct, full_turn_sec)
    robot.stop()
    robot.set_servo_center()
'''),
        "figure_eight_cmd": (
            "Движение восьмёркой — два круга в противоположные стороны",
            r'''def figure_eight_cmd(direction=-1, power_pct=DEFAULT_SPEED, full_turn_sec=4.0):
    robot.set_angle( direction * DEFAULT_TURN_ANGLE)
    robot.move(power_pct, full_turn_sec)
    robot.set_angle(-direction * DEFAULT_TURN_ANGLE)
    robot.move(power_pct, full_turn_sec)
    robot.stop()
    robot.set_servo_center()
'''),
        "spiral_cmd": (
            "Движение по спирали — линейная интерполяция угла руля",
            r'''def spiral_cmd(direction=-1, power_pct=DEFAULT_SPEED,
               steer_from=36, steer_to=24, turns=2, micro_sec=0.3):
    total_steps = int(turns * 360 / 5)
    for i in range(total_steps):
        t = i / max(1, total_steps - 1)
        steer_deg = steer_from + (steer_to - steer_from) * t
        robot.set_angle(int(direction * steer_deg))
        robot.move(power_pct, micro_sec)
    robot.stop()
    robot.set_servo_center()
'''),
        "bypass_cmd": (
            "Объезд препятствия — S-волна из 4 четверть-дуг",
            r'''def bypass_cmd(start_dir=1, max_steer=36, power_pct=DEFAULT_SPEED, quarter_sec=0.5):
    for phase in range(4):
        sign = start_dir if phase in (0, 3) else -start_dir
        robot.set_angle(sign * max_steer)
        robot.move(power_pct, quarter_sec)
    robot.stop()
    robot.set_servo_center()
'''),
        "course_cmd": (
            "Выставить курс на ходу — пропорциональный регулятор",
            r'''def course_cmd(target_deg, power_pct=DEFAULT_SPEED):
    current = robot.get_angle()['Z']
    diff = (target_deg - current + 180) % 360 - 180
    robot.set_angle(max(-DEFAULT_TURN_ANGLE, min(DEFAULT_TURN_ANGLE, diff)))
    robot.move(power_pct, abs(diff) / 30.0)
    robot.stop()
'''),
        "mark_danger_cmd": (
            "Отметить опасную зону по координатам",
            r'''def mark_danger_cmd(x, y, radius):
    pass
'''),
        "danger_here_cmd": (
            "Отметить опасную зону под текущей позицией робота (по одометрии)",
            r'''def danger_here_cmd(radius=10):
    # У 1T REX нет GPS — текущую позицию берём из нашей одометрии (odo).
    odo.sync_heading()
    # отметить (odo.x, odo.y, radius) на карте
'''),
        "attention_zone_cmd": (
            "Доехать в точку и пометить зону внимания (по одометрии после goto)",
            r'''def attention_zone_cmd(target_x, target_y, radius):
    goto_cmd(target_x, target_y)
    # После goto_cmd одометрия знает где робот — используем её.
    # отметить зону: (odo.x, odo.y, radius)
'''),
        "remove_zone_cmd": (
            "Удалить зону любого типа по координатам",
            r'''def remove_zone_cmd(x, y):
    pass
'''),
        "clear_here_cmd": (
            "Удалить зону под текущей позицией робота (по одометрии)",
            r'''def clear_here_cmd():
    # У 1T REX нет GPS — текущую позицию берём из нашей одометрии (odo).
    odo.sync_heading()
    # удалить зону под (odo.x, odo.y)
'''),
    }

    def _goto_execution_mode(self, cmd) -> tuple[str, float, float]:
        """Определяет, как лучше выполнить команду «иди в точку», исходя из
        ТЕКУЩЕЙ позиции и курса робота:

          • "skip"     — уже на месте (distance < 5 см)
          • "forward"  — курс совпадает с направлением на цель (±5°),
                         можно просто проехать вперёд
          • "backward" — курс противоположен направлению на цель (180°±5°),
                         можно проехать задом без разворота
          • "goto"     — общий случай (нужен полный goto_cmd)
          • "invalid"  — координаты не указаны

        Возвращает (mode, distance_cm, signed_bearing_deg)."""
        ALIGN_TOL = 5.0    # допуск по курсу для оптимизации, градусы
        SKIP_TOL  = 5.0    # уже «на месте», см
        xy = nlu.extract_coordinates(cmd.raw)
        if xy is None:
            return ("invalid", 0.0, 0.0)
        tx, ty = xy
        s = self.robot_state
        dx = float(tx) - float(s.x)
        dy = float(ty) - float(s.y)
        distance = math.hypot(dx, dy)
        if distance < SKIP_TOL:
            return ("skip", 0.0, 0.0)
        target_heading = math.degrees(math.atan2(dx, dy)) % 360.0
        bearing = ((target_heading - float(s.heading) + 540.0) % 360.0) - 180.0
        if abs(bearing) <= ALIGN_TOL:
            return ("forward", distance, bearing)
        if abs(bearing) >= 180.0 - ALIGN_TOL:
            return ("backward", distance, bearing)
        return ("goto", distance, bearing)

    def _helpers_for_cmd(self, cmd) -> list[str]:
        """Какие def-блоки нужны для генерации тела одной команды."""
        intent = cmd.intent
        raw    = cmd.raw
        static = {
            "forward_to_wall":    ["to_wall_cmd"],
            "backward_to_wall":   ["back_to_wall_cmd"],
            "turn_around":        ["turn_around_cmd"],
            "turn_around_place":  ["kturn_cmd"],
            "circle":             ["circle_cmd"],
            "figure_eight":       ["figure_eight_cmd"],
            "spiral_in":          ["spiral_cmd"],
            "spiral_out":         ["spiral_cmd"],
            "bypass_right":       ["bypass_cmd"],
            "bypass_left":        ["bypass_cmd"],
            "set_course":         ["course_cmd"],
            "home":               ["home_cmd"],
            "face_n":             ["face_cmd"],
            "face_ne":            ["face_cmd"],
            "face_e":             ["face_cmd"],
            "face_se":            ["face_cmd"],
            "face_s":             ["face_cmd"],
            "face_sw":            ["face_cmd"],
            "face_w":             ["face_cmd"],
            "face_nw":            ["face_cmd"],
            "face_to":            ["face_cmd"],
            # report_pos/report_status печатают позицию из одометрии —
            # одометрию надо определить даже если команда не двигает робота.
            "report_pos":         ["Odometry"],
            "report_status":      ["Odometry"],
        }
        if intent in static:
            return list(static[intent])
        if intent in ("forward", "back"):
            return ["duration"] if nlu.extract_distance(raw) else []
        if intent == "goto":
            mode, _, _ = self._goto_execution_mode(cmd)
            if mode in ("forward", "backward"):
                # Оптимизация: курс уже совпадает или противоположен —
                # достаточно простого forward/back, тяжёлый goto_cmd не нужен.
                return ["duration"]
            if mode == "goto":
                return ["goto_cmd"]
            return []   # "skip" — команда не записывается, helpers не нужны
        if intent == "mark_danger":
            return ["mark_danger_cmd"] if nlu.extract_coordinates(raw) is not None else ["danger_here_cmd"]
        if intent == "set_algorithm_zone":
            return ["attention_zone_cmd"] if nlu.extract_coordinates(raw) is not None else []
        if intent == "remove_zone":
            return ["remove_zone_cmd"] if nlu.extract_coordinates(raw) is not None else ["clear_here_cmd"]
        return []

    def _collect_helpers(self, cmds) -> list[str]:
        """Сжать список команд до плоского, топологически отсортированного
        списка имён нужных def-блоков (с транзитивными зависимостями)."""
        seen: list[str] = []
        def add(name: str):
            if name in seen:
                return
            for dep in self._HELPER_DEPS.get(name, []):
                add(dep)
            seen.append(name)
        for cmd in cmds:
            for h in self._helpers_for_cmd(cmd):
                add(h)
        return seen

    def _python_constants_block(self) -> str:
        c = self.cfg
        return (
            "# Python-скрипт для 1T REX. Описание системы команд — раздел «Справка → API 1T REX».\n"
            "import math, time\n"
            "\n"
            "# ── Параметры движения по умолчанию ──────────────────────────\n"
            f"DEFAULT_SPEED         = {c.move_speed}     # мощность мотора по умолчанию, % (диапазон -100..+100)\n"
            f"DEFAULT_TURN_ANGLE    = {c.turn_angle}     # угол руля по умолчанию, ° (диапазон -45..+45; 0 = прямо)\n"
            "\n"
            "# ── Калибровка одометрии (зависит от шасси и батареи) ────────\n"
            f"SPEED_CM_PER_S_AT_100 = {c.speed_at_100:.1f}   # реальная скорость при 100% мощности, см/с\n"
            f"WHEEL_CIRC_CM         = {c.wheel_circ_cm:.2f}  # длина окружности колеса, см (для D90 ≈ π × 9 ≈ 28.3)\n"
            f"HEADING_DEG_PER_ROT   = {c.heading_per_rot:.2f}  # изменение курса за оборот колеса при max угле руля, °\n"
            "\n"
            "# ── Геометрия мира и робота (для проверки проходимости) ──────\n"
            f"WALL_THICKNESS_CM     = {c.wall_thickness_cm:.1f}    # толщина стен арены, см\n"
            f"ROBOT_LENGTH_CM       = {c.robot_length_cm:.1f}   # длина робота от носа до кормы, см\n"
            f"ROBOT_WIDTH_CM        = {c.robot_width_cm:.1f}   # ширина робота, см\n"
            "\n"
            "# ── RGB-индикатор на плате ───────────────────────────────────\n"
            f"LIGHT_INDEX           = {LIGHT_INDEX}      # индекс первого LED в ленте (0 = первый)\n"
            f"LIGHT_COUNT           = {LIGHT_COUNT}      # сколько LED подряд зажигать одной командой\n"
            f"LIGHT_DEFAULT_COLOR   = {LIGHT_DEFAULT_COLOR}  # цвет «по умолчанию», кортеж (R, G, B) 0..255\n"
            "\n"
            "# ── Стартовая точка робота — «домой» и сброс одометрии ──────\n"
            "# X/Y — координаты в системе мира (числа, без единиц).\n"
            "# Курс 0° = «север» (вверх по экрану).\n"
            f"START_X               = {c.start_x_cm:.1f}     # стартовая X\n"
            f"START_Y               = {c.start_y_cm:.1f}     # стартовая Y\n"
            f"START_HEADING_DEG     = {c.start_heading_deg:.1f}     # стартовый курс, ° (0=N, 90=E, 180=S, 270=W)\n"
        )

    def _python_code_preamble(self, cmds=None) -> str:
        """Преамбула: константы → сентинель → нужные def-блоки.
        Сентинель идёт до helpers, чтобы клиентский appendPythonCode
        (который берёт только текст ПОСЛЕ сентинеля) видел def-блоки
        в «теле» каждой команды и мог их дедупить по имени def/class.
        Над каждой функцией — короткий русский комментарий.
        cmds=None — без helpers (пустая программа); cmds=[..] — для них."""
        out  = self._python_constants_block()
        out += "\n# === НАЧАЛО ПРОГРАММЫ ===\n"
        if cmds:
            for name in self._collect_helpers(cmds):
                description, code = self._HELPER_CODE[name]
                code = code.strip("\n")
                out += f"\n# {description}\n{code}\n"
        return out

    def _python_code_for_cmd(self, cmd: RobotCmd) -> tuple[str, str]:
        """Полный Python-код одной команды для отправки клиенту:
        константы → сентинель → нужные def-блоки → строки вызова."""
        description = cmd.label
        body = "\n".join(self._python_call_lines_for_cmd(cmd))
        full = self._python_code_preamble([cmd]) + body
        return description, full

    # Атомарные команды — те, чьё тело состоит из сырых `robot.X(...)`
    # вызовов (forward, steer, stop, light и т.п.). Парсер не может извлечь
    # из них исходную команду (regex DSL не пропускает точку в `robot.set_angle`),
    # поэтому для них генерируется маркер `# CMD: name(args)` — единственный
    # источник истины при «Запуске».
    # Для compound-команд (face_cmd, goto_cmd, circle_cmd, …) маркер
    # не нужен: их call-строка сама по себе парсится напрямую.
    _ATOMIC_INTENTS = frozenset({
        "forward", "back",
        "steer_right", "steer_left", "steer_right_small", "steer_left_small",
        "steer_center", "brake", "stop", "set_speed",
        "pause", "light_on", "light_off", "light_color", "recharge",
        "mode_inspector", "mode_cautious", "path_show", "path_hide",
        "reset", "report_pos", "report_status",
    })

    def _python_call_lines_for_cmd(self, cmd: RobotCmd) -> list[str]:
        """Строки вызова команды (без преамбулы и def-блоков).

        Для атомарных команд (raw robot.X-вызовы) добавляется маркер
        `# CMD: name(args)` сверху — иначе парсер на «Запуск» не сможет
        восстановить команду из текста textarea (regex DSL не матчит
        строки вида `robot.set_angle(13)`)."""
        raw = cmd.raw
        intent = cmd.intent
        c = self.cfg
        default_speed = c.move_speed
        dist = nlu.extract_distance(raw)
        cur_steer = int(self.robot_state.steer)
        code_lines: list[str] = []
        if intent in self._ATOMIC_INTENTS:
            code_lines.append(f"# CMD: {cmd.code}")

        def steer(angle_expr: str) -> str: return f"robot.set_angle({angle_expr})"
        def move(power: str, dur: str) -> str: return f"robot.move({power}, {dur})"
        def stop() -> str: return "robot.stop()"
        def light(c_expr: str) -> str: return f"robot.set_rgb(LIGHT_INDEX, {c_expr}, LIGHT_COUNT)"

        if intent == "forward":
            if dist:
                code_lines += [
                    f"{steer(str(cur_steer))}                      # руль текущий ({cur_steer}°)",
                    f"{move(str(default_speed), f'duration({dist}, {default_speed})')}  # вперёд {dist} см",
                    f"{stop()}                            # остановка",
                ]
            else:
                code_lines += [
                    f"{steer(str(cur_steer))}                      # руль текущий ({cur_steer}°)",
                    f"{move(str(default_speed), '1.0')}                # вперёд 1 секунду",
                ]
        elif intent == "back":
            if dist:
                code_lines += [
                    f"{steer(str(cur_steer))}                      # руль текущий ({cur_steer}°)",
                    f"{move(f'-{default_speed}', f'duration({dist}, {default_speed})')}  # назад {dist} см",
                    f"{stop()}                            # остановка",
                ]
            else:
                code_lines += [
                    f"{steer(str(cur_steer))}                      # руль текущий ({cur_steer}°)",
                    f"{move(f'-{default_speed}', '1.0')}               # назад 1 секунду",
                ]
        elif intent == "forward_to_wall":
            code_lines += [steer(str(cur_steer)),
                           f"to_wall_cmd({default_speed})  # вперёд до стены"]
        elif intent == "backward_to_wall":
            code_lines += [steer(str(cur_steer)),
                           f"back_to_wall_cmd({default_speed})  # назад до стены"]
        elif intent == "brake":
            code_lines += [f"{stop()}  # тормоз"]
        elif intent == "stop":
            code_lines += [f"{stop()}  # остановка"]
        elif intent in ("steer_right", "steer_left", "steer_right_small", "steer_left_small"):
            steer_delta = nlu.extract_angle(raw) or (nlu.SMALL_STEER_DEG if intent.endswith("small") else c.turn_angle)
            if intent in ("steer_left", "steer_left_small"):
                steer_delta = -steer_delta
            side = "налево" if steer_delta < 0 else "направо"
            code_lines += [f"{steer(str(steer_delta))}  # руль {side} на {abs(steer_delta):g}°"]
        elif intent == "steer_center":
            code_lines += ["robot.set_angle(0)  # руль прямо"]
        elif intent == "set_speed":
            spd = nlu.extract_speed(raw, default_speed)
            code_lines += [f"DEFAULT_SPEED = {spd}  # установить скорость по умолчанию"]
        elif intent == "turn_around":
            direction = -1 if "налево" in nlu.norm(raw) or "влево" in nlu.norm(raw) or "против" in nlu.norm(raw) else 1
            side = "налево" if direction < 0 else "направо"
            code_lines += [f"turn_around_cmd({direction})  # разворот на 180° {side}"]
        elif intent == "turn_around_place":
            steps = nlu.extract_kturn_steps(raw)
            code_lines += [f"kturn_cmd({steps})  # разворот на месте за {steps} шагов"]
        elif intent == "circle":
            n = nlu.norm(raw)
            direction = +1 if any(w in n for w in ("направо", "вправо", "по часовой")) else -1
            side = "по часовой" if direction > 0 else "против часовой"
            code_lines += [f"circle_cmd({direction})  # окружность {side}"]
        elif intent == "figure_eight":
            n = nlu.norm(raw)
            direction = +1 if any(w in n for w in ("направо", "вправо", "по часовой")) else -1
            side = "первый круг по часовой" if direction > 0 else "первый круг против часовой"
            code_lines += [f"figure_eight_cmd({direction})  # восьмёрка ({side})"]
        elif intent == "spiral_out":
            n = nlu.norm(raw)
            direction = +1 if any(w in n for w in ("направо", "вправо", "по часовой")) else -1
            side = "по часовой" if direction > 0 else "против часовой"
            code_lines += [f"spiral_cmd(direction={direction}, steer_from=36, steer_to=24)  # спираль наружу, {side}"]
        elif intent == "spiral_in":
            n = nlu.norm(raw)
            direction = +1 if any(w in n for w in ("направо", "вправо", "по часовой")) else -1
            side = "по часовой" if direction > 0 else "против часовой"
            code_lines += [f"spiral_cmd(direction={direction}, steer_from=24, steer_to=36)  # спираль внутрь, {side}"]
        elif intent in ("bypass_right", "bypass_left"):
            start_dir = +1 if intent == "bypass_right" else -1
            side = "справа" if start_dir > 0 else "слева"
            code_lines += [f"bypass_cmd(start_dir={start_dir})  # объезд препятствия {side}"]
        elif intent == "goto":
            xy = nlu.extract_coordinates(raw)
            if xy is None:
                code_lines += ["# координаты не указаны"]
            else:
                tx, ty = xy
                # Оптимизация: если курс уже совпадает (или противоположен) —
                # генерируем простой forward/back вместо тяжёлого goto_cmd.
                # Режим "skip" (робот уже в tolerance от цели): микро-движения
                # не реализуем (см. диспетчер — там подсказка пользователю).
                # В код НИЧЕГО не пишем, и команда не записывается в программу.
                mode, distance, bearing = self._goto_execution_mode(cmd)
                if mode == "skip":
                    cmd.skip_record = True
                    # пусто — return [] упадёт ниже как code_lines
                elif mode == "forward":
                    code_lines += [
                        "robot.set_angle(0)",
                        f"robot.move({default_speed}, duration({distance:.0f}, {default_speed}))  # вперёд {distance:.0f} см к ({tx:g}, {ty:g})",
                        "robot.stop()",
                    ]
                elif mode == "backward":
                    code_lines += [
                        "robot.set_angle(0)",
                        f"robot.move(-{default_speed}, duration({distance:.0f}, {default_speed}))  # задом {distance:.0f} см к ({tx:g}, {ty:g})",
                        "robot.stop()",
                    ]
                else:
                    # mode == "goto" или "skip": в обоих случаях пишем вызов.
                    # При "skip" goto_cmd сама вернётся рано по tolerance.
                    code_lines += [f"goto_cmd({tx:g}, {ty:g})  # перейти в точку ({tx:g}, {ty:g})"]
        elif intent == "home":
            code_lines += ["home_cmd()  # вернуться в стартовую точку"]
        elif intent in ("face_n", "face_ne", "face_e", "face_se",
                         "face_s", "face_sw", "face_w", "face_nw"):
            cardinal_deg = {"face_n":0, "face_ne":45, "face_e":90, "face_se":135,
                             "face_s":180, "face_sw":225, "face_w":270, "face_nw":315}
            cardinal_lbl = {"face_n":"на север",      "face_ne":"на северо-восток",
                            "face_e":"на восток",     "face_se":"на юго-восток",
                            "face_s":"на юг",         "face_sw":"на юго-запад",
                            "face_w":"на запад",      "face_nw":"на северо-запад"}
            tgt = cardinal_deg[intent]
            lbl = cardinal_lbl[intent]
            code_lines += [f"face_cmd({tgt})  # {lbl}"]
        elif intent == "face_to":
            deg = nlu.extract_face_angle(raw)
            if deg is None:
                code_lines += ["# угол поворота не распознан"]
            else:
                code_lines += [f"face_cmd({deg})  # поворот на месте на {deg}°"]
        elif intent == "set_course":
            target = nlu.extract_course(raw)
            if target is None:
                code_lines += ["# курс не распознан"]
            else:
                code_lines += [f"course_cmd({target})  # выставить курс {target}°"]
        elif intent == "mark_danger":
            xy = nlu.extract_coordinates(raw)
            r  = nlu.extract_radius(raw)
            if xy is None:
                code_lines += ["danger_here_cmd()  # отметить опасную зону здесь"]
            else:
                tx, ty = xy
                radius_arg = f"{r:g}" if r is not None else f"{c.danger_zone_radius:g}"
                code_lines += [f"mark_danger_cmd({tx:g}, {ty:g}, {radius_arg})  # опасная зона в ({tx:g}, {ty:g})"]
        elif intent == "pause":
            secs = nlu.extract_pause_seconds(raw)
            code_lines += [
                "robot.stop()                      # пауза начало",
                f"time.sleep({secs:g})                  # подождать {secs:g} с",
            ]
        elif intent == "set_algorithm_zone":
            xy = nlu.extract_coordinates(raw)
            r  = nlu.extract_radius(raw)
            if xy is None:
                code_lines += ["# координаты не указаны"]
            else:
                tx, ty = xy
                radius_arg = f"{r:g}" if r is not None else f"{c.danger_zone_radius:g}"
                code_lines += [f"attention_zone_cmd({tx:g}, {ty:g}, {radius_arg})  # доехать в ({tx:g}, {ty:g}) и пометить зону внимания"]
        elif intent == "remove_zone":
            xy = nlu.extract_coordinates(raw)
            if xy is None:
                code_lines += ["clear_here_cmd()  # удалить зону под роботом"]
            else:
                tx, ty = xy
                code_lines += [f"remove_zone_cmd({tx:g}, {ty:g})  # удалить зону, накрывающую ({tx:g}, {ty:g})"]
        elif intent == "mode_inspector":
            code_lines += ["# режим «инспектор» — интерфейсный, нет команды робота"]
        elif intent == "mode_cautious":
            code_lines += ["# режим «осторожно» — интерфейсный, нет команды робота"]
        elif intent == "path_show":
            code_lines += ["# показать путь — интерфейсное"]
        elif intent == "path_hide":
            code_lines += ["# скрыть путь — интерфейсное"]
        elif intent == "reset":
            code_lines += [f"{stop()}  # сброс — остановка"]
        elif intent == "report_pos":
            # У 1T REX нет GPS — позиция из нашей одометрии, курс с гироскопа.
            code_lines += [
                "odo.sync_heading()",
                "print(f'позиция: ({odo.x:.1f}, {odo.y:.1f}) курс: {odo.heading:.0f}°')",
            ]
        elif intent == "report_status":
            # Тоже только через реальные API: позиция/курс из одометрии,
            # дальномер и цвет с датчиков. Скорости/заряда у платы не вытащить.
            code_lines += [
                "odo.sync_heading()",
                "laser = robot.get_laser()",
                "print(f'позиция: ({odo.x:.1f}, {odo.y:.1f}) курс: {odo.heading:.0f}° '",
                "      f'лазер: {laser} см')",
            ]
        elif intent == "light_on":
            code_lines += [f"{light('LIGHT_DEFAULT_COLOR')}  # включить световой индикатор"]
        elif intent == "light_off":
            code_lines += [f"{light('(0, 0, 0)')}  # выключить световой индикатор"]
        elif intent == "light_color":
            color = nlu.extract_color(raw) or LIGHT_DEFAULT_COLOR
            code_lines += [f"{light(str(color))}  # цвет индикатора {color}"]
        elif intent == "recharge":
            code_lines += [
                "# зарядка батареи (в симуляторе — мгновенно):",
                "# robot.wait_for_charge(target_pct=100)",
            ]
        else:
            code_lines += ["# нет шаблона"]
        return code_lines

    # ── Выполнение одной команды ─────────────────────────────────────────────

    async def _dispatch(self, cmd: RobotCmd, db: Session = None):
        s = self.robot_state
        c = self.cfg
        intent = cmd.intent
        raw    = cmd.raw
        msg    = ""
        ok     = True

        # ── Кодогенерация ПЕРЕД выполнением ────────────────────────────
        # Чтобы оптимизации в _python_call_lines_for_cmd (например, для goto)
        # видели позицию РОБОТА ДО команды, а не после её исполнения.
        # Если бы мы делали кодоген после run, для goto(50,50) состояние
        # robot_state уже было бы (50,50) → распознавалось бы как «уже на месте».
        if not cmd.playback:
            python_desc, python_code = self._python_code_for_cmd(cmd)
        else:
            python_desc, python_code = None, None

        if intent == "forward":
            dist = nlu.extract_distance(raw)
            spd  = nlu.extract_speed(raw, c.move_speed)
            await self._run_forward(dist, spd)
            msg = f"Вперед {dist} см, {spd}%." if dist else f"Вперед, {spd}%."
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
            # Перед разворотом: проверка clearance, при необходимости отъезд назад.
            target_180 = (s.heading + 180.0) % 360.0
            await self._ensure_kturn_clearance(target_180)
            await self._k_turn_n(direction, steps=1)
            msg = f"Разворот {side} завершен."
        elif intent == "turn_around_place":
            n = nlu.norm(raw)
            direction = -1 if any(w in n for w in ("налево", "влево", "против")) else 1
            steps = nlu.extract_kturn_steps(raw)
            target_180 = (s.heading + 180.0) % 360.0
            await self._ensure_kturn_clearance(target_180)
            await self._k_turn_n(direction, steps)
            msg = "Разворот на месте завершен."
        elif intent == "circle":
            # По умолчанию CCW (против часовой, мат. направление 0→2π).
            # Только если явно сказано «направо/вправо/по часовой» — CW.
            n = nlu.norm(raw)
            direction = +1 if any(w in n for w in ("направо", "вправо", "по часовой")) else -1
            await self._run_circle(direction)
            msg = "Круг завершен (" + ("по часовой" if direction > 0 else "против часовой") + ")."
        elif intent == "figure_eight":
            n = nlu.norm(raw)
            direction = +1 if any(w in n for w in ("направо", "вправо", "по часовой")) else -1
            await self._run_figure_eight(direction)
            msg = "Восьмерка завершена."
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
            msg = "Объезд справа завершен."
        elif intent == "bypass_left":
            await self._run_bypass(start_dir=-1)
            msg = "Объезд слева завершен."
        elif intent == "goto":
            xy = nlu.extract_coordinates(raw)
            if xy is None:
                msg, ok = "Координаты не распознаны (нужно: 'в точку X Y').", False
            else:
                tx, ty = xy
                # Запоминаем дистанцию ДО запуска: если робот уже в tolerance
                # от цели — _run_goto ничего не сделает, нужна другая формулировка.
                pre_dist = math.hypot(tx - s.x, ty - s.y)
                await self._run_goto(tx, ty)
                if pre_dist < 5.0:
                    # Робот не двигался — показываем его реальную позицию,
                    # а не цель (иначе пользователь видит ложное «я в (tx,ty)»).
                    msg = f"Остался в точке ({s.x:.0f}, {s.y:.0f})."
                else:
                    msg = f"Прибыл в окрестность ({tx:.0f}, {ty:.0f})."
        elif intent == "home":
            await self._run_home()
            msg = "🏠 Возврат в стартовую точку завершен."
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
        elif intent == "face_to":
            deg = nlu.extract_face_angle(raw)
            if deg is None:
                msg, ok = "Угол не распознан (нужно: 'поверни на 70').", False
            else:
                await self._run_face_cardinal(deg, f"{deg}°")
                msg = f"Поворот на месте на {deg}° завершен."
        elif intent == "set_course":
            target = nlu.extract_course(raw)
            if target is not None:
                await self._run_set_course(target)
                msg = f"Курс {target}° выставлен."
            else:
                msg, ok = "Курс не распознан.", False
        elif intent == "mark_danger":
            # Опасные зоны — часть pre-flight обстановки. На replay строки
            # `mark_danger_cmd(...)` нужно воспроизводить, но идемпотентно:
            # если в этой точке уже есть опасная зона того же радиуса —
            # пропускаем, чтобы не плодить дубликаты при повторных запусках.
            if cmd.playback:
                xy = nlu.extract_coordinates(raw)
                r  = nlu.extract_radius(raw) or float(self.cfg.danger_zone_radius)
                if xy is not None and self._has_danger_zone_at(xy[0], xy[1], r):
                    msg = (f"⏵ mark_danger пропущен: зона в "
                           f"({xy[0]:.0f}, {xy[1]:.0f}) уже на карте.")
                elif xy is not None:
                    tx, ty = xy
                    await self._run_mark_danger(tx, ty, r, db)
                    msg = (f"⏵ Восстановлена опасная зона в "
                           f"({tx:.0f}, {ty:.0f}), радиус {r:.0f}.")
                else:
                    msg, ok = "mark_danger: координаты не указаны.", False
            else:
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
                # Mission tracking: матч с обязательным place_attention
                self._mission_check_action("place_attention", tx, ty)
        elif intent == "remove_danger_zone":
            # UI-удаление ОПАСНОЙ зоны мышью (⛯ Режим зон + ПКМ).
            # Не записывается в Python-код программы (это pre-flight чистка
            # карты, а не runtime-команда алгоритма).
            xy = nlu.extract_coordinates(raw)
            if xy is None:
                msg, ok = "remove_danger_zone: координаты не указаны.", False
            else:
                tx, ty = xy
                n_removed = await self._run_remove_danger_zone_at_point(tx, ty, db)
                if n_removed > 0:
                    # Взаимное гашение mark_danger ↔ remove_danger_zone (та же
                    # сессия): обе команды убираются из программы целиком.
                    cancelled = self._cancel_matching_mark_danger(tx, ty)
                    if cancelled:
                        await self.push_program()
                        msg = (f"↺ Опасная зона в ({tx:.0f}, {ty:.0f}) "
                               f"поставлена и удалена в этой же сессии — "
                               f"команды взаимно погашены.")
                    else:
                        msg = (f"Удалена опасная зона в ({tx:.0f}, {ty:.0f}).")
                    # В любом случае remove_danger_zone не записывается в код:
                    # это UI-команда, а не runtime-действие алгоритма.
                    cmd.skip_record = True
                    # Mission tracking: матч с обязательным remove_danger
                    self._mission_check_action("remove_danger", tx, ty)
                else:
                    msg, ok = (f"В точке ({tx:.0f}, {ty:.0f}) опасных зон "
                               f"не найдено."), False
        elif intent == "remove_zone":
            # ПРОГРАММНАЯ команда (из кода/голоса): требует робот внутри зоны.
            xy = nlu.extract_coordinates(raw)
            tx, ty = (xy if xy is not None else (s.x, s.y))
            n_removed = await self._run_remove_zone(tx, ty, db)
            if n_removed > 0:
                msg = (f"Удалено зон: {n_removed} "
                       f"в точке ({tx:.0f}, {ty:.0f}).")
                # Mission: можем удалять и опасные, и зоны внимания.
                # Пробуем оба типа — try_match_action ничего не сделает,
                # если такого action нет в required.
                self._mission_check_action("remove_danger", tx, ty)
                self._mission_check_action("remove_attention", tx, ty)
            else:
                # _run_remove_zone уже отправил конкретную причину
                # (нет зон / робот не внутри) — здесь msg оставляем пустым.
                msg, ok = "", False
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
            # При playback (replay программы) сохраняем режим
            # «осторожно» — иначе ▶ Запуск кода каждый раз гасит его
            # и action-кнопки моргают между жёлтым и синим.
            await self._run_reset(db, keep_mode=bool(cmd.playback))
            msg = "Поле очищено."
        elif intent == "report_pos":
            msg = f"X={s.x:.0f}, Y={s.y:.0f}, курс={s.heading:.0f}°."
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

        # python_desc / python_code были сгенерированы в начале _dispatch
        # ДО выполнения команды (с использованием pre-команды robot_state).

        if db and self._db_session_id:
            db.add(CommandLog(session_id=self._db_session_id,
                              raw_text=raw, intent=intent, success=ok))
            db.add(PathPoint(session_id=self._db_session_id,
                             x=s.x, y=s.y, heading=s.heading))
            db.commit()

        self.world.add_path(s.x, s.y)
        await self.push_state()
        if msg:
            # Если команда «погашена» (skip_record) — НЕ отправляем code,
            # иначе клиент впишет её в textarea как обычно.
            send_code = python_code if (ok and not cmd.skip_record) else None
            send_desc = python_desc if (ok and not cmd.skip_record) else None
            await self.push_message(msg, "success" if ok else "warning",
                                    code=send_code,
                                    description=send_desc)

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

                if (success and not cmd.skip_record
                        and cmd.intent not in _NO_RECORD):
                    if cmd.intent != "reset":
                        # Снимок позиции робота ПОСЛЕ исполнения — для
                        # последующего сохранения кастомной миссии.
                        cmd.end_x       = round(self.robot_state.x, 1)
                        cmd.end_y       = round(self.robot_state.y, 1)
                        cmd.end_heading = round(self.robot_state.heading, 1)
                        self._program.append(cmd)
                        # Сохраняем _program в БД ТОЛЬКО для voice/manual:
                        # replay (playback=True) не должен переписывать
                        # сохранённую программу — у неё уже есть свой
                        # источник истины (textarea/_program до replay).
                        if not cmd.playback:
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

    _DSL_LINE   = re.compile(r'^\s*([a-z_]+)\s*\(\s*([^)]*)\s*\)\s*(?:#.*)?$')
    _CMD_MARKER = re.compile(r'^\s*#\s*CMD\s*:\s*([a-z_]+)\s*\(\s*([^)]*)\s*\)')

    @staticmethod
    def _parse_dsl_line(fn: str, args: str) -> Optional[tuple[str, str]]:
        """Преобразует вызов DSL в (intent, raw_text для NLU). None если неизвестен.

        Принимает как короткую форму DSL (`forward(100)`, `mark_danger(0,0,10)`),
        так и Python-инвокации тел `X_cmd(args)` / `X_here_cmd()`, которые
        генерируются для реального робота. Также поддерживает старый префиксный
        формат `cmd_X(args)` для обратной совместимости (импорт старых
        сохранённых программ)."""
        # ── Нормализация имени:
        #   X_here_cmd  → X (без аргументов, _here → берём текущую позицию)
        #   X_cmd       → X
        #   cmd_X       → X (legacy)
        #   X_here      → X (без аргументов)
        #   X           → X
        if fn.endswith("_cmd"):
            fn = fn[:-4]
        elif fn.startswith("cmd_"):
            fn = fn[4:]
        if fn.endswith("_here"):
            fn = fn[:-5]
            args = ""    # _here-варианты вызываются без координат
        args = args.strip()
        # Алиасы коротких имён хелперов → канонические intent-имена.
        # Нужно для парсинга нового codegen (face_cmd, kturn_cmd, ...)
        # и старых сохранённых программ (face_cardinal_cmd → face_cardinal).
        _FN_ALIASES = {
            "to_wall":         "forward_to_wall",
            "back_to_wall":    "backward_to_wall",
            "kturn":           "turn_around_place",
            "face":            "face_cardinal",
            "course":          "set_course",
            "attention_zone":  "set_algorithm_zone",
            "danger":          "mark_danger",
            "clear":           "remove_zone",
        }
        fn = _FN_ALIASES.get(fn, fn)
        if fn == "reset":            return ("reset", "Вега новое поле")
        if fn == "brake":            return ("brake", "Вега тормоз")
        if fn == "stop":             return ("stop",  "Вега стоп")
        if fn == "forward":
            if args.lstrip("+-").isdigit():
                return ("forward", f"Вега вперед {int(args)} см")
            return ("forward", "Вега вперед")
        if fn == "back":
            if args.lstrip("+-").isdigit():
                return ("back", f"Вега назад {int(args)} см")
            return ("back", "Вега назад")
        if fn == "forward_to_wall":  return ("forward_to_wall", "Вега вперед до упора")
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
        if fn == "figure_eight":     return ("figure_eight", "Вега восьмерка")
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
        if fn == "face_cardinal":
            # face_cardinal(deg): если угол ≈ кардинальной точке (±2°) →
            # делегируем соответствующему face_X-интенту. Иначе — это
            # «поворот на произвольный угол» (face_to), точное значение
            # сохраняется как есть.
            try:
                deg = float(args.split(",")[0]) % 360
            except (ValueError, IndexError):
                return None
            cardinals = [
                (0,   "face_n",  "Вега на север"),
                (45,  "face_ne", "Вега на северо-восток"),
                (90,  "face_e",  "Вега на восток"),
                (135, "face_se", "Вега на юго-восток"),
                (180, "face_s",  "Вега на юг"),
                (225, "face_sw", "Вега на юго-запад"),
                (270, "face_w",  "Вега на запад"),
                (315, "face_nw", "Вега на северо-запад"),
            ]
            best = min(cardinals,
                       key=lambda c: min(abs(deg - c[0]), 360 - abs(deg - c[0])))
            best_diff = min(abs(deg - best[0]), 360 - abs(deg - best[0]))
            if best_diff < 2.0:
                return (best[1], best[2])
            # Произвольный угол (не кардинальный) → face_to
            return ("face_to", f"Вега поверни на {int(deg)}")
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

    def _parse_program_text(self, text: str) -> list[RobotCmd]:
        """Парсит команды из textarea.

        Источник истины (в порядке приоритета):
          1) Маркер `# CMD: name(args)` — для АТОМАРНЫХ команд (forward, steer,
             stop, light, …), чьё тело — сырые `robot.X(...)` вызовы. Сами
             эти вызовы regex `_DSL_LINE` не пропускает (точка в имени).
          2) Строка вызова `name(args)` (без точек) — для COMPOUND-команд
             (`circle_cmd(args)`, `face_cmd(180)`, `goto_cmd(x,y)`),
             а также короткая DSL-форма (`forward(100)`, `set_course(90)`)
             и legacy-префикс `cmd_X(args)`.

        Игнорируется:
          • пустые строки и обычные `# комментарии` (не CMD-маркеры);
          • def/class и сырые `robot.X(...)` вызовы (отсекает regex);
          • reset/brake/stop — они оборачивают воспроизведение автоматически."""
        cmds: list[RobotCmd] = []

        for line in text.split("\n"):
            stripped = line.strip()
            if not stripped:
                continue
            # 1) Маркер `# CMD: foo(args)` — приоритетно. Это единственный
            #    способ распознать атомарные команды.
            mm = self._CMD_MARKER.match(stripped)
            if mm:
                fn, args = mm.group(1), mm.group(2)
                parsed = self._parse_dsl_line(fn, args)
                if parsed:
                    intent, raw = parsed
                    if intent not in ("reset", "brake", "stop"):
                        cmd = self._build_cmd(intent, raw)
                        if cmd:
                            cmds.append(cmd)
                continue
            # Любые другие комментарии — пропускаем.
            if stripped.startswith("#"):
                continue
            # 2) Простой вызов name(args). def/class/многострочные выражения
            #    в _DSL_LINE не подходят — отсекаются регуляркой. Сырые
            #    `robot.X(...)` тоже отсекаются (точка в имени).
            md = self._DSL_LINE.match(stripped)
            if not md:
                continue
            fn_raw, args_raw = md.group(1), md.group(2)
            parsed = self._parse_dsl_line(fn_raw, args_raw)
            if not parsed:
                continue
            intent, raw = parsed
            if intent in ("reset", "brake", "stop"):
                continue
            cmd = self._build_cmd(intent, raw)
            if cmd:
                cmds.append(cmd)
        return cmds

    async def run_textarea_program(self, text: str):
        """Парсит текст из textarea, перестраивает self._program и запускает.
        Это путь по которому нажатие «▶ Запуск» уважает редактирование пользователя.

        НЕ вызываем push_program() после парсинга: иначе текстарея перерисуется
        из self._program и пользовательские правки в теле кода (например,
        дополнительные `mark_danger_cmd(10,10,30)`) исчезнут. Текстарея —
        master, self._program — ее зеркало для физики."""
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
        НЕ запускает автоматически — пользователь сам нажмет ▶ Запуск."""
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

        # Зарядка — мгновенное действие, не должна ждать в очереди маневров.
        if intent == "recharge":
            await self._run_recharge()
            await self.push_state()
            return

        # Запросы статуса — тоже мгновенные, без очереди.
        if intent in ("report_pos", "report_status"):
            cmd = self._build_cmd(intent, raw_text)
            # Прогоняем через _dispatch напрямую — он сформирует текст отчета.
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

        # При активной миссии команды управления НЕ двигают робота, а
        # только собирают программу (Python-код). Робот двигается только
        # через ▶ Запустить код, который запускает проверку миссии.
        # _NO_RECORD команды (mode_*, path_*) — UI-команды, выполняются
        # как обычно.
        if self._mission is not None and intent not in _NO_RECORD:
            self._program.append(cmd)
            self._save_program()
            await self.push_program()
            await self.push_message(
                f"➕ {cmd.label} — добавлено в программу. "
                f"Для проверки кода нажми ▶ Запустить.", "info")
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
          ≥ 60% (зеленый):           1.00 — без потерь
          30–60% (желто-оливковый):  0.70 → 1.00 (линейно)
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

            # Защитный стоп в режиме «осторожно»: если ЛЮБОЙ угол корпуса
            # реально оказался ВНУТРИ опасной зоны (не в раздутой —
            # планировщик уже ее обходит) — мгновенная остановка. Это
            # страховка от ошибок планирования на узких проходах.
            if s.cautious and not hit_wall and self.world.danger_zones:
                hit_zone = None
                for cx, cy in self._robot_corners():
                    for z in self.world.danger_zones:
                        if (cx - z.x) ** 2 + (cy - z.y) ** 2 < z.radius ** 2:
                            hit_zone = z
                            break
                    if hit_zone:
                        break
                if hit_zone:
                    s.speed     = 0
                    s.dist_left = 0
                    try: await self.robot.move(0)
                    except Exception: pass
                    await self.push_message(
                        f"⚠ Касание зоны «{hit_zone.label}» — стоп.",
                        "warning")

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
    """Возвращает сессию пользователя; создает и стартует, если нет."""
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
