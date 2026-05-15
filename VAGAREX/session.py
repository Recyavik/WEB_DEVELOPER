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
# 3-й параметр robot.set_rgb — `delay`, задержка между каналами в секундах
# (API: default 1.2 = плавный fade). Для индикации режима используем 0.0 —
# мгновенный отклик. Раньше тут было LIGHT_COUNT=1 — это была ошибка,
# которая на железе вызывала медленный fade вместо немедленного зажигания.
LIGHT_DELAY_SEC = 0.0
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
    # Алгоритм обхода зон в режиме «осторожно»:
    #   pure_pursuit / stanley / linear / manual (см. models.UserSettings)
    cautious_follow_algo: str  = "pure_pursuit"
    cautious_slow_curves: bool = True
    # Стратегия разворота в тесном пространстве (см. _run_face_cardinal):
    #   "backoff"    — отъехать назад на «нужно forward_need», сделать
    #                  один большой 3-дуговой K-turn, компенсировать отъезд.
    #                  Быстрее, но требует много места (≥80-100 см впереди).
    #   "multi_step" — разбить разворот на N маленьких K-turn'ов (каждый
    #                  возвращается в свою стартовую точку → ноль дрейфа).
    #                  N подбирается автоматически по доступному месту.
    #                  Медленнее (×N), но влезает в 8-20 см впереди.
    #   "manual"     — НЕ отъезжать и НЕ разворачиваться, если впереди мало
    #                  места. Просто остановиться и сообщить пользователю.
    #                  Пользователь сам разруливает (отъезжает, объезжает).
    wall_turn_strategy:  str  = "backoff"          # "backoff" | "multi_step" | "manual"
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
            wall_turn_strategy   = (row.wall_turn_strategy or "backoff"),
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

        # Последний текст пользовательской Python-программы (textarea).
        # Обновляется при run_python_code и при явной sync_code WS-команде.
        # Используется в _run_reset: START_X/Y/HEADING_DEG из КОДА побеждают
        # настройки (код — источник истины, настройки — только дефолты).
        self._last_python_code: Optional[str] = None

        # Событие для «pause/resume» в режиме обхода «manual» и fallback.
        # При manual-handoff exec-цепочка делает `await event.wait()`, а
        # дальше управление переходит к пользователю (голос/кнопки/руль).
        # ▶ Продолжить (intent="resume") дёргает `event.set()` → exec
        # просыпается и продолжает со следующей инструкции.
        self._resume_event: asyncio.Event = asyncio.Event()

        # Активный future от RobotProxy._run — нужен для ■ СТОП, чтобы
        # отменить текущую корутину команды (forward/goto/arc/...) и
        # прервать её мгновенно, не дожидаясь s.speed=0.
        self._python_active_future = None

        # Образовательная пауза (⏸ Пауза). Event начально SET = программа
        # «не на паузе». Когда пользователь жмёт ⏸, event clear'ится,
        # `_wait_movement` блокируется на нём; ▶ Продолжить → set'ит обратно.
        self._program_pause_event: asyncio.Event = asyncio.Event()
        self._program_pause_event.set()
        # Сохранённое состояние при паузе — чтобы продолжить с того же
        # speed/steer/dist_left, что были до паузы.
        self._paused_state: Optional[dict] = None

        # Запоминаем набор s.obstacles в момент входа в ⛯ Режим зон —
        # чтобы при выходе вернуть прежние препятствия (а не обнулять).
        # None пока в Режиме зон не входили.
        self._obstacles_before_zone = None

        # Эффективная стартовая точка — то, куда телепортируется робот
        # при reset, и где рисуется зелёный маркер на canvas. None пока
        # не было ни одного reset (новая сессия) — используется fallback
        # к cfg.start_x_cm. После reset с кодом, в котором START_X=50,
        # становится (50, …) и маркер «переезжает» с настроек на код.
        self._effective_start_x: Optional[float] = None
        self._effective_start_y: Optional[float] = None
        self._effective_start_heading: Optional[float] = None

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
          3. Опасные зоны миссии кладутся СРАЗУ в world + DB (это
             обстановка, а не команды программы). При ↺ Поле / ▶ Run
             в `_run_reset` они переставляются обратно из self._mission.
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
            # Опасные зоны миссии — это ОБСТАНОВКА: ставятся СРАЗУ в
            # world + DB (а не в код программы). Программа их не создаёт
            # и не может создать (robot.mark_danger удалён из API).
            # Удалять — можно (`robot.remove_zone*` когда робот внутри).
            for (zx, zy, zr) in self._mission.danger_zones:
                no = self._next_zone_display_no("danger")
                zone = self.world.add_danger_zone(
                    float(zx), float(zy), radius=float(zr),
                    label="Зона опасности", kind="danger",
                    display_no=no)
                dz = DangerZone(user_id=self.user_id, label=zone.label,
                                x=zone.x, y=zone.y, radius=zone.radius,
                                kind="danger", display_no=no)
                db.add(dz)
                db.flush()
                zone.db_id = dz.id
            db.commit()
            await self.push_world()
        finally:
            db.close()
        # На миссиях с опасными зонами (уровень ≥ 2) форсим режим
        # «осторожно». Проверка миссии должна выполняться в нём, и
        # переключиться обратно нельзя до stop/finalize (см. handler
        # mode_inspector).
        if self._mission.danger_zones and "danger" not in self.robot_state.obstacles:
            obs = set(self.robot_state.obstacles)
            obs.add("danger")
            self._apply_obstacles(obs)
            await self.push_message(
                "⚠ Миссия с опасными зонами — опасные зоны включены в "
                "препятствия. До завершения миссии их нельзя снять.",
                "info")
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

    async def run_check_python(self, code: str) -> bool:
        """Прогон Python-кода (`robot.X(...)`) для активной миссии.
        Параллель run_check для нового API. Сбрасывает счётчики, запускает
        пользовательский код через robot_api.run_user_python, замеряет время
        и накапливает в last_algo_duration_sec."""
        import time as _time
        from robot_api import run_user_python
        if self._mission is None:
            return False
        m = self._mission
        m.waypoints_visited.clear()
        m.actions_done.clear()
        m.coefficient        = 1.0
        m.deviations         = 0
        m.last_in_margin     = True
        m.last_robot_pos     = None
        algo_start_t = _time.monotonic()
        out = await run_user_python(self, code)
        run_dur = _time.monotonic() - algo_start_t
        if self._mission is not None:
            cumulative = (self._mission.last_algo_duration_sec or 0.0) + run_dur
            self._mission.last_algo_duration_sec = round(cumulative, 2)
            wp_done = len(self._mission.waypoints_visited)
            wp_total = len(self._mission.waypoints)
            prec = int(round(self._mission.coefficient * 100))
            await self.push_message(
                f"{out}\nТочки {wp_done}/{wp_total}, точность {prec}%, "
                f"всего времени алгоритма {cumulative:.1f} с. "
                f"Жми 🏁 Проверка задания для финала.",
                "info")
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
        No-op если миссии нет.

        Для инспектор-режима (L1/L2): любое action прощает наезд на
        ту опасную зону, внутри которой сейчас находится робот. Без
        этого игрок, который остановился внутри опасной зоны чтобы
        её убрать (или поставить attention рядом), получал бы −5% при
        выезде — что неверно, действие как раз и нейтрализует наезд."""
        if self._mission is None:
            return
        if self._mission.is_inspector:
            s = self.robot_state
            self._mission.forgive_current_zone_hits(s.x, s.y)
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
        # Финальная разбивка звёзд:
        #   • факт (за каждую посещённую точку и выполненное действие) —
        #     ВСЕГДА засчитывается; обучающийся честно довёл робота до
        #     точки, эту звезду нельзя отнять только потому, что миссию
        #     не закрыли целиком.
        #   • точность траектории — тоже всегда (бонус начисляется по
        #     пройденному пути и не зависит от полноты завершения).
        #   • скорость прохождения — только при success: премия за
        #     полностью законченную миссию в срок.
        fact_stars  = m.fact_stars()
        track_stars = m.track_bonus_stars()
        time_stars  = m.time_bonus_stars(duration_sec) if success else 0
        stars = fact_stars + track_stars + time_stars
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
                                           kind=(z.kind or "danger"),
                                           display_no=int(getattr(z, "display_no", 0) or 0))
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
        # Сначала ОБЪЯВЛЯЕМ статус миссии, потом высылаем код программы.
        # Иначе клиентский case 'program' видит `window._currentMission`
        # ещё пустым, считает «свободный режим» и подменяет серверный
        # код черновиком из localStorage — пользователь видит код от
        # прошлой сессии вместо актуальной программы миссии.
        if self._mission is not None:
            await ws.send_json({
                "type":    "mission_active",
                "mission": self._mission.to_client_dict(),
            })
        else:
            await ws.send_json({"type": "mission_inactive"})
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
        # Маркер «домой»: эффективная стартовая точка (была обновлена
        # последним _run_reset из START_X/Y/HEADING_DEG кода) — побеждает
        # настройки. До первого reset / при пустом коде — fallback к cfg.
        d["start_x_cm"]        = (self._effective_start_x
                                  if self._effective_start_x is not None
                                  else self.cfg.start_x_cm)
        d["start_y_cm"]        = (self._effective_start_y
                                  if self._effective_start_y is not None
                                  else self.cfg.start_y_cm)
        d["start_heading_deg"] = (self._effective_start_heading
                                  if self._effective_start_heading is not None
                                  else self.cfg.start_heading_deg)
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

    async def push_code_append(self, code: str, description: str = None):
        """Толкнуть строку Python-кода в textarea клиента БЕЗ записи в
        журнал. Используется в начале _dispatch — код кнопки появляется
        В КОДЕ сразу, до того как робот начал манёвр (визуально это
        важно: ребёнок видит «команда записана», потом смотрит как она
        исполняется). Результат исполнения уходит обычным push_message
        в конце _dispatch — там код уже не дублируется."""
        payload = {"type": "code_append", "code": code}
        if description is not None:
            payload["description"] = description
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

    async def _do_program_pause(self):
        """Образовательная пауза — мотор глушится, exec замирает на
        текущей команде. ▶ Продолжить — возобновляет с тем же speed и
        остатком дистанции. В отличие от ■ СТОП — программа НЕ убита."""
        s = self.robot_state
        if s.program_paused:
            return
        # Защита от «мёртвой паузы»: если exec не запущен — пауза
        # не имеет смысла (нечего приостанавливать), сообщим и выйдем.
        flag = getattr(self, "_python_cancel_flag", None)
        if flag is None:
            await self.push_message(
                "⏸ Программа сейчас не выполняется — нечего паузить.",
                "warning")
            return
        # Запоминаем состояние для восстановления.
        self._paused_state = {
            "speed":     float(s.speed),
            "steer":     float(s.steer),
            "dist_left": float(s.dist_left),
            "laser_stop": bool(s.laser_stop),
        }
        # Глушим мотор.
        await self.robot.move(0)
        s.speed = 0
        s.program_paused = True
        # Блокируем _wait_movement (ждёт _program_pause_event).
        self._program_pause_event.clear()
        await self.push_message(
            "⏸ Пауза. ▶ Продолжить — возобновить движение.", "info")
        await self.push_state()

    async def _do_program_resume(self):
        """Возобновление образовательной паузы. Восстанавливает мотор и
        отпускает _wait_movement через _program_pause_event."""
        s = self.robot_state
        if not s.program_paused:
            return
        s.program_paused = False
        st = self._paused_state or {}
        spd = int(st.get("speed", 0))
        if spd != 0:
            await self.robot.set_angle(int(st.get("steer", 0.0)))
            await self.robot.move(spd)
            s.speed = float(spd)
            s.steer = float(st.get("steer", 0.0))
            s.dist_left = float(st.get("dist_left", 0.0))
            s.laser_stop = bool(st.get("laser_stop", False))
        self._paused_state = None
        # Разбудить _wait_movement, который ждёт на event.
        self._program_pause_event.set()
        await self.push_message("▶ Программа возобновлена.", "success")
        await self.push_state()

    async def _do_stop(self):
        """Глобальный СТОП. Останавливает робота и всё, что было запущено:
          1) Очередь intent-команд (_pending) → очистить;
          2) Asyncio-задача исполнителя очереди (_exec_task) → отменить;
          3) Python-программа пользователя (другой поток) → флаг отмены,
             следующий robot.X() кинет RobotInterrupted;
          4) Программа в pause (manual handoff, await _resume_event) →
             будим event'ом, плюс взводим cancel_flag — в _pause_for_manual_handoff
             ветка проверки флага сразу же бросит RobotInterrupted без
             «программа возобновлена»;
          5) Драйвер → move(0), центрировать руль;
          6) robot_state → speed/dist_left/thinking/awaiting_user = idle.
        Должен работать для любого режима обхода (pp / stanley / linear / manual).
        """
        self._pending.clear()
        if self._exec_task and not self._exec_task.done():
            self._exec_task.cancel()
        # Поднимаем флаг отмены ДО event.set() — _pause_for_manual_handoff
        # проверит флаг после пробуждения и не пойдёт «возобновлять».
        flag = getattr(self, "_python_cancel_flag", None)
        if flag is not None:
            flag.set()
        # Прерываем ТЕКУЩУЮ команду пользовательской программы
        # (robot.forward/goto/arc/...) — отмена future в loop мгновенно
        # бросает CancelledError в корутине, _run() ловит её как
        # RobotInterrupted и выходит. Без этого forward(1000) ехал бы
        # до конца, даже если ■ СТОП нажат на середине.
        fut = self._python_active_future
        if fut is not None and not fut.done():
            try:
                fut.cancel()
            except Exception:
                pass
        # Разбудить exec-поток, заблокированный в _pause_for_manual_handoff.
        if self._resume_event is not None:
            self._resume_event.set()
        # Разбудить exec-поток, который ждёт ▶ Продолжить из ⏸ Паузы.
        # Без этого паузнутая программа НЕ умирает по ■ СТОП — exec вечно
        # висит на `await self._program_pause_event.wait()`.
        if self._program_pause_event is not None:
            self._program_pause_event.set()
        s = self.robot_state
        s.speed     = 0
        s.dist_left = 0
        s.laser_stop = False
        s.thinking  = "idle"
        s.awaiting_user = False
        s.program_paused = False
        self._paused_state = None
        await self.robot.move(0)
        await self.robot.set_servo_center()

    async def _wait_movement(self, timeout: float = 30.0):
        steps = int(timeout / 0.05)
        # Локальная ссылка на флаг отмены (■ СТОП). Если поднят —
        # обрываем ожидание ЧЕРЕЗ CancelledError (не через тихий return),
        # чтобы _run_forward/_arc_at_steer перешли в свой except-блок,
        # а _run() поднял RobotInterrupted и exec завершил программу.
        # Проверка флага идёт РАНЬШЕ speed==0: _do_stop ставит и флаг,
        # и speed=0 — если проверять speed первым, мы тихо вернёмся,
        # и пользователь не увидит сообщения «■ СТОП — прервано».
        cancel_flag = getattr(self, "_python_cancel_flag", None)
        for _ in range(steps):
            await asyncio.sleep(0.05)
            # ⏸ Образовательная пауза — блокируемся на event, пока не
            # нажмут ▶ Продолжить. Мотор уже глушён в _do_program_pause,
            # speed/dist_left восстановятся в _do_program_resume.
            if self.robot_state.program_paused:
                await self._program_pause_event.wait()
                # Сразу после resume — проверка стопа (вдруг во время
                # паузы пользователь решил всё-таки прервать программу).
                if cancel_flag is not None and cancel_flag.is_set():
                    self.robot_state.speed     = 0
                    self.robot_state.dist_left = 0
                    try:    await self.robot.move(0)
                    except Exception: pass
                    raise asyncio.CancelledError("■ СТОП")
                continue
            if cancel_flag is not None and cancel_flag.is_set():
                self.robot_state.speed     = 0
                self.robot_state.dist_left = 0
                try:    await self.robot.move(0)
                except Exception: pass
                raise asyncio.CancelledError("■ СТОП")
            if self.robot_state.speed == 0:
                return
        self.robot_state.speed     = 0
        self.robot_state.dist_left = 0
        await self.robot.move(0)

    def _clip_dist_cautious(self, dist_cm: float,
                            heading: Optional[float] = None) -> float:
        """Обрезает дистанцию движения так, чтобы не въехать в зону-
        препятствие. По умолчанию проверяет вдоль курса носа; для движения
        ЗАДОМ вызывающий передаёт heading = (s.heading + 180) % 360."""
        s = self.robot_state
        if not s.cautious or not self.world.danger_zones:
            return dist_cm
        h    = s.heading if heading is None else heading
        hrad = math.radians(h)
        dx   = math.sin(hrad)
        dy   = math.cos(hrad)
        MARGIN   = 20.0
        min_dist = dist_cm
        for zone in self.world.danger_zones:
            if not self._zone_is_obstacle(zone):
                continue
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
        STEER         = int(self.cfg.turn_angle)
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
        # K-turn крутится на месте — защитный стоп у зон его не обрывает
        # (см. update_physics). Флаг снимается в finally.
        s.turning_in_place = True
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
            s.turning_in_place = False
            await self.robot.stop()
            await self.robot.set_servo_center()

        # Snap ТОЛЬКО курса
        s.heading = float(target_heading)
        await self.push_state()

    # ── Примитивы движения ───────────────────────────────────────────────────

    async def _run_forward(self, dist_cm: Optional[float], spd: int):
        s = self.robot_state
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
        # cautious уже упёрся в зону на margin — не двигаем мотор.
        if s.cautious and zone_dist < 1.0:
            await self.robot.move(0)
            s.speed = 0
            s.dist_left = 0
            await self.push_message(
                "⚠ Уже у зоны — ехать вперёд нельзя.", "warning")
            return
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
        # Едем ЗАДОМ — зоны проверяем в направлении кормы (heading+180),
        # а не носа: _clip_dist_cautious по умолчанию смотрит вперёд.
        back_heading = (s.heading + 180.0) % 360.0
        zone_dist = (self._clip_dist_cautious(9999.0, back_heading)
                     if s.cautious else 9999.0)
        # Уже упёрлись кормой в зону — не двигаем мотор.
        if s.cautious and zone_dist < 1.0:
            await self.robot.move(0)
            s.speed = 0
            s.dist_left = 0
            await self.push_message(
                "⚠ Уже у зоны сзади — ехать задом нельзя.", "warning")
            return
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
        STEER       = int(self.cfg.turn_angle)
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
                              backward: bool = False, laser_check: bool = True):
        """Едет дугой при заданном угле руля до изменения курса на sweep_deg.
        backward=True — едет ЗАДОМ (steer тот же, скорость противоположная).
        laser_check=False — отключает дальномер на время дуги (для K-turn'а,
        где клиаренс УЖЕ проверен в `_ensure_kturn_clearance` и laser_stop
        обрывал бы арки, накапливая позиционный дрейф)."""
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
        s.laser_stop = bool(self.cfg.laser_enabled) and laser_check
        await self.robot.set_angle(int(steer_deg))
        await self.robot.move(drive_spd)
        try:
            await self._wait_movement(timeout=60.0)
        except asyncio.CancelledError:
            raise
        finally:
            s.laser_stop = False

    async def _run_arc(self, angle_deg: float, direction: int = -1):
        """Дуга на `angle_deg` градусов курса при максимальном угле руля
        (cfg.turn_angle). По умолчанию ПРОТИВ часовой (CCW, мат. +).
        direction=+1 — по часовой. `angle_deg` — модуль, направление
        управляется отдельным параметром. `_run_arc(360, dir)` = полный круг."""
        s = self.robot_state
        spd = self.cfg.move_speed
        steer = direction * self.cfg.turn_angle
        sweep = abs(float(angle_deg))
        if sweep <= 0:
            return
        try:
            await self._arc_at_steer(steer, sweep, spd)
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
        """Восьмёрка через ДВЕ дуги по 360°: первый круг в `direction`,
        второй — в противоположную. По умолчанию первый CCW, второй CW."""
        s = self.robot_state
        spd = self.cfg.move_speed
        steer = self.cfg.turn_angle
        try:
            await self.push_message("Восьмерка: дуга 1/2 (360°)…", "info")
            await self._arc_at_steer(direction * steer, 360.0, spd)
            await asyncio.sleep(0.2)
            await self.push_message("Восьмерка: дуга 2/2 (360°)…", "info")
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
        """Точка входа goto — всегда прямой заход в точку.

        Планировщик обхода зон (старый режим «осторожно») из goto убран:
        в режиме «Опасно» робот едет прямо и автоматически тормозит перед
        зоной/стеной (см. update_physics), а команда переписывается на
        фактически достигнутую. Построение обходного маршрута — задача
        отдельного режима «Автопилот»."""
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

        # Режим manual — НЕ ищем обход вообще. Передаём управление
        # пользователю сразу при первом goto, путь которого пересекает зону.
        algo_choice = (cfg.cautious_follow_algo or "pure_pursuit").lower()
        if algo_choice == "manual":
            await self._pause_for_manual_handoff(
                target_x, target_y,
                "Путь к цели пересекает зону, авто-обход отключён.")
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
            # A* не нашёл прохода — fallback в manual: пользователь сам
            # объедет/удалит зону, потом ▶ Продолжить.
            await self._pause_for_manual_handoff(
                target_x, target_y,
                "Авто-обход не нашёл прохода между зонами.")
            return True

        # Выбор алгоритма обхода. Linear — идём прямыми по углам A*,
        # повороты на месте (K-turn) в каждой вершине. Остальные — гладкая
        # дуга через Чайкин + pure-pursuit/stanley follower.
        algo = (cfg.cautious_follow_algo or "pure_pursuit").lower()
        if algo == "linear":
            # Подсветка пути на canvas — сами A*-углы (полилиния).
            self.world.add_auto_segment(waypoints)
            s.thinking = "idle"
            await self.push_world()
            await self.push_state()
            ok = await self._follow_linear(waypoints)
        else:
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
            # Follower обрвалcя (касание зоны/стены/обрыв пути). Fallback
            # в manual: пользователь дорулит, потом ▶ Продолжить.
            await self._pause_for_manual_handoff(
                target_x, target_y,
                f"Автоматическое следование сорвалось ({algo}). "
                f"Осталось {remaining:.0f} см.")
            return True
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

    async def _pause_for_manual_handoff(self,
                                          target_x: float, target_y: float,
                                          reason: str) -> None:
        """Останавливает робота и блокирует exec-цепочку программы до
        ▶ Продолжить от пользователя. Используется когда:
          • cautious + algo="manual" — на любой пересекающий зону goto
          • cautious + algo=(pp|stanley|linear) FAIL — fallback из «не нашёл/
            не доехал» в ручной режим (раз автоматика не справилась).
        Поднимает `s.awaiting_user` и `thinking=awaiting_user` — UI показывает
        кнопку ▶ Продолжить. Голос/кнопки/руль работают как обычно: они
        идут через handle_command, а не через exec, поэтому не блокированы.
        После возобновления exec продолжает СЛЕДУЮЩУЮ инструкцию (не goto)."""
        s = self.robot_state
        # Гарантированно тормозим и центрируем руль.
        await self.robot.stop()
        await self.robot.set_servo_center()
        s.speed = 0
        s.dist_left = 0
        s.steer = 0.0
        s.thinking = "awaiting_user"
        s.awaiting_user = True
        self._resume_event.clear()
        await self.push_message(
            f"⏸ {reason} Цель ({target_x:.0f}, {target_y:.0f}). "
            f"Рули вручную и жми ▶ Продолжить.",
            "warning")
        await self.push_state()
        # Ждём от пользователя ▶ Продолжить. Это блокирующая точка для
        # exec-программы — handle_command (voice/buttons) работает в
        # параллельной задаче и НЕ затронут.
        await self._resume_event.wait()
        s.thinking = "idle"
        s.awaiting_user = False
        await self.push_state()
        # Глобальный СТОП тоже дёргает _resume_event.set(), но при этом
        # взводит _python_cancel_flag. Здесь молча выходим — следующий
        # вызов robot.X() в коде пользователя сразу кинет RobotInterrupted,
        # сообщение «программа возобновлена» в этом случае было бы ложью.
        flag = getattr(self, "_python_cancel_flag", None)
        if flag is not None and flag.is_set():
            return
        await self.push_message("▶ Программа возобновлена.", "info")

    async def _follow_curve(self, points: list[tuple[float, float]]) -> bool:
        """Диспетчер следования за кривой. Алгоритм выбирается из настроек."""
        algo = self.cfg.cautious_follow_algo or "pure_pursuit"
        if algo == "stanley":
            return await self._follow_stanley(points)
        return await self._follow_pure_pursuit(points)

    async def _follow_linear(self,
                              waypoints: list[tuple[float, float]]) -> bool:
        """Линейный обход: едем по A*-углам прямыми отрезками, в каждой
        вершине ломаной выполняем K-turn (поворот на месте) к следующей
        точке. Без сглаживания — точно по полилинии.

        Плюсы: предсказуемо, попадает в каждую вершину; полезно для
        учебного режима и отладки (обучающийся видит как робот
        дискретно следует за планом).
        Минусы: ×N дольше из-за разворотов на месте.

        Возвращает True если дошли до последнего сегмента без обрыва."""
        s = self.robot_state
        spd = self.cfg.move_speed
        if len(waypoints) < 2:
            return True
        for i in range(1, len(waypoints)):
            nx, ny = waypoints[i]
            dx = nx - s.x
            dy = ny - s.y
            dist = math.hypot(dx, dy)
            # Очень короткий сегмент (< 2 см) — пропускаем, мы и так в точке.
            if dist < 2.0:
                continue
            # Курс к следующей вершине. atan2(dx, dy) т.к. heading=0 на север (Y+).
            target_heading = math.degrees(math.atan2(dx, dy)) % 360.0
            diff = (target_heading - s.heading + 540.0) % 360.0 - 180.0
            # K-turn только если курс реально не совпадает — экономим время.
            if abs(diff) > 3.0:
                try:
                    await self._k_turn_to_heading(target_heading)
                except asyncio.CancelledError:
                    return False
            try:
                await self._run_forward(dist, spd)
            except asyncio.CancelledError:
                return False
            # Если дальномер обрезал движение перед стеной / физика остановила
            # раньше — не пытаемся продолжать ломаную, путь блокирован.
            actual_to_target = math.hypot(s.x - nx, s.y - ny)
            if actual_to_target > max(15.0, 0.3 * dist):
                return False
        return True

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

            BEHIND_TOL = 180.0 - STRAIGHT_TOL    # 150°

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

            # Shortcut 2: цель почти ЗА СПИНОЙ → едем задом, без K-turn'а.
            if abs(bearing) > BEHIND_TOL:
                if attempt == 1:
                    await self.push_message(
                        f"В точку ({target_x:.0f}, {target_y:.0f}): "
                        f"цель за спиной ({bearing:+.0f}°), "
                        f"еду задом {distance:.0f} см.", "info")
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

            # Фаза 1: повернуться лицом к цели — через зоно-зависимый
            # авто-роутер (_run_face_cardinal): хватает места → обычный
            # K-turn; тесно/зона на пути разворота → мелкий многошаговый
            # разворот с отъездом; совсем негде → стоп. Без роутера K-turn
            # пропахал бы сквозь зону (turning_in_place гасит защитный стоп).
            await self._run_face_cardinal(target_heading, "цели")

            # Доворот: multi-step разворот в тесноте мог включить отъезд
            # назад и сместить робота — из-за этого курс на цель «уплыл»
            # (target_heading считался ДО отъезда). Пересчитываем курс из
            # НОВОЙ позиции и доворачиваем на остаток обычным 3-дуговым
            # K-turn — он геометрически возвращается в свою точку, не
            # смещает робота и отъезда не требует.
            # Большой остаток (> 30°) не доводим: значит основной разворот
            # не состоялся (некуда) — форсировать сквозь зону нельзя.
            rdx = float(target_x) - s.x
            rdy = float(target_y) - s.y
            if math.hypot(rdx, rdy) > TOL_CM:
                refined  = math.degrees(math.atan2(rdx, rdy)) % 360
                residual = (refined - s.heading + 540.0) % 360.0 - 180.0
                if 2.0 < abs(residual) <= 30.0:
                    await self.push_message(
                        f"↻ Курс на цель уточнён на {residual:+.0f}° "
                        f"(сместился при отъезде на разворот).", "info")
                    await self._k_turn_to_heading(refined)

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
        STEER     = int(self.cfg.turn_angle)
        direction = +1 if diff > 0 else -1
        # Сохраняем стартовую позицию — после K-turn вернёмся сюда.
        start_x, start_y = s.x, s.y
        # MAX_ITER=0 ОТКЛЮЧАЕТ in-K-turn коррекции (Phase 2/3).
        # Причина: коррекция через _drive_to_point + _k_turn_arcs создаёт
        # дополнительные арки в траектории, которые визуально смешиваются
        # с основным 3-дуговым K-turn'ом и делают картинку «грязной».
        # Reeds-Shepp геометрически возвращает в старт, дрейф симулятора
        # (~5-15 см на 180°) принимаем как факт. Финальный offset уйдёт
        # в следующую команду движения (она работает к абсолютным
        # координатам — компенсирует автоматически).
        TOL_POS = 5.0
        TOL_HDG = 3.0
        MAX_ITER = 0

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
            # laser_check=False: клиаренс УЖЕ проверен в _ensure_kturn_clearance,
            # включённый дальномер обрывал бы арки у стены и накапливал
            # позиционный дрейф (Y +50см, X +13см в 180° тесте).
            if alpha_deg > 0.5:
                await self._arc_at_steer(direction * STEER, alpha_deg, spd,
                                          laser_check=False)
            # Дуга 2: назад, ОБРАТНЫЙ руль (но курс продолжает крутиться
            # в ту же сторону, что и в дуге 1, благодаря смене знака v и steer)
            await self._arc_at_steer(-direction * STEER, beta_deg, spd,
                                      backward=True, laser_check=False)
            # Дуга 3: вперед, тот же руль, что дуга 1
            if alpha_deg > 0.5:
                await self._arc_at_steer(direction * STEER, alpha_deg, spd,
                                          laser_check=False)

            # ── Фазы 2+3 итеративно: возврат в точку + коррекция курса ──
            # Используем большие per_arc для Phase 3, чтобы коррекция шла
            # 2-4 sub-арками вместо 8-10. Визуально чище.
            # Сообщения о коррекциях скрываем: достаточно итогового статуса
            # в конце, если коррекций было больше одной.
            n_corrections = 0
            for it in range(MAX_ITER):
                pos_err = math.hypot(start_x - s.x, start_y - s.y)
                hdg_err = (target_deg - s.heading + 540.0) % 360.0 - 180.0
                if pos_err < TOL_POS and abs(hdg_err) < TOL_HDG:
                    break
                if pos_err >= TOL_POS:
                    await self._drive_to_point(start_x, start_y, TOL_POS, spd)
                    n_corrections += 1
                hdg_err = (target_deg - s.heading + 540.0) % 360.0 - 180.0
                if abs(hdg_err) >= TOL_HDG:
                    # per_arc=15° даёт всего 1 пару sub-арок для 30°
                    # коррекции (вместо 3-4 пар при per_arc=8°).
                    per_arc = 15.0 if it == 0 else 10.0
                    await self._k_turn_arcs(hdg_err, STEER, spd, per_arc_deg=per_arc)
                    n_corrections += 1
            if n_corrections > 0:
                final_pos_err = math.hypot(start_x - s.x, start_y - s.y)
                await self.push_message(
                    f"K-turn: уточнение позиции/курса ({n_corrections} коррекций, "
                    f"итоговый дрейф {final_pos_err:.0f} см).", "info")
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

    def _free_forward_cm(self, heading: float) -> float:
        """Сколько см робот может проехать в направлении `heading` до
        защитного буфера ближайшего препятствия.

        Препятствия:
          • стены — всегда (и в «Инспекторе», и в «Опасно»);
          • опасные зоны и зоны внимания — ТОЛЬКО в режиме «Опасно»
            (s.cautious). В «Инспекторе» зона не препятствие — робот
            боится только стен.

        Буфер = «Толщина стены» из настроек (тот же, что у защитного
        стопа физики). Используется при планировании разворота: помещается
        ли K-turn / multi-step здесь, или надо отъезжать."""
        s  = self.robot_state
        wt = self.cfg.wall_thickness_cm
        free = self._wall_dist_cm(heading) - wt
        if s.cautious and self.world.danger_zones:
            rad = math.radians(heading)
            dx, dy = math.sin(rad), math.cos(rad)
            for z in self.world.danger_zones:
                if not self._zone_is_obstacle(z):
                    continue
                lx, ly = z.x - s.x, z.y - s.y
                tca = lx * dx + ly * dy
                if tca < 0:
                    continue                       # зона позади направления
                d2 = lx * lx + ly * ly - tca * tca
                if d2 > z.radius * z.radius:
                    continue                       # луч проходит мимо зоны
                thc = math.sqrt(max(0.0, z.radius * z.radius - d2))
                zone_free = max(0.0, (tca - thc) - wt)
                free = min(free, zone_free)
        return free

    def _kturn_forward_clearance_needed(self, delta_deg: float) -> float:
        """Сколько см свободного пространства нужно ВПЕРЁД для K-turn на delta_deg.
        Берётся из геометрии Reeds-Shepp: после первой forward-дуги α робот
        смещается на R·sin(α) вперёд от старта (это максимум forward-сдвига
        за всю последовательность 3 дуг)."""
        delta = abs(delta_deg)
        if delta < 1.0:
            return 0.0
        # Минимальный радиус поворота при STEER = cfg.turn_angle
        # (то же, что использует _k_turn_to_heading / _k_turn_n).
        steer_ratio = float(self.cfg.turn_angle) / 45.0
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

    async def _ensure_kturn_clearance(self, target_deg: float) -> float:
        """Проверяет, помещается ли K-turn на target_deg в свободном пространстве.
        Если ВПЕРЁДНОЙ свободы недостаточно, отъезжает назад на нужную величину
        (в пределах того, сколько места есть СЗАДИ).

        Возвращает фактически отъеханное расстояние в см (0.0 если отъезда
        не было). Курс не меняет. Используется как сигнал для последующей
        компенсации: после K-turn'а вызывающий код может прокатиться задом
        на ту же величину, чтобы развернуть робота «на месте» — точка до
        и после команды совпадает (для 180° K-turn точно, для других углов
        — приближённо)."""
        s = self.robot_state
        diff = (float(target_deg) - s.heading + 540.0) % 360.0 - 180.0
        if abs(diff) < 3.0:
            return 0.0
        forward_need  = self._kturn_forward_clearance_needed(abs(diff))
        # Минимум по габариту для крупных поворотов (≥ 90°): нужно где
        # развернуть кузов, не только wheel-center дугу.
        if abs(diff) >= 90.0:
            forward_need = max(forward_need, self.cfg.robot_length_cm)
        # Safety:
        # • +12.5 см — внутренний зазор laser_stop симулятора
        #   (wall_thickness/2 + 2·wall_thickness = 2.5 + 10).
        # • +полширины робота — углы корпуса при повороте выступают
        #   вперёд относительно носа на ≈ W/2.
        # • +15 см — запас на дискретный дрейф интегратора (per-tick
        #   ошибка форвард-Эйлера в дугах).
        forward_need += 12.5 + self.cfg.robot_width_cm / 2.0 + 15.0
        forward_have  = self._wall_dist_cm(s.heading) - self.cfg.wall_thickness_cm
        if forward_have >= forward_need:
            return 0.0          # места хватает, ничего не делаем
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
            return 0.0
        await self.push_message(
            f"⚠ Для разворота нужно {forward_need:.0f} см впереди, "
            f"а есть только {forward_have:.0f}. Отъезжаю назад на {backup:.0f} см.",
            "warning")
        await self._run_back(backup, self.cfg.move_speed)
        return float(backup)

    async def _compensate_kturn_backup(self, backup_cm: float,
                                         total_diff_deg: float = 180.0) -> None:
        """Компенсация отъезда: едем НАЗАД в направлении носа на backup_cm.

        Условие: total_diff_deg ≈ 180° (с допуском ±10°).
        Для других углов компенсация даёт боковой снос (направление
        заднего хода в новом курсе НЕ совпадает со старым курсом),
        поэтому пропускаем — лучше оставить робота в отъехавшей точке,
        чем сместить вбок на 30-50 см.

        Дальномер ОТКЛЮЧЁН: позиция «до отъезда» по определению безопасна
        (мы туда только что приехали), а K-turn мог чуть приблизить нас
        к стене — включённый laser_stop обрывал бы возврат на полпути."""
        if backup_cm < 1.0:
            return
        if abs(total_diff_deg) < 170.0:
            await self.push_message(
                f"🔁 Разворот завершён. Отъезд {backup_cm:.0f} см не компенсирую "
                f"(угол {total_diff_deg:.0f}° ≠ 180° — задний ход уведёт вбок).",
                "info")
            return
        s = self.robot_state
        await self.push_message(
            f"🔁 Разворот завершён. Компенсирую отъезд: задом {backup_cm:.0f} см.",
            "info")
        # Гарантируем steer=0 ДО старта заднего хода (после K-turn'а его
        # finally уже центрирует, но подстрахуемся).
        s.steer = 0.0
        await self.robot.set_servo_center()
        # Прямой задний ход С ОТКЛЮЧЁННЫМ laser_stop.
        await self.robot.set_angle(0)
        await self.robot.move(-self.cfg.move_speed)
        s.speed     = float(-self.cfg.move_speed)
        s.dist_left = float(backup_cm)
        s.laser_stop = False
        try:
            await self._wait_movement(timeout=max(5.0, backup_cm / 5.0))
        except asyncio.CancelledError:
            s.speed     = 0
            s.dist_left = 0
            await self.robot.move(0)
            raise

    async def _multi_step_kturn(self, target_deg: float) -> None:
        """Разворот N маленькими K-turn'ами (каждый возвращается в свою
        стартовую точку — нулевой дрейф позиции).

        Алгоритм:
          1. Δh = кратчайший подписанный угол поворота (−180..+180°).
          2. Подобрать минимальное N такое, что один шаг Δh/N помещается
             в текущий forward_have (с safety margin).
          3. Если даже N=MAX_N (12) не помещается — отъехать назад ровно
             столько, чтобы шаг при N=MAX_N влез. Запомнить backup_cm.
          4. Выполнить N последовательных _k_turn_to_heading-вызовов.
          5. Если был отъезд — компенсировать его задним ходом.

        Преимущество перед обычным K-turn'ом: один шаг 45° требует ~16 см
        вместо ~70 см для шага 180°. Применимо в тесных пространствах.
        """
        s = self.robot_state
        diff = (float(target_deg) - s.heading + 540.0) % 360.0 - 180.0
        if abs(diff) < 3.0:
            s.heading = float(target_deg) % 360.0
            await self.push_state()
            return

        # Safety на 1 шаг: laser margin + углы корпуса + Euler-drift.
        SAFETY = 12.5 + self.cfg.robot_width_cm / 2.0 + 5.0   # ≈23.5 см
        # R — минимальный радиус поворота при STEER = cfg.turn_angle.
        steer_ratio_cfg = float(self.cfg.turn_angle) / 45.0
        R = (self.cfg.wheel_circ_cm * 360.0) / (
            2.0 * math.pi * self.cfg.heading_per_rot * steer_ratio_cfg)

        forward_have  = self._free_forward_cm(s.heading)
        backward_have = self._free_forward_cm((s.heading + 180.0) % 360.0) - 5.0

        chosen_n, backup = self._pick_kturn_step_count(
            diff, forward_have, backward_have, R, SAFETY, max_n=4)

        if backup > 0.0:
            step_deg     = abs(diff) / chosen_n
            per_step_req = self._kturn_per_step_clearance(step_deg, R) + SAFETY
            await self.push_message(
                f"⚠ Multi-step: для {chosen_n} шагов нужно {per_step_req:.0f} см "
                f"впереди, есть {forward_have:.0f}. Отъезжаю на {backup:.0f} см.",
                "warning")
            await self._run_back(backup, self.cfg.move_speed)
        elif chosen_n == 4 and forward_have < self._kturn_per_step_clearance(
                abs(diff) / 4, R) + SAFETY - 1.0:
            await self.push_message(
                f"⚠ Места мало и впереди ({forward_have:.0f}), и сзади "
                f"({max(0,backward_have):.0f}). Разворачиваюсь как могу.",
                "warning")

        step_signed = diff / chosen_n
        await self.push_message(
            f"Разворот мульти-шагом: {chosen_n}× по {step_signed:+.1f}° "
            f"(Δh={diff:+.0f}°).", "info")

        for i in range(chosen_n):
            new_heading = (s.heading + step_signed) % 360.0
            await self._k_turn_to_heading(new_heading)

        await self._compensate_kturn_backup(backup, total_diff_deg=abs(diff))

    @staticmethod
    def _kturn_per_step_clearance(step_deg: float, R: float) -> float:
        """Сколько см свободного пространства ВПЕРЁД нужно для одного K-turn'а
        на step_deg градусов при радиусе R. Формула Reeds-Shepp:
            2α + β = step_deg,  sin(β/2) = sin(step_deg/2) / 2
        Forward extent = R·sin(α)."""
        d = abs(step_deg)
        if d < 1.0:
            return 0.0
        half = math.radians(d / 2.0)
        s_half = math.sin(half)
        if abs(s_half / 2.0) > 1.0:
            return float('inf')
        beta  = 2.0 * math.degrees(math.asin(s_half / 2.0))
        alpha = (d - beta) / 2.0
        return R * math.sin(math.radians(alpha))

    @staticmethod
    def _kturn_step_extents(step_deg: float, direction: int, R: float,
                             body_l: float = 0.0, body_w: float = 0.0
                             ) -> tuple[float, float, float, float]:
        """Численно прокручивает 3-дуговой K-turn step_deg в указанном
        direction (±1) и возвращает (forward, backward, right, left) —
        максимальное удаление корпуса робота (с учётом 4 углов
        прямоугольника L×W) от стартовой позиции в start-heading frame.

        forward = +y (по курсу),  backward = -y,
        right   = +x (heading+90), left = -x (heading-90).

        Все 4 значения ≥ 0. Применяется в `_pick_kturn_step_count` для
        проверки 4-сторонних клиренсов до стен."""
        if step_deg <= 0.5:
            # Очень маленький шаг — только габарит робота
            return (0.0, body_l, body_w / 2.0, body_w / 2.0)
        half = math.radians(step_deg / 2.0)
        s_half = math.sin(half)
        if abs(s_half / 2.0) > 1.0:
            return (R, R, R, R)
        beta_deg  = 2.0 * math.degrees(math.asin(s_half / 2.0))
        alpha_deg = (step_deg - beta_deg) / 2.0

        # 4 угла корпуса в локальной (start-heading) системе при позиции
        # (x, y) и курсе h_deg. Нос — это (x, y); корпус уходит назад на body_l.
        def body_corners(x, y, h_deg):
            hr  = math.radians(h_deg)
            sh, ch = math.sin(hr), math.cos(hr)
            # перпендикуляр (вправо от курса): (cos h, -sin h)
            half_w = body_w / 2.0
            nose_l = (x - ch * half_w, y + sh * half_w)
            nose_r = (x + ch * half_w, y - sh * half_w)
            rx = x - sh * body_l
            ry = y - ch * body_l
            rear_l = (rx - ch * half_w, ry + sh * half_w)
            rear_r = (rx + ch * half_w, ry - sh * half_w)
            return [nose_l, nose_r, rear_l, rear_r]

        # Дискретная прокрутка дуги ~1° на микрошаг, как в физике.
        def trace_arc(x, y, h, sweep_signed_deg, drive_sign):
            steps = max(1, int(math.ceil(abs(sweep_signed_deg))))
            d_head = sweep_signed_deg / steps
            ds     = R * math.radians(abs(d_head))
            corners_all = []
            for _ in range(steps):
                h += d_head
                hr = math.radians(h)
                x += drive_sign * math.sin(hr) * ds
                y += drive_sign * math.cos(hr) * ds
                corners_all.extend(body_corners(x, y, h))
            return x, y, h, corners_all

        all_corners = list(body_corners(0.0, 0.0, 0.0))
        # Дуга 1: вперёд, heading += direction · α
        x, y, h, c1 = trace_arc(0.0, 0.0, 0.0, direction * alpha_deg, +1)
        all_corners.extend(c1)
        # Дуга 2: назад, heading += direction · β
        x, y, h, c2 = trace_arc(x, y, h, direction * beta_deg, -1)
        all_corners.extend(c2)
        # Дуга 3: вперёд, heading += direction · α
        x, y, h, c3 = trace_arc(x, y, h, direction * alpha_deg, +1)
        all_corners.extend(c3)

        xs = [c[0] for c in all_corners]
        ys = [c[1] for c in all_corners]
        return (max(0.0, max(ys)),
                max(0.0, -min(ys)),
                max(0.0, max(xs)),
                max(0.0, -min(xs)))

    @staticmethod
    def _pick_kturn_step_count(diff: float, forward_have: float,
                                backward_have: float, R: float,
                                safety: float, max_n: int = 4,
                                max_step_deg: float = 45.0
                                ) -> tuple[int, float]:
        """Подбирает N для multi-step K-turn'а.

        Базовое правило (от пользователя):
          • Шаг разворота ≤ max_step_deg (45°). Чем меньше |Δh|, тем
            меньше шагов: 45° → 1, 90° → 2, 135° → 3, 180° → 4.
          • Не больше max_n=4 шагов.
        База:   N_min = ceil(|Δh| / max_step_deg), но не больше max_n.

        Если N_min шагов помещаются в (forward_have − safety) — берём их.
        Если нет — увеличиваем N (шаги мельче) до max_n. Если и max_n
        не лезет — мини-отъезд назад (с поправкой на backward_have).

        Возвращает (chosen_n, backup_cm)."""
        abs_diff = abs(diff)
        if abs_diff < 1e-6:
            return 1, 0.0
        safe_have = max(0.0, forward_have - safety)

        # Минимальное N по правилу «шаг ≤ 45°»
        n_min = max(1, math.ceil(abs_diff / max_step_deg))
        n_min = min(n_min, max_n)

        for n in range(n_min, max_n + 1):
            if UserSession._kturn_per_step_clearance(abs_diff / n, R) <= safe_have:
                return n, 0.0

        # Даже max_n не влезает — нужен отъезд.
        step_deg     = abs_diff / max_n
        per_step_req = UserSession._kturn_per_step_clearance(step_deg, R) + safety
        backup_need  = per_step_req - forward_have
        backup       = min(backup_need, max(0.0, backward_have))
        if backup < 5.0:
            return max_n, 0.0   # нет места и сзади — крутимся как есть
        return max_n, backup

    async def _run_face_cardinal(self, deg: float, label: str):
        """Разворот лицом к курсу `deg`. Стратегия выбирается АВТОМАТИЧЕСКИ
        по доступному месту впереди — без ручной настройки:
          1) места хватает → обычный 3-дуговой K-turn;
          2) тесно → мелкий многошаговый разворот (_multi_step_kturn сам
             подберёт число шагов и при нужде чуть отъедет назад);
          3) развернуться негде (нет места ни впереди, ни сзади) → стоп
             с сообщением, пользователь разруливает вручную."""
        await self.push_message(
            f"🧭 Развернуться лицом к {label} (курс {deg:.0f}°).", "info")
        s = self.robot_state
        diff = (float(deg) - s.heading + 540.0) % 360.0 - 180.0
        if abs(diff) < 3.0:
            s.heading = float(deg) % 360.0
            await self.push_state()
            return

        # Сколько места впереди нужно обычному 3-дуговому K-turn'у — та же
        # формула, что в _ensure_kturn_clearance (геометрия Reeds-Shepp +
        # запас на габарит, лазерный зазор и дрейф интегратора).
        forward_need = self._kturn_forward_clearance_needed(abs(diff))
        if abs(diff) >= 90.0:
            forward_need = max(forward_need, self.cfg.robot_length_cm)
        forward_need += 12.5 + self.cfg.robot_width_cm / 2.0 + 15.0
        forward_have = self._free_forward_cm(s.heading)

        # 1) Места достаточно — обычный K-turn без отъезда.
        if forward_have >= forward_need:
            await self._k_turn_to_heading(deg)
            return

        # Тесно. Проверяем, влезает ли хотя бы самый мелкий многошаговый
        # разворот (макс. дробление — 4 шага).
        SAFETY = 12.5 + self.cfg.robot_width_cm / 2.0 + 5.0
        steer_ratio = max(1e-6, float(self.cfg.turn_angle) / 45.0)
        R = (self.cfg.wheel_circ_cm * 360.0) / (
            2.0 * math.pi * self.cfg.heading_per_rot * steer_ratio)
        backward_have = self._free_forward_cm((s.heading + 180.0) % 360.0) - 5.0
        min_step_need = self._kturn_per_step_clearance(abs(diff) / 4.0, R) + SAFETY

        # 3) Совсем негде: мелкий шаг не влезает впереди и сзади нет места
        #    отъехать → стоп, не дёргаем робота вслепую.
        if forward_have < min_step_need and backward_have < 5.0:
            await self.push_message(
                f"⛔ Развернуться негде: для самого мелкого разворота нужно "
                f"{min_step_need:.0f} см впереди (есть {forward_have:.0f}), "
                f"сзади тоже только {max(0.0, backward_have):.0f} см. "
                f"Отъедь вручную и повтори команду.", "warning")
            return

        # 2) Тесно, но многошаговый разворот выполним (сам подберёт число
        #    шагов и при нужде чуть отъедет назад).
        await self.push_message(
            "↻ Тесно для обычного разворота — выполняю мелким многошаговым.",
            "info")
        await self._multi_step_kturn(deg)

    async def _kturn_clearance_ok(self, target_deg: float) -> bool:
        """Проверяет, достаточно ли места для K-turn'а БЕЗ отъезда.
        Используется в режиме wall_turn_strategy='manual': если места
        мало — отправляет предупреждение и возвращает False (вызывающий
        код останавливается, не разворачивается)."""
        s = self.robot_state
        diff = (float(target_deg) - s.heading + 540.0) % 360.0 - 180.0
        if abs(diff) < 3.0:
            return True
        forward_need  = self._kturn_forward_clearance_needed(abs(diff))
        if abs(diff) >= 90.0:
            forward_need = max(forward_need, self.cfg.robot_length_cm)
        forward_need += 12.5 + self.cfg.robot_width_cm / 2.0 + 15.0
        forward_have  = self._wall_dist_cm(s.heading) - self.cfg.wall_thickness_cm
        if forward_have >= forward_need:
            return True
        await self.push_message(
            f"⛔ Ручной режим: для разворота нужно {forward_need:.0f} см впереди, "
            f"есть только {forward_have:.0f}. Остановился. Отъехай назад вручную "
            f"и повтори команду.", "warning")
        return False

    async def _run_load_danger_zones(self,
                                       zones: list[tuple[float, float, float]]):
        """Идемпотентная установка списка опасных зон на карте.
        Очищает все текущие kind='danger' зоны пользователя (включая
        «осиротевшие» active-строки от прошлых runs) и кладёт новые
        с компактной нумерацией 1..N. Жёлтые зоны (kind='algorithm')
        не трогаем."""
        # 1) Снять все текущие красные зоны (in-memory) — авторитетная замена.
        self.world.danger_zones = [z for z in self.world.danger_zones
                                   if z.kind != "danger"]
        db = SessionLocal()
        try:
            # 2) Hard-delete ВСЕ красные зоны пользователя из DB
            #    (а не только те, чьи db_id были в текущем world) —
            #    чтобы не накапливались «осиротевшие» active-строки.
            (db.query(DangerZone)
               .filter(DangerZone.user_id == self.user_id,
                       DangerZone.kind == "danger")
               .delete(synchronize_session=False))
            db.commit()
            # 2) Положить новые с display_no 1..N.
            for i, (zx, zy, zr) in enumerate(zones, start=1):
                zone = self.world.add_danger_zone(
                    float(zx), float(zy), radius=float(zr),
                    label="Зона опасности", kind="danger", display_no=i)
                dz = DangerZone(user_id=self.user_id, label=zone.label,
                                x=zone.x, y=zone.y, radius=zone.radius,
                                kind="danger", display_no=i)
                db.add(dz)
                db.flush()
                zone.db_id = dz.id
            db.commit()
        finally:
            db.close()
        await self.push_message(
            f"🗺 Загружено {len(zones)} опасных зон обстановки.", "info")
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
        # Подробное сообщение с #N для каждого удалённого — обучающийся
        # видит какую именно зону снял.
        for z in removed:
            kind_lbl = self._zone_label_human(z.kind)
            if z.display_no:
                await self.push_message(
                    f"✓ {kind_lbl} #{z.display_no} удалена "
                    f"в ({z.x:.0f}, {z.y:.0f}).", "info")
        # Перенумеровываем оставшиеся зоны затронутых kind'ов компактно
        # (без дыр в DANGER_ZONES блоке и в журнале).
        for k in {z.kind for z in removed}:
            self._renumber_zones(k, db)
        await self.push_world()
        return len(removed)

    def _next_zone_display_no(self, kind: str) -> int:
        """Следующий номер для НОВОЙ зоны указанного kind. Нумерация
        компактная (без пропусков): благодаря `_renumber_zones`,
        активные зоны всегда занимают 1..N, значит новая получит N+1."""
        n_active = sum(1 for z in self.world.danger_zones if z.kind == kind)
        return n_active + 1

    def _renumber_zones(self, kind: str, db_session=None) -> None:
        """Перенумеровывает активные зоны указанного kind в 1..N по
        порядку создания (db_id), пишет обновления в world И в DB.
        Вызывается после удаления зон, чтобы в коде/журнале не было
        «дыр» в нумерации (после ⛯ ставит/убирает мышью)."""
        active = sorted(
            (z for z in self.world.danger_zones if z.kind == kind),
            key=lambda z: z.db_id or 0)
        updates: list[tuple[int, int]] = []   # (db_id, new_no)
        for idx, z in enumerate(active, start=1):
            if z.display_no != idx:
                z.display_no = idx
                if z.db_id is not None:
                    updates.append((z.db_id, idx))
        if db_session and updates:
            for db_id, new_no in updates:
                (db_session.query(DangerZone)
                    .filter(DangerZone.id == db_id)
                    .update({"display_no": new_no},
                            synchronize_session=False))
            db_session.commit()

    @staticmethod
    def _zone_label_human(kind: str) -> str:
        """Человекочитаемое название типа зоны для сообщений журнала."""
        return "Опасная" if kind == "danger" else "Внимания"

    def _obstacle_stop_message(self, direction: str = "вперёд") -> str:
        """Сообщение для forward_to_wall/backward_to_wall в зависимости
        от того, что реально остановило робота:
          • зона на пути (cautious-режим, _clip_dist_cautious остановил
            ДО зоны с margin 20 см) — «Стоп перед опасной зоной #N»;
          • корпус касается зоны (защитный стоп физики) — то же;
          • иначе (упёрся в границу поля) — «Достиг стены»."""
        s = self.robot_state
        suffix = "" if direction == "вперёд" else " (назад)"
        kind_lbl_fn = self._zone_label_human

        def _fmt(z) -> str:
            kind_lbl = kind_lbl_fn(z.kind).lower()
            no = z.display_no or 0
            tag = f" #{no}" if no else ""
            return f"⚠ Стоп перед {kind_lbl} зоной{tag}{suffix}."

        # Check 1: угол корпуса ВНУТРИ зоны (защитный стоп).
        try:
            corners = self._robot_corners()
        except Exception:
            corners = [(s.x, s.y)]
        for cx, cy in corners:
            for z in self.world.danger_zones:
                if (cx - z.x) ** 2 + (cy - z.y) ** 2 <= z.radius ** 2:
                    return _fmt(z)

        # Check 2: зона на пути и робот стоит на «stop-margin» расстоянии
        # от неё (то, что делает _clip_dist_cautious при движении к стене).
        # Используем ту же геометрию: half-plane вперёд от носа + perp <
        # radius+MARGIN + dot почти равен stop_r.
        sign = 1.0 if direction == "вперёд" else -1.0
        hrad = math.radians(s.heading)
        dx = math.sin(hrad) * sign
        dy = math.cos(hrad) * sign
        MARGIN = 20.0    # совпадает с _clip_dist_cautious
        TOL    = 6.0     # допуск «стоит у самой кромки»
        nearest = None
        nearest_dot = None
        for z in self.world.danger_zones:
            zx = z.x - s.x
            zy = z.y - s.y
            dot = zx * dx + zy * dy
            if dot <= 0:
                continue                       # зона сзади
            perp = abs(zx * dy - zy * dx)
            stop_r = z.radius + MARGIN
            if perp >= stop_r:
                continue                       # зона не на «полосе» движения
            # Робот должен находиться в радиусе stop_r ± TOL от центра зоны
            # по продольной оси — это «упёрся в зону по лазеру».
            if dot - stop_r <= TOL:
                if nearest is None or dot < nearest_dot:
                    nearest = z
                    nearest_dot = dot
        if nearest is not None:
            return _fmt(nearest)

        return f"Достиг стены{suffix}."

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
                # HARD-DELETE (а не active=False). UI-мышь = pre-flight
                # подготовка обстановки: «поставил и снял» = «как будто
                # не было». После удаления — перенумеровываем оставшиеся
                # красные зоны в 1..N (без дыр).
                (db.query(DangerZone)
                   .filter(DangerZone.id.in_(ids))
                   .delete(synchronize_session=False))
                db.commit()
        for z in removed:
            if z.display_no:
                await self.push_message(
                    f"✕ Опасная #{z.display_no} снята с карты "
                    f"в ({z.x:.0f}, {z.y:.0f}).", "info")
        # Перенумеровываем оставшиеся красные зоны компактно.
        self._renumber_zones("danger", db)
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
        no = self._next_zone_display_no("algorithm")
        zone = self.world.add_danger_zone(s.x, s.y,
                                          radius=r,
                                          label="Зона внимания",
                                          kind="algorithm",
                                          display_no=no)
        if db:
            dz = DangerZone(user_id=self.user_id, label=zone.label,
                            x=zone.x, y=zone.y, radius=zone.radius,
                            kind="algorithm", display_no=no)
            db.add(dz)
            db.commit()
            zone.db_id = dz.id
        await self.push_message(
            f"🟡 Внимания #{no} в ({zone.x:.0f}, {zone.y:.0f}), r={r:.0f}.",
            "info")
        await self.push_world()

    async def _run_place_attention_here(self, radius: Optional[float] = None,
                                        db: Session = None):
        """Поставить зону внимания (жёлтая пунктирная) в ТЕКУЩЕЙ позиции
        робота — без поездки. Используется UI-кнопкой «🟡 Установить зону»:
        пользователь сначала вручную едет в нужное место, потом помечает."""
        s = self.robot_state
        r = float(radius) if radius is not None else float(self.cfg.danger_zone_radius)
        no = self._next_zone_display_no("algorithm")
        await self.push_message(
            f"🟡 Внимания #{no} в ({s.x:.0f}, {s.y:.0f}), r={r:.0f}.",
            "info")
        zone = self.world.add_danger_zone(s.x, s.y,
                                          radius=r,
                                          label="Зона внимания",
                                          kind="algorithm",
                                          display_no=no)
        if db:
            dz = DangerZone(user_id=self.user_id, label=zone.label,
                            x=zone.x, y=zone.y, radius=zone.radius,
                            kind="algorithm", display_no=no)
            db.add(dz)
            db.commit()
            zone.db_id = dz.id
        await self.push_world()

    @staticmethod
    def _parse_start_from_python(text: Optional[str]) -> tuple[Optional[float], Optional[float], Optional[float]]:
        """Извлекает константы `START_X` / `START_Y` / `START_HEADING_DEG`
        из текста пользовательской программы. Возвращает (x, y, heading),
        каждое — float или None если не нашли / не распарсилось.

        Регулярка матчит присваивания на уровне модуля:
            START_X = 100.0
            START_Y  = -50
            START_HEADING_DEG=90
        Допускаются комментарии после значения."""
        if not text:
            return None, None, None
        def _grab(name: str) -> Optional[float]:
            m = re.search(
                rf'^\s*{name}\s*=\s*(-?\d+\.?\d*)\s*(?:#.*)?$',
                text, re.MULTILINE)
            if not m:
                return None
            try:
                return float(m.group(1))
            except ValueError:
                return None
        return _grab("START_X"), _grab("START_Y"), _grab("START_HEADING_DEG")

    @staticmethod
    def _parse_obstacles_from_python(text: Optional[str]):
        """Извлекает набор препятствий из строки `OBSTACLES = [...]`.

        Возвращает set ⊆ {"danger", "attention"} (возможно пустой) либо
        None, если строки нет. Код главнее галочек UI (как START_X): при
        ▶ Запуске набор берётся отсюда. Стены в набор не входят — они
        препятствие всегда."""
        if not text:
            return None
        m = re.search(r'^\s*OBSTACLES\s*=\s*\[([^\]]*)\]',
                      text, re.MULTILINE)
        if not m:
            return None
        found = set(re.findall(r'["\'](\w+)["\']', m.group(1)))
        return {k for k in found if k in ("danger", "attention")}

    @classmethod
    def _parse_danger_zones_from_python(cls,
                                          text: Optional[str]
                                          ) -> list[tuple[float, float, float]]:
        """Извлекает список (x, y, r) из блока DANGER_ZONES = [...] в
        преамбуле программы (между сентинелями OBSTACLE_BLOCK_TOP/BOT).
        Используется при ↺/▶ для восстановления опасных зон обстановки —
        они в коде декларативно, но при reset их надо вернуть в world.

        Парсит ТОЛЬКО внутри блока — DANGER_ZONES вне сентинелей не
        учитывается (на случай если пользователь определил переменную
        с тем же именем в своей логике)."""
        if not text:
            return []
        top_idx = text.find(cls.OBSTACLE_BLOCK_TOP)
        if top_idx < 0:
            return []
        bot_idx = text.find(cls.OBSTACLE_BLOCK_BOT, top_idx)
        if bot_idx < 0:
            return []
        block = text[top_idx:bot_idx]
        m = re.search(r'DANGER_ZONES\s*=\s*\[(.*?)\]', block, re.DOTALL)
        if not m:
            return []
        zones: list[tuple[float, float, float]] = []
        for m2 in re.finditer(
            r'\(\s*(-?\d+\.?\d*)\s*,\s*(-?\d+\.?\d*)\s*,\s*(-?\d+\.?\d*)\s*\)',
            m.group(1)):
            try:
                zones.append((float(m2.group(1)), float(m2.group(2)),
                              float(m2.group(3))))
            except ValueError:
                pass
        return zones

    async def _run_reset(self, db: Session = None, keep_mode: bool = False,
                         code_text: Optional[str] = None):
        """Сброс поля.

        keep_mode=False (по умолчанию, явная команда «↺ Поле») — полный
        сброс, включая режим (инспектор / осторожно).
        keep_mode=True — частичный сброс для replay ▶ Запуск кода и
        для активации миссии: пользовательский выбор «осторожно» должен
        сохраниться через перезапуск, иначе action-кнопки моргают.

        Начальная позиция: пытаемся взять `START_X` / `START_Y` /
        `START_HEADING_DEG` из текста программы (код — главнее настроек).
        Источник кода:
          • `code_text` параметр (если передан) — используется при ▶ Run,
            это всегда самый свежий текст из textarea клиента;
          • иначе `self._last_python_code` — кэш с прошлого sync_code/▶;
          • иначе fallback к cfg (преамбула, очищенная ✕)."""
        s = self.robot_state
        # SNAPSHOT опасных зон ДО очистки — нужен как fallback при ↺ Поле
        # без свежего кода (когда serverкэш _last_python_code устарел и
        # парсинг DANGER_ZONES даст пусто). Снимаем только kind="danger":
        # зоны внимания — runtime-сущность, при reset они и должны исчезать.
        snapshot_danger = [(z.x, z.y, z.radius)
                           for z in self.world.danger_zones
                           if z.kind == "danger"]
        self.world.clear_danger_zones()
        self.world.clear_path()
        self.world.clear_auto_segments()
        # Если передан свежий код — кэшируем и парсим его. Иначе берём
        # последний известный.
        if code_text is not None:
            self._last_python_code = code_text
        code_x, code_y, code_h = self._parse_start_from_python(self._last_python_code)
        eff_x = float(code_x) if code_x is not None else float(self.cfg.start_x_cm)
        eff_y = float(code_y) if code_y is not None else float(self.cfg.start_y_cm)
        eff_h = (float(code_h) if code_h is not None
                 else float(self.cfg.start_heading_deg)) % 360
        # Запоминаем — _world_dict отдаст эти значения клиенту, и зелёный
        # маркер старта переедет туда, где сейчас стартует робот.
        self._effective_start_x = eff_x
        self._effective_start_y = eff_y
        self._effective_start_heading = eff_h
        s.x = eff_x
        s.y = eff_y
        s.heading = eff_h
        s.speed     = 0
        s.steer     = 0.0
        s.dist_left = 0
        if not keep_mode:
            s.mode = "normal"
        # Препятствия — из строки OBSTACLES в коде (код главнее галочек,
        # как START_X). Нет строки: при полном сбросе ↺ Поле → только
        # стены; при keep_mode (▶ Run / миссия) — оставляем текущий набор.
        code_obs = self._parse_obstacles_from_python(self._last_python_code)
        if code_obs is not None:
            self._apply_obstacles(code_obs)
        elif not keep_mode:
            self._apply_obstacles(set())
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
        # Деактивация ВСЕХ зон пользователя в DB — должна происходить
        # всегда, в т.ч. при ▶ Run (где db приходит как None). Иначе
        # активные строки от прошлых runs накапливаются и при ребуте
        # сессии вылезают как «зомби-зоны».
        _dz_db = db if db is not None else SessionLocal()
        _dz_close = (db is None)
        try:
            (_dz_db.query(DangerZone)
                   .filter(DangerZone.user_id == self.user_id)
                   .update({"active": False}))
            _dz_db.commit()
        finally:
            if _dz_close:
                _dz_db.close()
        # Восстановление опасных зон обстановки после очистки world+DB:
        #   1) Миссия активна → берём из self._mission (авторитет миссии).
        #   2) ⛯ «Режим зон» активен → НЕ восстанавливаем. Пользователь
        #      в режиме setup'а, ↺ Поле = «начать расстановку с нуля».
        #   3) ▶ Run (code_text передан) → парсим DANGER_ZONES из СВЕЖЕГО
        #      кода. Пользователь мог отредактировать список — его правки
        #      должны примениться.
        #   4) ↺ Поле без code_text → используем SNAPSHOT текущих зон.
        #      В Инспекторе/Осторожно «обстановка» (красные) сохраняется,
        #      ↺ только возвращает робота в старт.
        #   5) Fallback — парсинг из кэша _last_python_code (для загрузки
        #      сессии, когда снапшот пустой).
        zones_to_place: list[tuple[float, float, float]] = []
        if self._mission and self._mission.danger_zones:
            zones_to_place = [(float(x), float(y), float(r))
                              for (x, y, r) in self._mission.danger_zones]
        elif s.zone_mode:
            zones_to_place = []   # явный «полный сброс» в режиме зон
        elif code_text is not None:
            zones_to_place = self._parse_danger_zones_from_python(code_text)
        elif snapshot_danger:
            zones_to_place = snapshot_danger
        else:
            zones_to_place = self._parse_danger_zones_from_python(
                self._last_python_code)
        if zones_to_place:
            _local_db = db
            _close_local = False
            if _local_db is None:
                _local_db = SessionLocal()
                _close_local = True
            try:
                for (zx, zy, zr) in zones_to_place:
                    no = self._next_zone_display_no("danger")
                    zone = self.world.add_danger_zone(
                        float(zx), float(zy), radius=float(zr),
                        label="Зона опасности", kind="danger",
                        display_no=no)
                    dz = DangerZone(user_id=self.user_id, label=zone.label,
                                    x=zone.x, y=zone.y, radius=zone.radius,
                                    kind="danger", display_no=no)
                    _local_db.add(dz)
                    _local_db.flush()
                    zone.db_id = dz.id
                _local_db.commit()
            finally:
                if _close_local:
                    _local_db.close()
        await self.push_world()
        # Пушим и состояние робота: при reset s.x/s.y/s.heading изменились,
        # а push_state по умолчанию шлёт физика только когда speed != 0.
        # Без явного push клиент не увидит телепорт при ▶ Run (а ↺ Поле
        # работало случайно — там _dispatch сам push_state шлёт в конце).
        await self.push_state()

    async def apply_obstacles_from_ui(self, obs) -> None:
        """Галочки «Препятствия» сменены в UI. Применяем набор,
        переписываем строку OBSTACLES в коде, синкаем состояние."""
        if self._mission is not None and self._mission.danger_zones:
            await self.push_message(
                "Во время миссии с опасными зонами набор препятствий "
                "переключать нельзя.", "warning")
            await self.push_state()
            return
        self._apply_obstacles(obs)
        await self.broadcast({"type": "obstacles_line",
                              "obstacles": sorted(self.robot_state.obstacles)})
        await self.push_message(self._obstacles_human(), "info")
        await self.push_state()

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
        elif intent == "set_turn_angle":
            ang = nlu.extract_default_turn_angle(raw, c.turn_angle)
            code, label = f"set_turn_angle({ang})", f"Угол руля {ang}°"
        elif intent == "turn_around":
            n = nlu.norm(raw)
            side = "влево" if any(w in n for w in ("налево", "влево", "против")) else "вправо"
            code, label = "turn_around()", f"Разворот {side}"
        elif intent == "turn_around_place":
            steps = nlu.extract_kturn_steps(raw)
            code, label = f"turn_around_place({steps})", f"Разворот на месте за {steps} шагов"
        elif intent == "circle":
            # «Круг» — алиас для arc(360). Метода circle() больше нет.
            n = nlu.norm(raw)
            cw = any(w in n for w in ("направо", "вправо", "по часовой"))
            side = "по часовой" if cw else "против часовой"
            code, label = "arc(360)", f"Круг {side}"
        elif intent == "arc":
            n = nlu.norm(raw)
            cw = any(w in n for w in ("направо", "вправо", "по часовой"))
            ang = nlu.extract_arc_angle(raw)
            side = "по часовой" if cw else "против часовой"
            code, label = f"arc({ang})", f"Дуга {ang}° {side}"
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
        # «mark_danger» удалён — красные зоны теперь не команды программы,
        # а обстановка (см. mission load в session.start).
        elif intent == "pause":
            secs = nlu.extract_pause_seconds(raw)
            code, label = f"pause({secs:g})", f"⏸ Пауза {secs:g} с"
        elif intent == "set_algorithm_zone":
            xy = nlu.extract_coordinates(raw)
            r = nlu.extract_radius(raw)
            if xy is None:
                # UI-кнопка «🟡 Установить зону» / голос «установи зону»
                # без координат → зона рисуется в текущей позиции робота
                # (без поездки). Координаты подставит _dispatch.
                if r is not None:
                    code  = f"set_algorithm_zone({r:.0f})"
                    label = f"🟡 Зона внимания здесь, r={r:.0f}"
                else:
                    code  = "set_algorithm_zone()"
                    label = "🟡 Зона внимания здесь"
            else:
                tx, ty = xy
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
        elif intent == "mark_danger":
            # UI-команда из «⛯ Режим зон» (ЛКМ): pre-flight-установка
            # красной зоны мышью. Парный с remove_danger_zone.
            # В Python-код программы НЕ пишется (skip_record в _dispatch).
            xy = nlu.extract_coordinates(raw)
            if xy is None:
                return None
            tx, ty = xy
            r = nlu.extract_radius(raw)
            r_txt = f" r={r:.0f}" if r is not None else ""
            code  = f"# UI: опасная зона ({tx:.0f}, {ty:.0f}){r_txt}"
            label = f"⚠ Опасная зона ({tx:.0f}, {ty:.0f}){r_txt}"
        elif intent == "mode_inspector":
            code, label = "mode(inspector)", "Режим инспектор"
        elif intent == "mode_cautious":
            code, label = "mode(cautious)", "Режим Опасно"
        elif intent == "path_show":
            code, label = "path_show()", "Показать путь"
        elif intent == "path_hide":
            code, label = "path_hide()", "Скрыть путь"
        elif intent == "reset":
            code, label = "robot.clear()", "Новое поле"
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
    # Day 2: реестр хелперов (_HELPER_DEPS + _HELPER_CODE + _helpers_for_cmd
    # + _collect_helpers) удалён вместе с парсером. Преамбула теперь —
    # только константы (см. _python_constants_block ниже); тело программы
    # пишется в стиле robot.X(...) и исполняется напрямую через exec().

    # Порядок типов в строке OBSTACLES — стабильный.
    _OBSTACLE_ORDER = ("danger", "attention")

    def _apply_obstacles(self, obs) -> None:
        """Единая точка установки набора препятствий. Держит s.obstacles
        и производный s.cautious согласованными. Стены в набор не входят —
        они препятствие всегда."""
        s = self.robot_state
        s.obstacles = {k for k in obs if k in self._OBSTACLE_ORDER}
        s.cautious  = bool(s.obstacles)

    def _zone_is_obstacle(self, zone) -> bool:
        """Является ли зона препятствием при текущем наборе s.obstacles.
        kind 'algorithm' (жёлтая зона внимания) ↔ ключ 'attention'."""
        key = ("attention" if getattr(zone, "kind", "danger") == "algorithm"
               else "danger")
        return key in self.robot_state.obstacles

    def _obstacles_repr(self) -> str:
        """Литерал списка для строки OBSTACLES в коде, напр. ["danger"]."""
        obs = self.robot_state.obstacles
        items = [k for k in self._OBSTACLE_ORDER if k in obs]
        return "[" + ", ".join(f'"{k}"' for k in items) + "]"

    def _obstacles_human(self) -> str:
        """Человекочитаемое описание текущего набора препятствий."""
        obs = self.robot_state.obstacles
        parts = ["стены"]
        if "danger" in obs:    parts.append("опасные зоны")
        if "attention" in obs: parts.append("зоны внимания")
        return "Препятствия: " + ", ".join(parts) + "."

    def _python_constants_block(self) -> str:
        c = self.cfg
        return (
            "# Программа для 1T REX. Команды robot.X(...) — см. «Справка → API».\n"
            "import math, time\n"
            "\n"
            "# ── Препятствия: на что робот реагирует авто-стопом ──────────\n"
            "# Стены — всегда препятствие. Дополнительно (галочки в UI):\n"
            "#   \"danger\"    — опасные зоны\n"
            "#   \"attention\" — зоны внимания\n"
            f"OBSTACLES = {self._obstacles_repr()}\n"
            "\n"
            "# ── RGB-индикатор на плате (для robot.set_rgb) ───────────────\n"
            f"LIGHT_INDEX         = {LIGHT_INDEX}      # индекс LED\n"
            f"LIGHT_DELAY_SEC     = {LIGHT_DELAY_SEC}    # задержка между каналами (0 = мгновенно)\n"
            f"LIGHT_DEFAULT_COLOR = {LIGHT_DEFAULT_COLOR}  # белый по умолчанию (R, G, B)\n"
            "\n"
            "# ── Стартовая точка робота — отсюда начнётся каждый ▶ Запуск ──\n"
            "# Курс 0° = «север» (вверх по экрану). 90=E, 180=S, 270=W.\n"
            f"START_X           = {c.start_x_cm:.1f}\n"
            f"START_Y           = {c.start_y_cm:.1f}\n"
            f"START_HEADING_DEG = {c.start_heading_deg:.1f}\n"
        )

    # Точные строки-сентинели блока обстановки — клиент использует их
    # для surgical replace в textarea (см. obstacle_block WS-сообщение
    # в control.js). НЕ менять без синхронной правки клиента.
    OBSTACLE_BLOCK_TOP = "# === Опасные зоны обстановки ==="
    OBSTACLE_BLOCK_BOT = "# === Конец опасных зон обстановки ==="

    def _obstacle_block(self) -> str:
        """Блок с текущими опасными зонами для преамбулы кода.

        Обновляется автоматически на выходе из «⛯ Режим зон» (сервер шлёт
        клиенту obstacle_block WS-сообщение, клиент заменяет блок в textarea
        между сентинелями). Программа МОЖЕТ читать DANGER_ZONES (это
        обычный Python-список), но НЕ может создавать новые красные зоны
        — только удалять через `robot.remove_zone(x, y)` когда робот внутри.
        """
        danger = sorted(
            (z for z in self.world.danger_zones if z.kind == "danger"),
            key=lambda z: z.display_no or 0)
        lines = [
            self.OBSTACLE_BLOCK_TOP,
            "# Автоблок: обновляется при выходе из режима «Обстановка».",
            "# Программа может читать DANGER_ZONES, но НЕ создаёт новые "
            "красные зоны.",
        ]
        if not danger:
            lines.append("# Сейчас опасных зон нет.")
            lines.append("DANGER_ZONES = []")
        else:
            lines.append("DANGER_ZONES = [")
            lines.append("    # (x, y, radius)")
            for z in danger:
                no = z.display_no or 0
                no_tag = f"  # Опасная #{no}" if no else ""
                lines.append(
                    f"    ({z.x:.1f}, {z.y:.1f}, {z.radius:.1f}),{no_tag}")
            lines.append("]")
        # Явный вызов установки: пользователь видит ДЕЙСТВИЕ, а не только
        # координаты. Метод идемпотентен — повторный запуск с теми же
        # данными не плодит дубликатов.
        lines.append("robot.load_danger_zones(DANGER_ZONES)  "
                     "# установить зоны на карте")
        lines.append(self.OBSTACLE_BLOCK_BOT)
        return "\n".join(lines) + "\n"

    def _python_code_preamble(self, cmds=None) -> str:
        """Преамбула: константы + блок ОБСТАНОВКА + сентинель НАЧАЛО ПРОГРАММЫ.
        Day 2: def-блоки хелперов больше не вставляются — robot.X(...)
        вызовы исполняются напрямую через robot_api. cmds-параметр
        оставлен для совместимости сигнатуры с вызывающим кодом."""
        return (
            self._python_constants_block()
            + "\n"
            + self._obstacle_block()
            + "\n# === НАЧАЛО ПРОГРАММЫ ===\n"
        )

    def _python_code_for_cmd(self, cmd: RobotCmd) -> tuple[str, str]:
        """Полный Python-код одной команды для отправки клиенту:
        преамбула + строка вызова (`robot.X(...)`)."""
        description = cmd.label
        body = "\n".join(self._python_call_lines_for_cmd(cmd))
        return description, self._python_code_preamble() + body

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
        "steer_center", "brake", "stop", "set_speed", "set_turn_angle",
        "pause", "light_on", "light_off", "light_color", "recharge",
        "mode_inspector", "mode_cautious", "path_show", "path_hide",
        "reset", "report_pos", "report_status",
    })

    # Inline-комментарий после кода: 2+ пробела + `#` + хвост до конца строки.
    # Используется в постобработке call-строк, см. _strip_codegen_comments.
    _RE_INLINE_COMMENT = re.compile(r'\s{2,}#.*$')

    def _python_call_lines_for_cmd(self, cmd: RobotCmd) -> list[str]:
        """Строки вызова команды для textarea — высокоуровневый Python-API
        `robot.X(...)`. Преамбула с константами + эти строки = полная
        программа, исполняемая через настоящий exec() (см. robot_api.py).

        В конце пропускаем через `_strip_codegen_comments` — убирает
        inline-комментарии и маркер `# CMD: …`. Источник истины — само
        тело команды, не комментарии."""
        raw = cmd.raw
        intent = cmd.intent
        c = self.cfg
        dist = nlu.extract_distance(raw)
        code_lines: list[str] = []

        if intent == "forward":
            d = dist if dist else 20
            code_lines += [f"robot.forward({d})  # вперёд {d} см"]
        elif intent == "back":
            d = dist if dist else 20
            code_lines += [f"robot.back({d})  # назад {d} см"]
        elif intent == "forward_to_wall":
            code_lines += ["robot.forward_to_wall()  # вперёд до стены"]
        elif intent == "backward_to_wall":
            code_lines += ["robot.backward_to_wall()  # назад до стены"]
        elif intent == "brake":
            code_lines += ["robot.stop()  # тормоз"]
        elif intent == "stop":
            code_lines += ["robot.stop()  # остановка"]
        elif intent in ("steer_right", "steer_left", "steer_right_small", "steer_left_small"):
            steer_delta = nlu.extract_angle(raw) or (nlu.SMALL_STEER_DEG if intent.endswith("small") else c.turn_angle)
            if intent in ("steer_left", "steer_left_small"):
                steer_delta = -steer_delta
            side = "налево" if steer_delta < 0 else "направо"
            code_lines += [f"robot.set_angle({steer_delta})  # руль {side} на {abs(steer_delta):g}°"]
        elif intent == "steer_center":
            code_lines += ["robot.set_angle(0)  # руль прямо"]
        elif intent == "set_speed":
            spd = nlu.extract_speed(raw, c.move_speed)
            code_lines += [f"robot.set_default_speed({spd})  # скорость по умолчанию {spd}%"]
        elif intent == "set_turn_angle":
            ang = nlu.extract_default_turn_angle(raw, c.turn_angle)
            code_lines += [f"robot.set_default_turn_angle({ang})  # угол руля по умолчанию {ang}°"]
        elif intent == "turn_around":
            direction = -1 if any(w in nlu.norm(raw) for w in ("налево", "влево", "против")) else 1
            side = "налево" if direction < 0 else "направо"
            code_lines += [f"robot.turn_around({direction})  # разворот на 180° {side}"]
        elif intent == "turn_around_place":
            steps = nlu.extract_kturn_steps(raw)
            direction = -1 if any(w in nlu.norm(raw) for w in ("налево", "влево", "против")) else 1
            code_lines += [f"robot.kturn(steps={steps}, direction={direction})  # разворот на месте за {steps} шагов"]
        elif intent == "circle":
            direction = +1 if any(w in nlu.norm(raw) for w in ("направо", "вправо", "по часовой")) else -1
            side = "по часовой" if direction > 0 else "против часовой"
            code_lines += [f"robot.arc(360, {direction})  # круг {side}"]
        elif intent == "arc":
            direction = +1 if any(w in nlu.norm(raw) for w in ("направо", "вправо", "по часовой")) else -1
            ang = nlu.extract_arc_angle(raw)
            side = "по часовой" if direction > 0 else "против часовой"
            code_lines += [f"robot.arc({ang}, {direction})  # дуга {ang}° {side}"]
        elif intent == "figure_eight":
            direction = +1 if any(w in nlu.norm(raw) for w in ("направо", "вправо", "по часовой")) else -1
            code_lines += [f"robot.figure_eight({direction})  # восьмёрка"]
        elif intent == "spiral_out":
            direction = +1 if any(w in nlu.norm(raw) for w in ("направо", "вправо", "по часовой")) else -1
            code_lines += [f"robot.spiral({direction}, outward=True)  # спираль наружу"]
        elif intent == "spiral_in":
            direction = +1 if any(w in nlu.norm(raw) for w in ("направо", "вправо", "по часовой")) else -1
            code_lines += [f"robot.spiral({direction}, outward=False)  # спираль внутрь"]
        elif intent in ("bypass_right", "bypass_left"):
            start_dir = +1 if intent == "bypass_right" else -1
            side = "справа" if start_dir > 0 else "слева"
            code_lines += [f"robot.bypass({start_dir})  # объезд препятствия {side}"]
        elif intent == "goto":
            xy = nlu.extract_coordinates(raw)
            if xy is None:
                code_lines += ["# координаты не указаны"]
            else:
                tx, ty = xy
                code_lines += [f"robot.goto({tx:g}, {ty:g})  # перейти в точку ({tx:g}, {ty:g})"]
        elif intent == "home":
            code_lines += ["robot.home()  # вернуться в стартовую точку"]
        elif intent in ("face_n", "face_ne", "face_e", "face_se",
                         "face_s", "face_sw", "face_w", "face_nw"):
            cardinal_deg = {"face_n":0, "face_ne":45, "face_e":90, "face_se":135,
                             "face_s":180, "face_sw":225, "face_w":270, "face_nw":315}
            cardinal_lbl = {"face_n":"на север", "face_ne":"на северо-восток",
                            "face_e":"на восток", "face_se":"на юго-восток",
                            "face_s":"на юг", "face_sw":"на юго-запад",
                            "face_w":"на запад", "face_nw":"на северо-запад"}
            tgt = cardinal_deg[intent]
            code_lines += [f"robot.face({tgt})  # {cardinal_lbl[intent]}"]
        elif intent == "face_to":
            deg = nlu.extract_face_angle(raw)
            if deg is None:
                code_lines += ["# угол поворота не распознан"]
            else:
                code_lines += [f"robot.face({deg})  # поворот на {deg}°"]
        elif intent == "set_course":
            target = nlu.extract_course(raw)
            if target is None:
                code_lines += ["# курс не распознан"]
            else:
                code_lines += [f"robot.set_course({target})  # выставить курс {target}°"]
        # «mark_danger» удалён из кодогена — программа не может создавать
        # красные зоны (только удалять через remove_zone).
        elif intent == "pause":
            secs = nlu.extract_pause_seconds(raw)
            code_lines += [f"robot.wait({secs:g})  # пауза {secs:g} с"]
        elif intent == "set_algorithm_zone":
            xy = nlu.extract_coordinates(raw)
            r  = nlu.extract_radius(raw)
            if xy is None:
                if r is not None:
                    code_lines += [f"robot.attention_here({r:g})  # зона внимания здесь, r={r:g}"]
                else:
                    code_lines += ["robot.attention_here()  # зона внимания здесь"]
            else:
                tx, ty = xy
                if r is not None:
                    code_lines += [f"robot.attention_zone({tx:g}, {ty:g}, {r:g})  # зона внимания ({tx:g}, {ty:g}) r={r:g}"]
                else:
                    code_lines += [f"robot.attention_zone({tx:g}, {ty:g})  # зона внимания ({tx:g}, {ty:g})"]
        elif intent == "remove_zone":
            xy = nlu.extract_coordinates(raw)
            if xy is None:
                code_lines += ["robot.remove_zone_here()  # убрать зону под роботом"]
            else:
                tx, ty = xy
                code_lines += [f"robot.remove_zone({tx:g}, {ty:g})  # убрать зону в ({tx:g}, {ty:g})"]
        elif intent == "mode_inspector":
            code_lines += ["# режим «инспектор» — интерфейсный, не записывается в программу"]
        elif intent == "mode_cautious":
            code_lines += ["# режим «Опасно» — интерфейсный, не записывается в программу"]
        elif intent == "path_show":
            code_lines += ["# показать путь — интерфейсное"]
        elif intent == "path_hide":
            code_lines += ["# скрыть путь — интерфейсное"]
        elif intent == "reset":
            code_lines += ["robot.clear()  # очистить поле: зоны, путь, в стартовую точку"]
        elif intent == "report_pos":
            code_lines += ["print(f'X={robot.x:.0f}, Y={robot.y:.0f}, курс={robot.heading:.0f}°')"]
        elif intent == "report_status":
            code_lines += [
                "print(f'X={robot.x:.0f}, Y={robot.y:.0f}, "
                "курс={robot.heading:.0f}°')",
            ]
        elif intent == "light_on":
            code_lines += ["robot.set_rgb(LIGHT_INDEX, LIGHT_DEFAULT_COLOR, LIGHT_DELAY_SEC)  # свет вкл"]
        elif intent == "light_off":
            code_lines += ["robot.set_rgb(LIGHT_INDEX, (0, 0, 0), LIGHT_DELAY_SEC)  # свет выкл"]
        elif intent == "light_color":
            color = nlu.extract_color(raw) or LIGHT_DEFAULT_COLOR
            code_lines += [f"robot.set_rgb(LIGHT_INDEX, {color}, LIGHT_DELAY_SEC)  # цвет {color}"]
        elif intent == "recharge":
            code_lines += ["robot.recharge()  # зарядить батарею"]
        else:
            code_lines += ["# нет шаблона"]
        return self._strip_codegen_comments(code_lines)

    @classmethod
    def _strip_codegen_comments(cls, lines: list[str]) -> list[str]:
        """Убирает из генерируемых call-строк всё, что выглядит как
        комментарий: inline `  # …` и старый маркер `# CMD: …`.

        Пустые строки (исключительно из-за «всё было комментарием»)
        отбрасываются. Полностью пустых строк изначально в генерации нет,
        поэтому случайных вырезаний не будет."""
        out: list[str] = []
        for line in lines:
            stripped = line.lstrip()
            if stripped.startswith("# CMD"):
                # Старый маркер — главный источник путаницы (пользователь
                # его правил, думая что это команда). Полностью удаляем.
                continue
            if stripped.startswith("#"):
                # Самостоятельная строка-комментарий (плэйсхолдер вроде
                # "# координаты не указаны") — тоже убираем, чтобы в коде
                # не было ничего, что не является исполнимым телом.
                continue
            cleaned = cls._RE_INLINE_COMMENT.sub("", line).rstrip()
            if cleaned:
                out.append(cleaned)
        return out

    # ── Выполнение одной команды ─────────────────────────────────────────────

    async def _dispatch(self, cmd: RobotCmd, db: Session = None):
        s = self.robot_state
        c = self.cfg
        intent = cmd.intent
        raw    = cmd.raw
        msg    = ""
        ok     = True

        # ── Cautious + forward: clip & shorten вместо паузы ────────────
        # Если впереди опасная зона, не отдаём управление в goto/manual
        # (что вызывало бы паузу), а сами укорачиваем команду до безопасной
        # дистанции и редактируем cmd.raw — кодоген ниже увидит уже новую
        # длину и в textarea появится «forward(45)» вместо «forward(100)».
        # Если приблизиться нельзя совсем — отменяем команду полностью.
        if (intent == "forward" and not cmd.playback
                and s.cautious and self.world.danger_zones):
            req = nlu.extract_distance(raw)
            if req:
                clipped = self._clip_dist_cautious(float(req))
                if clipped < 1.0:
                    # Не приблизиться — отменяем команду целиком.
                    await self.push_message(
                        f"⚠ Препятствие на курсе — forward({req}) "
                        f"отменена, ехать нельзя.", "warning")
                    return
                if clipped < req - 0.5:
                    # Сокращаем до безопасной дистанции.
                    adj = int(clipped)
                    await self.push_message(
                        f"⚠ Препятствие — forward({req}) сокращена "
                        f"до forward({adj}).", "info")
                    # Подменяем raw (cодержит дистанцию текстом) — кодоген
                    # ниже снова дернёт extract_distance и увидит {adj}.
                    cmd.raw = re.sub(r'\d+', str(adj), cmd.raw, count=1)
                    cmd.code = f"forward({adj})"
                    cmd.label = f"Вперед {adj} см"
                    raw = cmd.raw   # обновим локальную переменную

        # ── Cautious + back: то же, что forward, но в направлении кормы ──
        # Зеркало блока выше: укорачиваем back(N) до безопасной дистанции,
        # проверяя зоны по курсу кормы (heading+180). Без этого «назад»
        # не правил команду в программе, в отличие от «вперёд».
        if (intent == "back" and not cmd.playback
                and s.cautious and self.world.danger_zones):
            req = nlu.extract_distance(raw)
            if req:
                back_h  = (s.heading + 180.0) % 360.0
                clipped = self._clip_dist_cautious(float(req), back_h)
                if clipped < 1.0:
                    await self.push_message(
                        f"⚠ Препятствие сзади — back({req}) "
                        f"отменена, ехать нельзя.", "warning")
                    return
                if clipped < req - 0.5:
                    adj = int(clipped)
                    await self.push_message(
                        f"⚠ Препятствие — back({req}) сокращена "
                        f"до back({adj}).", "info")
                    cmd.raw = re.sub(r'\d+', str(adj), cmd.raw, count=1)
                    cmd.code = f"back({adj})"
                    cmd.label = f"Назад {adj} см"
                    raw = cmd.raw   # обновим локальную переменную

        # ── Кодогенерация ПЕРЕД выполнением ────────────────────────────
        # Чтобы оптимизации в _python_call_lines_for_cmd (например, для goto)
        # видели позицию РОБОТА ДО команды, а не после её исполнения.
        # Если бы мы делали кодоген после run, для goto(50,50) состояние
        # robot_state уже было бы (50,50) → распознавалось бы как «уже на месте».
        if not cmd.playback:
            python_desc, python_code = self._python_code_for_cmd(cmd)
        else:
            python_desc, python_code = None, None

        # ── Push кода в textarea СРАЗУ, до выполнения ──────────────────
        # Ребёнок видит как команда уже появилась в коде, и только потом
        # робот начинает её выполнять (полусекундный визуальный effect:
        # «вписано → поехал»). На playback и для skip_record команд кодоген
        # либо None, либо не пишется в текст программы — там пропускаем.
        if (python_code is not None and not cmd.playback
                and not getattr(cmd, "skip_record", False)):
            await self.push_code_append(python_code, python_desc)

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
            msg = self._obstacle_stop_message(direction="вперёд")
        elif intent == "backward_to_wall":
            spd = nlu.extract_speed(raw, c.move_speed)
            await self._run_backward_to_wall(spd)
            msg = self._obstacle_stop_message(direction="назад")
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
        elif intent == "set_turn_angle":
            ang = nlu.extract_default_turn_angle(raw, c.turn_angle)
            self.cfg.turn_angle = ang
            msg = f"Угол руля по умолчанию {ang}°."
        elif intent == "turn_around":
            n = nlu.norm(raw)
            direction = -1 if any(w in n for w in ("налево", "влево", "против")) else 1
            side = "влево" if direction == -1 else "вправо"
            target_180 = (s.heading + 180.0) % 360.0
            if self.cfg.wall_turn_strategy == "manual":
                if not await self._kturn_clearance_ok(target_180):
                    msg = "Разворот невозможен (мало места)."
                    ok = False
                else:
                    await self._k_turn_n(direction, steps=1)
                    msg = f"Разворот {side} завершен."
            elif self.cfg.wall_turn_strategy == "multi_step":
                await self._multi_step_kturn(target_180)
                msg = f"Разворот {side} завершен."
            else:
                backup = await self._ensure_kturn_clearance(target_180)
                await self._k_turn_n(direction, steps=1)
                await self._compensate_kturn_backup(backup, total_diff_deg=180.0)
                msg = f"Разворот {side} завершен."
        elif intent == "turn_around_place":
            n = nlu.norm(raw)
            direction = -1 if any(w in n for w in ("налево", "влево", "против")) else 1
            steps = nlu.extract_kturn_steps(raw)
            target_180 = (s.heading + 180.0) % 360.0
            if self.cfg.wall_turn_strategy == "manual":
                if not await self._kturn_clearance_ok(target_180):
                    msg = "Разворот невозможен (мало места)."
                    ok = False
                else:
                    await self._k_turn_n(direction, steps)
                    msg = "Разворот на месте завершен."
            elif self.cfg.wall_turn_strategy == "multi_step":
                await self._multi_step_kturn(target_180)
                msg = "Разворот на месте завершен."
            else:
                backup = await self._ensure_kturn_clearance(target_180)
                await self._k_turn_n(direction, steps)
                await self._compensate_kturn_backup(backup, total_diff_deg=180.0)
                msg = "Разворот на месте завершен."
        elif intent == "circle":
            # По умолчанию CCW (против часовой, мат. направление 0→2π).
            # Только если явно сказано «направо/вправо/по часовой» — CW.
            n = nlu.norm(raw)
            direction = +1 if any(w in n for w in ("направо", "вправо", "по часовой")) else -1
            await self._run_arc(360.0, direction)
            msg = "Круг завершен (" + ("по часовой" if direction > 0 else "против часовой") + ")."
        elif intent == "arc":
            n = nlu.norm(raw)
            direction = +1 if any(w in n for w in ("направо", "вправо", "по часовой")) else -1
            ang = nlu.extract_arc_angle(raw)
            await self._run_arc(float(ang), direction)
            side = "по часовой" if direction > 0 else "против часовой"
            msg = f"Дуга {ang}° {side} завершена."
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
            # Во время миссии «в точку» запрещено: обучающийся должен
            # программировать маршрут через forward/turn, а не телепортировать
            # робота одной командой. Играть с командой можно только в
            # свободном режиме.
            if self._mission is not None and not cmd.playback:
                msg, ok = ("Команда «в точку» отключена во время миссии — "
                           "составьте маршрут из forward/поворотов в коде.",
                           False)
            else:
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
            # UI-команда из «⛯ Режим зон» (ЛКМ): pre-flight установка
            # красной зоны мышью. НЕ записывается в Python-код программы
            # (red zones — обстановка, не команды). Аналог remove_danger_zone
            # для ПКМ. Сразу же кладётся в world + DB с display_no.
            xy = nlu.extract_coordinates(raw)
            r  = nlu.extract_radius(raw) or float(self.cfg.danger_zone_radius)
            if xy is None:
                msg, ok = "mark_danger: координаты не указаны.", False
            else:
                tx, ty = xy
                no = self._next_zone_display_no("danger")
                zone = self.world.add_danger_zone(
                    tx, ty, radius=r, label="Зона опасности",
                    kind="danger", display_no=no)
                if db:
                    dz = DangerZone(user_id=self.user_id, label=zone.label,
                                    x=zone.x, y=zone.y, radius=zone.radius,
                                    kind="danger", display_no=no)
                    db.add(dz); db.flush()
                    zone.db_id = dz.id
                    db.commit()
                await self.push_world()
                # Не записываем в Python-программу: красные зоны — обстановка.
                cmd.skip_record = True
                msg = f"⚠ Опасная #{no} в ({tx:.0f}, {ty:.0f}), r={r:.0f}."
        elif intent == "pause":
            secs = nlu.extract_pause_seconds(raw)
            await self._run_pause(secs)
            msg = f"Пауза {secs:g} с завершена."
        elif intent == "resume":
            # Возобновляет программу, паузнутую _pause_for_manual_handoff.
            # Если ждать не на чем — мягкая ошибка.
            if not getattr(s, "awaiting_user", False):
                msg, ok = "Сейчас программа не на паузе — нечего возобновлять.", False
            else:
                self._resume_event.set()
                msg = "▶ Возобновляю программу."
        elif intent == "set_algorithm_zone":
            xy = nlu.extract_coordinates(raw)
            r = nlu.extract_radius(raw)
            if xy is None:
                # Без координат — ставим зону в текущей позиции робота
                # (без goto). Это режим UI-кнопки «🟡 Установить зону».
                tx, ty = s.x, s.y
                await self._run_place_attention_here(r, db)
                msg = (f"Зона внимания установлена в текущей позиции "
                       f"({tx:.0f}, {ty:.0f})"
                       + (f", радиус {r:.0f}." if r is not None else "."))
            else:
                tx, ty = xy
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
            # Голосовой пресет «инспектор» = снять все зон-препятствия
            # (остаются только стены). На миссии с опасными зонами —
            # запрещено (danger-зоны зафиксированы).
            if self._mission is not None and self._mission.danger_zones:
                msg, ok = ("Во время миссии с опасными зонами препятствия "
                           "переключать нельзя. Завершите или остановите "
                           "миссию.", False)
            else:
                s.mode = "normal"
                self._apply_obstacles(set())
                s.zone_mode = False     # mutex с «⛯ Зоны»
                self._obstacles_before_zone = None  # явный выбор стирает стэш
                await self.broadcast({"type": "obstacles_line",
                                      "obstacles": sorted(s.obstacles)})
                msg = self._obstacles_human()
        elif intent == "mode_cautious":
            # Голосовой пресет «опасно» = реагировать на все типы зон.
            if self._mission is not None and self._mission.danger_zones:
                msg, ok = ("Во время миссии с опасными зонами препятствия "
                           "переключать нельзя. Завершите или остановите "
                           "миссию.", False)
            else:
                self._apply_obstacles({"danger", "attention"})
                s.zone_mode = False     # mutex с «⛯ Зоны»
                self._obstacles_before_zone = None  # явный выбор стирает стэш
                await self.broadcast({"type": "obstacles_line",
                                      "obstacles": sorted(s.obstacles)})
                msg = self._obstacles_human()
        elif intent == "path_show":
            await self.broadcast({"type": "path_visible", "visible": True})
            msg = "Путь показан."
        elif intent == "path_hide":
            await self.broadcast({"type": "path_visible", "visible": False})
            msg = "Путь скрыт."
        elif intent == "reset":
            # Во время активной миссии ↺ Поле блокирован — обучающийся
            # не должен случайно сбрасывать обстановку миссии. Исключение —
            # playback (▶ Run): он внутри программы делает свой reset.
            if self._mission is not None and not cmd.playback:
                msg, ok = ("Во время миссии ↺ Поле недоступно. Заверши или "
                           "останови миссию, чтобы сбросить поле.", False)
            else:
                # В ⛯ «Режим зон» ↺ Поле = «начать расстановку с нуля»:
                # запоминаем флаг, чтобы потом синхронизировать DANGER_ZONES
                # в коде с очищенным world.
                was_zone_mode = bool(s.zone_mode)
                # При playback (replay программы) сохраняем режим
                # «осторожно» — иначе ▶ Запуск кода каждый раз гасит его
                # и action-кнопки моргают между жёлтым и синим.
                await self._run_reset(db, keep_mode=bool(cmd.playback))
                if was_zone_mode:
                    # Зоны выпилены — обновляем блок DANGER_ZONES в textarea.
                    await self.broadcast({
                        "type": "obstacle_block",
                        "block": self._obstacle_block(),
                    })
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
            await self.robot.set_rgb(LIGHT_INDEX, LIGHT_DEFAULT_COLOR, LIGHT_DELAY_SEC)
            msg = "Свет включен."
        elif intent == "light_off":
            s.light_color = (0, 0, 0)
            await self.robot.set_rgb(LIGHT_INDEX, (0, 0, 0), LIGHT_DELAY_SEC)
            msg = "Свет выключен."
        elif intent == "light_color":
            color = nlu.extract_color(raw) or LIGHT_DEFAULT_COLOR
            s.light_color = color
            await self.robot.set_rgb(LIGHT_INDEX, color, LIGHT_DELAY_SEC)
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
            # Код уже был отправлен ДО выполнения через push_code_append —
            # здесь только journal-сообщение о результате, без code.
            await self.push_message(msg, "success" if ok else "warning")

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

    # Day 2: парсер textarea (_parse_program_text + _parse_dsl_line + ~500 строк
    # регэкспов и body-extractors) удалён. exec() запускает код пользователя
    # напрямую через robot_api.run_user_python — никакой обратной трансляции
    # текста в RobotCmd больше не нужно.

    async def load_published_code(self, code: str, source_label: str = "опубликованный маршрут"):
        """Загружает чужой опубликованный Python-код в textarea клиента.
        Day 2: парсинг текста удалён — клиент видит код 1-в-1 и сам нажимает ▶.
        `self._program` уже не используется как источник истины."""
        await self._do_stop()
        self._program = []  # legacy-список больше не несёт смысла
        self._last_python_code = code
        await self.broadcast({
            "type":  "program",
            "lines": [],
            "text":  code,
        })
        await self.push_message(
            f"📥 Загружено: {source_label}. Можно отредактировать и нажать ▶.",
            "success")
        return True

    # ── Точка входа для команды ──────────────────────────────────────────────

    # Команды, разрешённые при включённом «⛯ Режим зон». Всё остальное
    # отвергается с сообщением «включён режим установки зон». В набор
    # входят: установка/удаление красной зоны мышью, выход в другой режим,
    # запросы статуса, сброс поля и аварийный stop.
    _ZONE_MODE_ALLOWED_INTENTS = frozenset({
        "mark_danger", "remove_danger_zone",
        "mode_inspector", "mode_cautious",
        "report_pos", "report_status",
        "reset", "stop",
    })

    async def handle_command(self, raw_text: str, db: Session = None):
        intent, conf = nlu.predict(raw_text)
        log.info("[user %d] CMD %r → intent=%s conf=%.2f", self.user_id, raw_text, intent, conf)

        # Гарантированный stop игнорирует любые блокировки.
        if intent == "stop":
            await self._do_stop()
            await self.push_state()
            await self.push_queue()
            cmd = self._build_cmd(intent, raw_text)
            description, code = self._python_code_for_cmd(cmd) if cmd else (None, None)
            await self.push_message("Стоп!", "success", code=code, description=description)
            return

        # Блок: пока включён «⛯ Режим зон», робот не выполняет команды
        # движения / зон-внимания / лампы / итд. Разрешены только
        # установка-снятие красных зон мышью + выход в другой режим.
        if self.robot_state.zone_mode and intent not in self._ZONE_MODE_ALLOWED_INTENTS:
            await self.push_message(
                "⛯ Обстановка ВКЛ — команды робота отключены.",
                "warning")
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

        # Голосовые пресеты препятствий (инспектор/опасно) — мгновенные,
        # в обход очереди манёвров: сразу меняют набор s.obstacles и
        # переписывают строку OBSTACLES в коде, не дожидаясь окончания
        # текущего манёвра. _dispatch для этих интентов только правит
        # состояние + шлёт obstacles_line — мотор не затрагивается.
        if intent in ("mode_inspector", "mode_cautious"):
            cmd = self._build_cmd(intent, raw_text)
            if cmd is not None:
                db_local = SessionLocal()
                try:
                    await self._dispatch(cmd, db_local)
                finally:
                    db_local.close()
            return

        cmd = self._build_cmd(intent, raw_text)
        if cmd is None:
            # Чаще всего _build_cmd возвращает None когда intent распознан,
            # но не хватает обязательного аргумента (координат, угла, …).
            # Подсказываем пользователю в зависимости от типа намерения.
            if not intent:
                msg = "Команда не распознана."
            elif intent in ("goto", "set_algorithm_zone", "remove_zone",
                            "mark_danger"):
                msg = (f"Не удалось извлечь координаты для «{intent}». "
                       f"Используй формат: <X Y> через пробел, например "
                       f"«-50 -50» или «100 50».")
            elif intent in ("face_to", "set_course"):
                msg = (f"Не удалось извлечь угол для «{intent}». "
                       f"Используй формат: «поверни на 90» или «курс 45».")
            else:
                msg = f"Намерение «{intent}» не поддерживается."
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

            # Защитный стоп в режиме «Опасно»: если ЛЮБОЙ угол корпуса вошёл
            # в защитный буфер вокруг зоны — мгновенная остановка ДО касания.
            # Применяется к движению на ходу (forward/goto/дуги/спирали/
            # bypass) и ко ВСЕМ типам зон (опасные + внимания) — в «Опасно»
            # зона ведёт себя как стена. Запас = «Толщина стены» из настроек.
            #
            # ИСКЛЮЧЕНИЕ — K-turn (s.turning_in_place): разворот на месте
            # крутится почти не смещаясь и геометрически возвращается в
            # исходную точку. Обрыв его буфером зоны оставлял робота с
            # недокрученным курсом → дальше он ехал «не туда». Поэтому
            # K-turn доводится до конца; стоп у зон — только на ходу.
            SAFETY_BUFFER_CM = c.wall_thickness_cm
            if (s.cautious and not hit_wall and not s.turning_in_place
                    and self.world.danger_zones):
                hit_zone = None
                for cx, cy in self._robot_corners():
                    for z in self.world.danger_zones:
                        if not self._zone_is_obstacle(z):
                            continue
                        r_buf = z.radius + SAFETY_BUFFER_CM
                        if (cx - z.x) ** 2 + (cy - z.y) ** 2 < r_buf * r_buf:
                            hit_zone = z
                            break
                    if hit_zone:
                        break
                if hit_zone:
                    s.speed     = 0
                    s.dist_left = 0
                    try: await self.robot.move(0)
                    except Exception: pass
                    kind_lbl = self._zone_label_human(hit_zone.kind).lower()
                    no = hit_zone.display_no or 0
                    tag = f" #{no}" if no else ""
                    await self.push_message(
                        f"⚠ Стоп — близко к {kind_lbl} зоне{tag}.",
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
