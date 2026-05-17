"""
main.py — FastAPI приложение VEGAREX (мульти-пользовательский режим).

Архитектура:
  1. Аутентификация (cookie-сессии + bcrypt) — auth.py
  2. На каждого залогиненного пользователя создается UserSession — session.py
     (свой robot_state, world, драйвер робота, программа, очередь, фоновые задачи)
  3. WebSocket /ws привязывается к UserSession этого пользователя
  4. Настройки робота, размер поля, программа — у каждого свои (DB)
"""
import asyncio
import json
import logging
import re
import secrets
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import Depends, FastAPI, Form, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware

import config
from auth import (hash_password, login_user, logout_user, require_admin,
                  require_user, verify_password)
from database import Base, SessionLocal, engine, get_db
from models import (AppSettings, CommandLog, DangerZone, Mission, MissionRun,
                    PathPoint, ProgramCommand, PublishedRoute, RobotSession,
                    SavedRoute, User, UserSettings)
from session import (UserCfg, _ensure_user_settings, get_or_create_session,
                     get_session, stop_all_sessions)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
# Lifespan
# ═══════════════════════════════════════════════════════════════════════════════

@asynccontextmanager
async def lifespan(app: FastAPI):
    # ВАЖНО: дроп legacy-таблиц должен быть ДО create_all, чтобы новая
    # схема Mission/MissionRun создалась с нуля (create_all не альтерит
    # существующие таблицы).
    _drop_legacy_missions()
    Base.metadata.create_all(bind=engine)
    _ensure_schema_migrations()
    _ensure_app_settings()
    _ensure_admin_exists()
    yield
    # При остановке — корректно гасим все сессии
    await stop_all_sessions()


def _drop_legacy_missions():
    """Одноразовая миграция: схема Mission/MissionResult полностью
    переделана под систему сгенерированных миссий с траекторией, зонами
    и звёздами. Проще всего дропнуть старые таблицы — пользовательские
    данные в них не хранились (только три демо-миссии)."""
    from sqlalchemy import inspect, text
    insp = inspect(engine)
    existing = set(insp.get_table_names())
    legacy_signature_columns = {
        # старая Mission имела target_x/target_y/time_limit/difficulty,
        # новая — owner_id/level/waypoints/...
        "missions": "target_x",
    }
    for table, marker in legacy_signature_columns.items():
        if table not in existing:
            continue
        cols = {c["name"] for c in insp.get_columns(table)}
        if marker in cols:
            log.info("Dropping legacy %s table (incompatible schema)", table)
            with engine.begin() as conn:
                # mission_results имеет FK на missions — дропаем сначала зависимый
                conn.execute(text("DROP TABLE IF EXISTS mission_results"))
                conn.execute(text(f"DROP TABLE IF EXISTS {table}"))


def _ensure_admin_exists():
    """Если в системе есть пользователи, но ни один не админ —
    промоутим самого старого. Это для совместимости со старыми БД,
    где не было колонки is_admin."""
    db = SessionLocal()
    try:
        any_admin = db.query(User).filter(User.is_admin == True).first()
        if any_admin is not None:
            return
        oldest = db.query(User).order_by(User.id).first()
        if oldest is not None:
            oldest.is_admin = True
            db.commit()
            log.info("Promoted oldest user '%s' (id=%d) to admin", oldest.username, oldest.id)
    finally:
        db.close()


def _ensure_schema_migrations():
    """Легкие миграции: добавляем новые колонки в существующие таблицы,
    чтобы при апгрейде не нужно было вайпать БД."""
    from sqlalchemy import inspect, text
    insp = inspect(engine)
    existing_tables = set(insp.get_table_names())

    additions = {
        "users": {
            "is_admin":                 "BOOLEAN NOT NULL DEFAULT FALSE",
            "temp_password":            "VARCHAR(255)",
            "password_changed_by_user": "BOOLEAN NOT NULL DEFAULT TRUE",
        },
        "danger_zones": {
            "kind":       "VARCHAR(20) NOT NULL DEFAULT 'danger'",
            "display_no": "INTEGER NOT NULL DEFAULT 0",
        },
        "robot_sessions": {
            "user_id": "INTEGER",
        },
        "user_settings": {
            "battery_minutes":         "INTEGER NOT NULL DEFAULT 60",
            "battery_pct":             "REAL NOT NULL DEFAULT 100.0",
            "path_cell_size_cm":       "INTEGER NOT NULL DEFAULT 10",
            "autopilot_algo":          "VARCHAR(20) NOT NULL DEFAULT 'polyline'",
            "cautious_follow_algo":    "VARCHAR(20) NOT NULL DEFAULT 'pure_pursuit'",
            "cautious_slow_curves":    "BOOLEAN NOT NULL DEFAULT TRUE",
            "wall_turn_strategy":      "VARCHAR(20) NOT NULL DEFAULT 'backoff'",
        },
        "missions": {
            "path": "TEXT NOT NULL DEFAULT '[]'",
            "obstacles": "TEXT NOT NULL DEFAULT '[]'",
        },
        "mission_runs": {
            "duration_sec":      "REAL NOT NULL DEFAULT 0.0",
            "algo_duration_sec": "REAL NOT NULL DEFAULT 0.0",
        },
    }
    for table, columns in additions.items():
        if table not in existing_tables:
            continue
        existing_cols = {c["name"] for c in insp.get_columns(table)}
        with engine.begin() as conn:
            for col_name, col_def in columns.items():
                if col_name not in existing_cols:
                    log.info("Migration: ALTER TABLE %s ADD COLUMN %s", table, col_name)
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_def}"))


def _ensure_app_settings():
    """Гарантируем, что в app_settings лежит singleton-строка."""
    from models import AppSettings
    db = SessionLocal()
    try:
        row = db.query(AppSettings).filter(AppSettings.id == 1).first()
        if row is None:
            db.add(AppSettings(id=1, registration_open=True))
            db.commit()
    finally:
        db.close()


# _seed_missions удалён вместе со старой схемой — миссии теперь создают
# пользователи через генератор на странице /tasks.


# ═══════════════════════════════════════════════════════════════════════════════
# FastAPI app + middleware
# ═══════════════════════════════════════════════════════════════════════════════

app = FastAPI(title="VEGAREX", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")
templates.env.globals["robot_version"] = config.ROBOT_VERSION


_PUBLIC_PATHS = ("/login", "/register", "/static", "/favicon.ico")


class AuthRequiredMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if any(path == p or path.startswith(p + "/") for p in _PUBLIC_PATHS):
            return await call_next(request)
        user_id = request.session.get("user_id") if hasattr(request, "session") else None
        if not user_id:
            return RedirectResponse("/login", status_code=303)
        return await call_next(request)


# Порядок: SessionMiddleware (внешний) → AuthRequiredMiddleware (внутренний)
app.add_middleware(AuthRequiredMiddleware)
app.add_middleware(
    SessionMiddleware,
    secret_key=config.SESSION_SECRET,
    session_cookie="vegarex_session",
    https_only=False,
    same_site="lax",
)


# ═══════════════════════════════════════════════════════════════════════════════
# Аутентификация
# ═══════════════════════════════════════════════════════════════════════════════

_USERNAME_RE = re.compile(r"^[A-Za-z0-9_\-\.]{3,64}$")


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if request.session.get("user_id"):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "login.html", {"current_user": None})


@app.post("/login", response_class=HTMLResponse)
async def login_submit(request: Request,
                       username: str = Form(...),
                       password: str = Form(...),
                       db: Session = Depends(get_db)):
    user = db.query(User).filter(User.username == username.strip()).first()
    if not user or not verify_password(password, user.password_hash):
        return templates.TemplateResponse(request, "login.html", {
            "current_user": None,
            "error": "Неверный логин или пароль.",
            "username": username,
        }, status_code=400)
    login_user(request, user)
    return RedirectResponse("/", status_code=303)


def _registration_is_open(db: Session) -> bool:
    row = db.query(AppSettings).filter(AppSettings.id == 1).first()
    return bool(row.registration_open) if row else True


@app.get("/register", response_class=HTMLResponse)
async def register_page(request: Request, db: Session = Depends(get_db)):
    if request.session.get("user_id"):
        return RedirectResponse("/", status_code=303)
    # Первый юзер всегда может зарегистрироваться (станет админом).
    has_users = db.query(User).count() > 0
    if has_users and not _registration_is_open(db):
        return templates.TemplateResponse(request, "register.html", {
            "current_user": None,
            "error": "Регистрация новых пользователей закрыта администратором.",
            "registration_disabled": True,
        }, status_code=403)
    return templates.TemplateResponse(request, "register.html", {"current_user": None})


@app.post("/register", response_class=HTMLResponse)
async def register_submit(request: Request,
                          username: str = Form(...),
                          password: str = Form(...),
                          password2: str = Form(...),
                          db: Session = Depends(get_db)):
    username = username.strip()
    has_users = db.query(User).count() > 0
    # Если регистрация закрыта и уже есть пользователи — отказ
    if has_users and not _registration_is_open(db):
        return templates.TemplateResponse(request, "register.html", {
            "current_user": None,
            "error": "Регистрация новых пользователей закрыта администратором.",
            "registration_disabled": True,
        }, status_code=403)
    error = None
    if not _USERNAME_RE.match(username):
        error = "Логин: 3–64 символа, только латиница, цифры, _ - ."
    elif len(password) < 4:
        error = "Пароль должен быть не короче 4 символов."
    elif password != password2:
        error = "Пароли не совпадают."
    elif db.query(User).filter(User.username == username).first():
        error = "Такой логин уже занят."
    if error:
        return templates.TemplateResponse(request, "register.html", {
            "current_user": None,
            "error": error,
            "username": username,
        }, status_code=400)
    # Первый зарегистрированный = администратор
    is_first = not has_users
    user = User(
        username=username,
        password_hash=hash_password(password),
        is_admin=is_first,
        password_changed_by_user=True,
        temp_password=None,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    login_user(request, user)
    return RedirectResponse("/", status_code=303)


@app.get("/logout")
async def logout(request: Request):
    user_id = request.session.get("user_id")
    logout_user(request)
    # Сессию симулятора гасим — чтобы освободить ресурсы и применить новые настройки при следующем входе
    if user_id:
        sess = get_session(user_id)
        if sess is not None:
            try:
                await sess.stop()
            except Exception:
                pass
            from session import SESSIONS
            SESSIONS.pop(user_id, None)
    return RedirectResponse("/login", status_code=303)


# ═══════════════════════════════════════════════════════════════════════════════
# Смена собственного пароля (любой залогиненный)
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/account/password", response_class=HTMLResponse)
async def change_password_page(request: Request,
                                current_user: User = Depends(require_user)):
    return templates.TemplateResponse(request, "change_password.html",
                                       {"current_user": current_user})


@app.post("/account/password", response_class=HTMLResponse)
async def change_password_submit(request: Request,
                                  current_password: str = Form(...),
                                  new_password:     str = Form(...),
                                  new_password2:    str = Form(...),
                                  current_user: User = Depends(require_user),
                                  db: Session = Depends(get_db)):
    error = None
    if not verify_password(current_password, current_user.password_hash):
        error = "Текущий пароль неверный."
    elif len(new_password) < 4:
        error = "Новый пароль должен быть не короче 4 символов."
    elif new_password != new_password2:
        error = "Новые пароли не совпадают."
    if error:
        return templates.TemplateResponse(request, "change_password.html", {
            "current_user": current_user,
            "error": error,
        }, status_code=400)
    current_user.password_hash            = hash_password(new_password)
    current_user.temp_password            = None
    current_user.password_changed_by_user = True
    db.commit()
    return templates.TemplateResponse(request, "change_password.html", {
        "current_user": current_user,
        "success": "Пароль изменен.",
    })


# ═══════════════════════════════════════════════════════════════════════════════
# Админ-панель
# ═══════════════════════════════════════════════════════════════════════════════

def _generate_password(length: int = 8) -> str:
    """Удобочитаемый временный пароль (без неоднозначных 0/O/1/l)."""
    alphabet = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(length))


@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request,
                      admin: User = Depends(require_admin),
                      db: Session = Depends(get_db)):
    users = db.query(User).order_by(User.id).all()
    settings = db.query(AppSettings).filter(AppSettings.id == 1).first()
    from session import SESSIONS
    return templates.TemplateResponse(request, "admin.html", {
        "current_user":      admin,
        "users":             users,
        "registration_open": bool(settings.registration_open) if settings else True,
        "active_session_ids": set(SESSIONS.keys()),
    })


@app.get("/admin/print", response_class=HTMLResponse)
async def admin_print(request: Request,
                       admin: User = Depends(require_admin),
                       db: Session = Depends(get_db)):
    users = db.query(User).order_by(User.username).all()
    return templates.TemplateResponse(request, "admin_print.html", {
        "current_user": admin,
        "users":        users,
    })


@app.post("/admin/registration_toggle")
async def admin_registration_toggle(admin: User = Depends(require_admin),
                                     db: Session = Depends(get_db)):
    settings = db.query(AppSettings).filter(AppSettings.id == 1).first()
    if settings is None:
        settings = AppSettings(id=1, registration_open=False)
        db.add(settings)
    else:
        settings.registration_open = not settings.registration_open
    db.commit()
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/users/create")
async def admin_create_user(username: str = Form(...),
                             password: str = Form(""),
                             is_admin: str = Form("0"),
                             admin: User = Depends(require_admin),
                             db: Session = Depends(get_db)):
    username = username.strip()
    if not _USERNAME_RE.match(username):
        return RedirectResponse("/admin?err=bad_username", status_code=303)
    if db.query(User).filter(User.username == username).first():
        return RedirectResponse("/admin?err=username_taken", status_code=303)
    if not password:
        password = _generate_password()
    if len(password) < 4:
        return RedirectResponse("/admin?err=short_password", status_code=303)
    user = User(
        username=username,
        password_hash=hash_password(password),
        is_admin=(is_admin == "1"),
        temp_password=password,            # сохраняем чтобы админ видел/печатал
        password_changed_by_user=False,
    )
    db.add(user)
    db.commit()
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/users/{user_id}/reset_password")
async def admin_reset_password(user_id: int,
                                new_password: str = Form(""),
                                admin: User = Depends(require_admin),
                                db: Session = Depends(get_db)):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        return RedirectResponse("/admin?err=no_user", status_code=303)
    pwd = new_password.strip() or _generate_password()
    if len(pwd) < 4:
        return RedirectResponse("/admin?err=short_password", status_code=303)
    user.password_hash            = hash_password(pwd)
    user.temp_password            = pwd
    user.password_changed_by_user = False
    db.commit()
    # Если у юзера была активная сессия — гасим (логиниться придется заново)
    from session import SESSIONS
    sess = SESSIONS.pop(user_id, None)
    if sess is not None:
        try:
            await sess.stop()
        except Exception:
            pass
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/users/{user_id}/toggle_admin")
async def admin_toggle_admin(user_id: int,
                              admin: User = Depends(require_admin),
                              db: Session = Depends(get_db)):
    if user_id == admin.id:
        return RedirectResponse("/admin?err=cant_demote_self", status_code=303)
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        return RedirectResponse("/admin?err=no_user", status_code=303)
    user.is_admin = not user.is_admin
    db.commit()
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/users/{user_id}/delete")
async def admin_delete_user(user_id: int,
                             admin: User = Depends(require_admin),
                             db: Session = Depends(get_db)):
    if user_id == admin.id:
        return RedirectResponse("/admin?err=cant_delete_self", status_code=303)
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        return RedirectResponse("/admin?err=no_user", status_code=303)
    # Гасим активную сессию
    from session import SESSIONS
    sess = SESSIONS.pop(user_id, None)
    if sess is not None:
        try:
            await sess.stop()
        except Exception:
            pass
    db.delete(user)        # cascade удалит UserSettings, ProgramCommand, DangerZone (FK ondelete=CASCADE)
    db.commit()
    return RedirectResponse("/admin", status_code=303)


# ═══════════════════════════════════════════════════════════════════════════════
# Страницы
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/", response_class=HTMLResponse)
async def index(request: Request,
                current_user: User = Depends(require_user),
                db: Session = Depends(get_db)):
    sess = await get_or_create_session(current_user.id, db)
    return templates.TemplateResponse(request, "index.html", {
        "simulated":    sess.cfg.simulation_mode,
        "robot_host":   sess.cfg.rex_host,
        "current_user": current_user,
    })


@app.get("/help", response_class=HTMLResponse)
async def help_index(request: Request,
                     current_user: User = Depends(require_user)):
    """Хаб-страница «Справка»: список разделов с кратким описанием
    и кнопкой перехода в каждый."""
    return templates.TemplateResponse(request, "help.html", {
        "current_user": current_user,
    })


@app.get("/training", response_class=HTMLResponse)
async def training(request: Request,
                   current_user: User = Depends(require_user)):
    return templates.TemplateResponse(request, "training.html", {
        "current_user": current_user,
    })


@app.get("/system", response_class=HTMLResponse)
async def system_commands(request: Request,
                          current_user: User = Depends(require_user)):
    return templates.TemplateResponse(request, "system.html", {
        "current_user": current_user,
    })


@app.get("/maneuvers", response_class=HTMLResponse)
async def maneuvers_docs(request: Request,
                         current_user: User = Depends(require_user)):
    return templates.TemplateResponse(request, "maneuvers.html", {
        "current_user": current_user,
    })


@app.get("/about", response_class=HTMLResponse)
async def about_page(request: Request,
                     current_user: User = Depends(require_user)):
    """Страница «О программе» — описание проекта, целей, технологий,
    автор. Версия подтягивается из шаблона (robot_version)."""
    return templates.TemplateResponse(request, "about.html", {
        "current_user": current_user,
    })


@app.get("/tasks", response_class=HTMLResponse)
async def tasks_page(request: Request, db: Session = Depends(get_db),
                     current_user: User = Depends(require_user)):
    """Хаб миссий: 2 таба — «Сгенерировать» и «Каталог».
    Каталог содержит МОИ миссии + ОПУБЛИКОВАННЫЕ другими в одном
    списке с фильтрами (Все/Мои/Общие) и поиском по ID/названию.
    """
    from sqlalchemy import or_, func
    rows = (db.query(Mission)
              .filter(or_(
                  Mission.owner_id == current_user.id,
                  Mission.published == True,
              ))
              .order_by(Mission.created_at.desc()).all())
    # Лучшее прохождение пользователя по каждой миссии: max(stars) среди
    # ЗАВЕРШЁННЫХ MissionRun (completed_at IS NOT NULL). Активные/брошенные
    # прохождения сюда не входят — иначе «Набрано: 0⭐» появится в карточке
    # сразу после ▶ Пройти, ещё до окончания миссии.
    best_runs = (db.query(MissionRun.mission_id,
                          func.max(MissionRun.stars))
                   .filter(MissionRun.user_id == current_user.id)
                   .filter(MissionRun.completed_at.isnot(None))
                   .group_by(MissionRun.mission_id).all())
    best_stars = {mid: stars for mid, stars in best_runs}
    return templates.TemplateResponse(request, "tasks.html", {
        "missions":     rows,
        "best_stars":   best_stars,
        "current_user": current_user,
    })


def _build_user_geom(db: Session, current_user: User):
    """Геометрия мира для генератора — из user-settings либо дефолт."""
    from mission_generator import WorldGeom
    settings = (db.query(UserSettings)
                  .filter(UserSettings.user_id == current_user.id).first())
    if settings:
        return WorldGeom(
            world_w_cm=float(settings.world_w_cm),
            world_h_cm=float(settings.world_h_cm),
            wall_thick_cm=float(settings.wall_thickness_cm),
            robot_w_cm=float(settings.robot_width_cm),
            robot_l_cm=float(settings.robot_length_cm),
            safety_margin_cm=float(settings.wall_thickness_cm),
            start_x=float(settings.start_x_cm),
            start_y=float(settings.start_y_cm),
            start_heading=float(settings.start_heading_deg),
            danger_zone_radius_cm=float(settings.danger_zone_radius),
        )
    return WorldGeom()


@app.post("/missions/generate")
async def missions_generate(level: int = Form(...),
                            db: Session = Depends(get_db),
                            current_user: User = Depends(require_user)):
    """Сгенерировать миссию (preview), БЕЗ сохранения в БД.
    Сохранение делается отдельным запросом /missions/save — пользователь
    сам решает, нравится ему результат или генерировать ещё. Это
    предотвращает накопление мусорных миссий в «Мои миссии»."""
    from mission_generator import generate_mission
    if level not in (1, 2, 3, 4, 5):
        return JSONResponse({"error": "level must be 1..5"}, status_code=400)
    geom = _build_user_geom(db, current_user)
    data = generate_mission(level=level, geom=geom)
    return JSONResponse({
        "id":               None,                 # ещё не сохранено
        "title":            data["title"],
        "description":      data["description"],
        "level":            data["level"],
        "waypoints":        json.loads(data["waypoints"]),
        "path":             json.loads(data.get("path", "[]")),
        "danger_zones":     json.loads(data["danger_zones"]),
        "actions_required": json.loads(data["actions_required"]),
        # raw-данные для последующего save (ходят в /missions/save обратно)
        "_save_payload": {
            "title":            data["title"],
            "description":      data["description"],
            "level":            data["level"],
            "waypoints":        data["waypoints"],
            "path":             data.get("path", "[]"),
            "danger_zones":     data["danger_zones"],
            "actions_required": data["actions_required"],
            "reference_voice":  data["reference_voice"],
            "reference_code":   data["reference_code"],
            "safety_margin_cm": data["safety_margin_cm"],
        },
    })


def _smallest_unused_mission_id(db: Session, max_attempts: int = 100) -> int:
    """Минимальный целочисленный ID, не занятый ни одной миссией.
    Если в БД [3, 5, 7] — вернёт 1. Если [1, 2, 3] — вернёт 4.
    Это намеренное «переиспользование номеров»: пользователь много
    генерирует и удаляет миссии, и ID не должен расти бесконечно."""
    existing = {row[0] for row in db.query(Mission.id).all()}
    n = 1
    while n in existing and n < max_attempts + len(existing):
        n += 1
    return n


@app.post("/missions/save")
async def missions_save(request: Request,
                        db: Session = Depends(get_db),
                        current_user: User = Depends(require_user)):
    """Сохранить ранее сгенерированную миссию в БД. На вход — JSON
    с полями миссии (как _save_payload из /missions/generate).
    ID назначается как минимальный свободный (переиспользует
    освободившиеся номера после удалений)."""
    from sqlalchemy.exc import IntegrityError
    payload = await request.json()
    required = {"title", "description", "level", "waypoints",
                "danger_zones", "actions_required",
                "reference_voice", "reference_code", "safety_margin_cm"}
    missing = required - set(payload.keys())
    if missing:
        return JSONResponse({"error": f"missing fields: {sorted(missing)}"},
                            status_code=400)

    # До 3 попыток на случай гонки с другим юзером, который параллельно
    # сохраняет миссию и забрал «наш» минимальный свободный id.
    raw_title = (payload.get("title") or "").strip()
    for attempt in range(3):
        new_id = _smallest_unused_mission_id(db)
        # Если имя не задано — fallback на «Миссия #N» (где N — выбранный
        # минимальный свободный id). Это известно ДО commit.
        title = raw_title if raw_title else f"Миссия #{new_id}"
        m = Mission(
            id=new_id,
            owner_id=current_user.id,
            title=title,
            description=payload["description"],
            level=int(payload["level"]),
            waypoints=payload["waypoints"],
            path=payload.get("path", "[]"),
            danger_zones=payload["danger_zones"],
            actions_required=payload["actions_required"],
            reference_voice=payload["reference_voice"],
            reference_code=payload["reference_code"],
            safety_margin_cm=float(payload["safety_margin_cm"]),
            published=False,
        )
        try:
            db.add(m); db.commit(); db.refresh(m)
            return JSONResponse({"id": m.id, "ok": True, "title": m.title})
        except IntegrityError:
            db.rollback()
            continue
    return JSONResponse({"error": "id collision after retries"}, status_code=500)


@app.get("/missions/{mission_id}")
async def missions_get(mission_id: int,
                       db: Session = Depends(get_db),
                       current_user: User = Depends(require_user)):
    """Полные данные миссии для preview из каталога. Доступ —
    владелец, админ ИЛИ опубликована."""
    m = db.query(Mission).filter(Mission.id == mission_id).first()
    if m is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    is_owner = (m.owner_id == current_user.id)
    is_admin = bool(getattr(current_user, "is_admin", False))
    if not (is_owner or is_admin or m.published):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    return JSONResponse({
        "id":               m.id,
        "title":            m.title,
        "description":      m.description,
        "level":            m.level,
        "waypoints":        json.loads(m.waypoints),
        "path":             json.loads(m.path or "[]"),
        "danger_zones":     json.loads(m.danger_zones),
        "actions_required": json.loads(m.actions_required),
        "published":        m.published,
        "is_mine":          is_owner,
    })


@app.post("/missions/{mission_id}/publish")
async def missions_publish_toggle(mission_id: int,
                                  db: Session = Depends(get_db),
                                  current_user: User = Depends(require_user)):
    """Переключить флаг published.
    - Владелец может публиковать/снимать только свои миссии.
    - Админ может СНИМАТЬ с публикации любые миссии (модерация),
      но не публиковать чужие (это право автора)."""
    m = db.query(Mission).filter(Mission.id == mission_id).first()
    if m is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    is_owner = (m.owner_id == current_user.id)
    is_admin = bool(getattr(current_user, "is_admin", False))
    if not is_owner:
        # Не-владелец может только снимать чужую с публикации (и только админ)
        if not (is_admin and m.published):
            return JSONResponse({"error": "forbidden"}, status_code=403)
    m.published = not m.published
    db.commit()
    return JSONResponse({"id": m.id, "published": m.published})


@app.delete("/missions/{mission_id}")
async def missions_delete(mission_id: int,
                          db: Session = Depends(get_db),
                          current_user: User = Depends(require_user)):
    """Удалить миссию (вместе с прогонами по cascade).
    - Владелец может удалить свою.
    - Админ может удалить любую (для модерации мусорных миссий)."""
    m = db.query(Mission).filter(Mission.id == mission_id).first()
    if m is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    is_owner = (m.owner_id == current_user.id)
    is_admin = bool(getattr(current_user, "is_admin", False))
    if not (is_owner or is_admin):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    db.delete(m)
    db.commit()
    return JSONResponse({"ok": True})


@app.get("/missions/next_id")
async def missions_next_id(db: Session = Depends(get_db),
                           current_user: User = Depends(require_user)):
    """Следующий свободный номер миссии — для подсказки в диалоге
    сохранения кастомной миссии («будет «Кастомная #N»»). Это лишь
    предпросмотр: фактический id присваивается при сохранении."""
    return JSONResponse({"next_id": _smallest_unused_mission_id(db)})


@app.post("/missions/save_custom")
async def missions_save_custom(request: Request,
                               db: Session = Depends(get_db),
                               current_user: User = Depends(require_user)):
    """Сохранить текущее состояние свободного режима как «кастомную»
    миссию (level=0). Снимок: финальная позиция робота как waypoint,
    текущие опасные зоны (kind="danger") как pre-placed, текущие зоны
    внимания (kind="algorithm") как обязательные действия, программа
    из редактора как reference_code, голосовые фразы как reference_voice."""
    from sqlalchemy.exc import IntegrityError
    payload = await request.json()
    title = (payload.get("title") or "").strip()
    sess = get_session(current_user.id)
    if sess is None:
        return JSONResponse({"error": "no active session"}, status_code=400)

    # ── Снимок состояния симулятора ───────────────────────────────────
    import math as _math
    from mission_state import keypoints_from_segments

    start_x = round(float(sess.cfg.start_x_cm), 1)
    start_y = round(float(sess.cfg.start_y_cm), 1)

    # Полная траектория для визуализации — сэмпл path_history с шагом 5 см.
    # Даёт плавную дашед-линию в превью миссии (в т. ч. для кривых).
    raw_path = list(sess.world.path_history or [])
    path: list[list[float]] = []
    if raw_path:
        path.append([round(raw_path[0][0], 1), round(raw_path[0][1], 1)])
        last = raw_path[0]
        for x, y in raw_path[1:]:
            if _math.hypot(x - last[0], y - last[1]) >= 5.0:
                path.append([round(x, 1), round(y, 1)])
                last = (x, y)
        fx, fy = raw_path[-1]
        if _math.hypot(fx - path[-1][0], fy - path[-1][1]) > 0.1:
            path.append([round(fx, 1), round(fy, 1)])

    # На поле нет траектории (программу не запускали / робот не двигался) —
    # сохранять нечего: путь не прорисуется в миссии. Возвращаем no_trajectory;
    # клиент по этому ответу сам запустит программу (▶) и повторит сохранение.
    if len(path) < 2:
        return JSONResponse({"error": "no_trajectory"})

    # Контрольные точки = ГРАНИЦЫ движущихся манёвров (начало/конец дуги,
    # прямой, объезда, …). Развороты и автопилот точек НЕ дают: развернуться
    # можно по-разному, а отклонения при развороте и так не штрафуются.
    # Источник — отрезки манёвров, записанные ПРИ ВЫПОЛНЕНИИ:
    #   • Python-программа (▶) — RobotProxy._run_segment пишет sess._traj_segments;
    #   • голос/кнопки — отрезки восстанавливаем из endpoints _program.
    # waypoints — БЕЗ стартовой точки; последний waypoint = финиш маршрута.
    segments = list(sess._traj_segments)
    if not segments:
        MOVE_INTENTS = {
            "forward", "back", "forward_to_wall", "backward_to_wall",
            "course", "goto", "home",
            "circle", "figure_eight", "spiral_in", "spiral_out",
            "bypass_left", "bypass_right",
        }
        prev = (start_x, start_y)
        for c in sess._program:
            if c.end_x is None or c.end_y is None:
                continue
            end = (c.end_x, c.end_y)
            if c.intent in MOVE_INTENTS:
                segments.append((prev, end))
            prev = end
    key = keypoints_from_segments(segments)
    waypoints: list[list[float]] = (
        [[x, y] for x, y in key[1:]] if len(key) > 1 else [])

    # Контрольных точек нет, хотя путь на поле есть. Типичный случай:
    # маршрут построен 🧭 Автопилотом — он ведёт робота и дописывает код,
    # но его движения идут мимо _run_segment, поэтому _traj_segments пуст.
    # Просим клиент прогнать программу (▶) — тогда сгенерированные
    # face/forward пройдут через RobotProxy и отрезки манёвров запишутся.
    if not waypoints:
        return JSONResponse({"error": "no_trajectory"})

    # Зоны: красные — pre-placed обстановка; жёлтые — обязательные действия.
    danger_zones = []
    actions_required = []
    for z in sess.world.danger_zones:
        zk = getattr(z, "kind", "danger")
        if zk == "danger":
            danger_zones.append([round(z.x, 1), round(z.y, 1), round(z.radius, 1)])
        elif zk == "algorithm":
            actions_required.append({
                "type": "place_attention",
                "x": round(z.x, 1), "y": round(z.y, 1),
                "r": round(z.radius, 1),
            })

    # Набор препятствий, при которых строилась траектория. Решать миссию
    # нужно с теми же галочками «Препятствия» (стены — всегда).
    obstacles = sorted(sess.robot_state.obstacles)

    reference_voice = [c.raw for c in sess._program if c.raw]
    reference_code  = sess._program_text() or ""
    safety_margin_cm = float(sess.cfg.wall_thickness_cm)

    # ── Условие миссии (description) ──────────────────────────────────
    # Только ЦЕЛИ: старт, препятствия, контрольные точки, зоны. Список
    # команд НЕ пишем — путь подсказывает SVG-траектория, манёвры игрок
    # выбирает сам. reference_voice/code — эталон, виден только админу.
    obs_human = ["стены"]
    if "danger" in obstacles:    obs_human.append("опасные зоны")
    if "attention" in obstacles: obs_human.append("зоны внимания")

    # Округление координат для описания — ТОЧНО как Math.round в JS:
    # карта (tasks.html) подписывает точки через Math.round, и если в
    # описании брать int() (отбрасывает дробь), числа расходятся
    # (−14.6 → int −14, но Math.round −15). math.floor(v+0.5) совпадает
    # с Math.round для любых значений, включая .5.
    def _r(v):
        return _math.floor(float(v) + 0.5)

    desc_parts = []
    desc_parts.append(f"🟢 Старт маршрута ({_r(start_x)}, {_r(start_y)}).")
    desc_parts.append(
        "🚧 Препятствия — пройдите с этими галочками: "
        + ", ".join(obs_human) + ".")
    if waypoints:
        *mid, finish = waypoints
        if mid:
            mid_str = ", ".join(f"({_r(x)}, {_r(y)})" for x, y in mid)
            desc_parts.append(
                f"📍 Контрольные точки маршрута ({len(mid)} шт.): {mid_str}.")
        desc_parts.append(
            f"🏁 Финиш маршрута ({_r(finish[0])}, {_r(finish[1])}) — "
            f"обязательная точка.")
    else:
        desc_parts.append("📍 Манёвров не зафиксировано (робот не двигался).")
    if actions_required:
        zs = ", ".join(f"({a['x']:.0f}, {a['y']:.0f})" for a in actions_required)
        desc_parts.append(f"🟡 Установите зоны внимания: {zs}.")
    if danger_zones:
        zs = ", ".join(f"({z[0]:.0f}, {z[1]:.0f})" for z in danger_zones)
        desc_parts.append(
            f"⚠ Опасные зоны на карте ({len(danger_zones)} шт.): {zs}. "
            f"Не задевайте.")
    desc_parts.append(
        "⭐ Звёзды — за проезд через контрольные точки маршрута. "
        "Важна и точность ведения: робот должен идти по линии "
        "траектории. Если он отклонится от неё дальше габаритов "
        "робота — точность снижается.")
    description = "\n".join(desc_parts)

    for attempt in range(3):
        new_id = _smallest_unused_mission_id(db)
        actual_title = title if title else f"Кастомная #{new_id}"
        m = Mission(
            id=new_id,
            owner_id=current_user.id,
            title=actual_title,
            description=description,
            level=0,                        # 0 = «кастомная», не 1-5
            waypoints=json.dumps(waypoints),
            path=json.dumps(path),
            obstacles=json.dumps(obstacles),
            danger_zones=json.dumps(danger_zones),
            actions_required=json.dumps(actions_required),
            reference_voice=json.dumps(reference_voice),
            reference_code=reference_code,
            safety_margin_cm=safety_margin_cm,
            published=False,
        )
        try:
            db.add(m); db.commit(); db.refresh(m)
            await sess.push_message(
                f"💾 Миссия #{m.id} «{m.title}» сохранена в Каталог.",
                "success")
            return JSONResponse({"id": m.id, "ok": True, "title": m.title})
        except IntegrityError:
            db.rollback()
            continue
    return JSONResponse({"error": "id collision after retries"}, status_code=500)


@app.post("/missions/{mission_id}/start")
async def missions_start(mission_id: int,
                         db: Session = Depends(get_db),
                         current_user: User = Depends(require_user)):
    """Активировать миссию для текущей сессии пользователя.
    Доступ: своя миссия ИЛИ опубликованная другим."""
    m = db.query(Mission).filter(Mission.id == mission_id).first()
    if m is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    if m.owner_id != current_user.id and not m.published:
        return JSONResponse({"error": "not accessible"}, status_code=403)
    sess = get_session(current_user.id)
    if sess is None:
        return JSONResponse({"error": "no active session"}, status_code=400)
    ok = await sess.start_mission(mission_id)
    if not ok:
        return JSONResponse({"error": "could not start"}, status_code=500)
    return JSONResponse({"ok": True, "mission_id": mission_id})


@app.post("/missions/active/hint")
async def missions_hint_active(current_user: User = Depends(require_user)):
    """Подсказка для активной миссии: куда ехать к следующей непосещённой
    waypoint. Возвращает угол поворота относительно текущего курса
    (направо/налево/прямо) и расстояние, плюс шлёт сообщение в журнал."""
    import math as _math
    sess = get_session(current_user.id)
    if sess is None:
        return JSONResponse({"error": "no active session"}, status_code=400)
    if sess._mission is None:
        return JSONResponse({"error": "no active mission"}, status_code=400)
    m = sess._mission
    target = None
    for i, (wx, wy) in enumerate(m.waypoints):
        if i not in m.waypoints_visited:
            target = (i, wx, wy)
            break
    if target is None:
        await sess.push_message(
            "💡 Все контрольные точки пройдены. Жмите ▶ для проверки.",
            "success")
        return JSONResponse({"ok": True, "complete": True})
    idx, tx, ty = target
    s = sess.robot_state
    dx, dy = tx - s.x, ty - s.y
    distance = _math.hypot(dx, dy)
    # Курс к цели (0=север, по часовой). atan2(dx, dy) даёт нужный
    # знак в системе VEGAREX, где dy=cos(h), dx=sin(h).
    bearing_abs = _math.degrees(_math.atan2(dx, dy)) % 360
    relative = (bearing_abs - s.heading + 540.0) % 360.0 - 180.0
    # Подсказка — формулируется как готовые голосовые команды, которые
    # NLU гарантированно распознаёт:
    #   «Вега развернись на N» → face_to(N) (абсолютный курс)
    #   «Вега вперёд D см»     → forward(D)
    # «Налево/направо» неоднозначно (на роботе это руль), поэтому
    # используем абсолютный курс. «Развернуться»/«ехать» — инфинитивы,
    # NLU их не понимает, нужно повелительное наклонение.
    target_heading = int(round(bearing_abs)) % 360
    dist_int = max(5, int(round(distance)))
    if abs(relative) < 3:
        cmds = f"«Вега вперёд {dist_int}»"
    else:
        cmds = (f"«Вега развернись на {target_heading}», "
                f"затем «Вега вперёд {dist_int}»")
    await sess.push_message(
        f"💡 Точка #{idx + 1} ({tx:.0f}, {ty:.0f}): {cmds}.",
        "info")
    return JSONResponse({
        "ok":           True,
        "target_index": idx,
        "target_x":     round(tx, 1),
        "target_y":     round(ty, 1),
        "distance_cm":  round(distance, 1),
        "turn_deg":     round(relative, 1),
        "bearing_deg":  round(bearing_abs, 1),
    })


@app.post("/missions/active/check")
async def missions_check_active(request: Request,
                                current_user: User = Depends(require_user)):
    """Прогон программы для активной миссии — БЕЗ автозавершения.

    Текст из textarea приходит в body — это источник истины: если
    пользователь удалил/изменил команду в коде, мы перепарсиваем и
    подменяем self._program перед запуском, иначе старая команда
    выполнится повторно.

    Запускает _program через очередь, замеряет время и НАКАПЛИВАЕТ его
    к суммарному времени алгоритма (штраф за множественные пробы).
    Финал — отдельным действием через /missions/active/finalize."""
    import asyncio as _aio
    sess = get_session(current_user.id)
    if sess is None:
        return JSONResponse({"error": "no active session"}, status_code=400)
    if sess._mission is None:
        return JSONResponse({"error": "no active mission"}, status_code=400)
    code_text = ""
    try:
        body = await request.json()
        code_text = (body or {}).get("code", "") or ""
    except Exception:
        code_text = ""
    # Day 2: единственный путь — exec() пользовательского Python через
    # robot_api.run_user_python. Парсер _program больше не используется.
    if not code_text.strip():
        return JSONResponse({"error": "empty program"}, status_code=400)
    await sess.push_message("▶ Запуск Python-кода…", "info")
    _aio.create_task(sess.run_check_python(code_text))
    return JSONResponse({"ok": True})


@app.post("/missions/active/finalize")
async def missions_finalize_active(current_user: User = Depends(require_user)):
    """Финальная проверка задания — фиксирует результат и завершает миссию.
    Звёзды считаются по результату ПОСЛЕДНЕГО прогона + суммарному
    времени алгоритма по всем прогонам."""
    sess = get_session(current_user.id)
    if sess is None:
        return JSONResponse({"error": "no active session"}, status_code=400)
    if sess._mission is None:
        return JSONResponse({"error": "no active mission"}, status_code=400)
    await sess.finalize_mission()
    return JSONResponse({"ok": True})


@app.post("/missions/active/stop")
async def missions_stop_active(current_user: User = Depends(require_user)):
    """Завершить текущую активную миссию (по требованию пользователя).
    Незавершённая миссия не получает звёзд."""
    sess = get_session(current_user.id)
    if sess is None:
        return JSONResponse({"error": "no active session"}, status_code=400)
    if sess._mission is None:
        return JSONResponse({"ok": True, "was_active": False})
    await sess.stop_mission(success=False)
    return JSONResponse({"ok": True, "was_active": True})


@app.get("/stats", response_class=HTMLResponse)
async def stats_page(request: Request, db: Session = Depends(get_db),
                     current_user: User = Depends(require_user)):
    """Сводная статистика пользователя по прохождениям миссий.

    Общий блок:
      - сколько прохождений / сколько успешных
      - всего звёзд, средняя точность, общее время в миссиях
    По миссиям (агрегат лучших):
      - название миссии, лучшие звёзды, лучшая точность, лучшее время.
    """
    from sqlalchemy import func
    # Общие счётчики.
    total_runs = (db.query(func.count(MissionRun.id))
                    .filter(MissionRun.user_id == current_user.id)
                    .filter(MissionRun.completed_at.isnot(None)).scalar() or 0)
    success_runs = (db.query(func.count(MissionRun.id))
                    .filter(MissionRun.user_id == current_user.id)
                    .filter(MissionRun.success == True).scalar() or 0)
    total_stars = (db.query(func.sum(MissionRun.stars))
                    .filter(MissionRun.user_id == current_user.id)
                    .filter(MissionRun.completed_at.isnot(None)).scalar() or 0)
    avg_precision = (db.query(func.avg(MissionRun.coefficient))
                    .filter(MissionRun.user_id == current_user.id)
                    .filter(MissionRun.success == True).scalar() or 0.0)
    total_duration_sec = (db.query(func.sum(MissionRun.duration_sec))
                    .filter(MissionRun.user_id == current_user.id)
                    .filter(MissionRun.completed_at.isnot(None)).scalar() or 0.0)
    total_algo_duration_sec = (db.query(func.sum(MissionRun.algo_duration_sec))
                    .filter(MissionRun.user_id == current_user.id)
                    .filter(MissionRun.completed_at.isnot(None)).scalar() or 0.0)
    # Лучшее по каждой миссии: best_stars + best_precision + best_time.
    # Для best_time нужно min duration_sec ИМЕННО среди успешных и > 0.
    best_per_mission = (db.query(
                            MissionRun.mission_id,
                            func.max(MissionRun.stars).label("best_stars"),
                            func.max(MissionRun.coefficient).label("best_prec"),
                            func.count(MissionRun.id).label("runs_count"),
                        )
                        .filter(MissionRun.user_id == current_user.id)
                        .filter(MissionRun.completed_at.isnot(None))
                        .group_by(MissionRun.mission_id).all())
    # Лучшее время отдельно — только для успешных прогонов.
    best_time_per_mission = dict(
        db.query(MissionRun.mission_id, func.min(MissionRun.duration_sec))
          .filter(MissionRun.user_id == current_user.id)
          .filter(MissionRun.success == True)
          .filter(MissionRun.duration_sec > 0)
          .group_by(MissionRun.mission_id).all())
    best_algo_per_mission = dict(
        db.query(MissionRun.mission_id, func.min(MissionRun.algo_duration_sec))
          .filter(MissionRun.user_id == current_user.id)
          .filter(MissionRun.success == True)
          .filter(MissionRun.algo_duration_sec > 0)
          .group_by(MissionRun.mission_id).all())
    # Подтянем названия миссий одним запросом.
    mission_ids = [r.mission_id for r in best_per_mission]
    titles = {}
    if mission_ids:
        for mid, mtitle in (db.query(Mission.id, Mission.title)
                              .filter(Mission.id.in_(mission_ids)).all()):
            titles[mid] = mtitle
    # Собираем строки для таблицы — отсортируем по best_stars desc.
    rows = []
    for r in best_per_mission:
        rows.append({
            "mission_id":  r.mission_id,
            "title":       titles.get(r.mission_id, f"Миссия #{r.mission_id}"),
            "best_stars":  r.best_stars or 0,
            "best_prec":   round((r.best_prec or 0.0) * 100),
            "best_time":   best_time_per_mission.get(r.mission_id),
            "best_algo":   best_algo_per_mission.get(r.mission_id),
            "runs_count":  r.runs_count or 0,
        })
    rows.sort(key=lambda x: (-x["best_stars"], -x["best_prec"]))
    return templates.TemplateResponse(request, "stats.html", {
        "current_user":       current_user,
        "total_runs":         int(total_runs),
        "success_runs":       int(success_runs),
        "total_stars":        int(total_stars),
        "avg_precision_pct":  int(round((avg_precision or 0.0) * 100)),
        "total_duration_sec":      float(total_duration_sec or 0.0),
        "total_algo_duration_sec": float(total_algo_duration_sec or 0.0),
        "rows":                    rows,
    })


@app.post("/stats/reset")
async def stats_reset(admin: User = Depends(require_admin),
                      db: Session = Depends(get_db)):
    """Админ-сброс ВСЕЙ статистики: удаляются все MissionRun (всех
    пользователей). Карточки «Набрано: N⭐» в каталоге миссий пропадут,
    страница /stats обнулится. Сами миссии и их waypoints не трогаются."""
    deleted = db.query(MissionRun).delete()
    db.commit()
    log.info("Admin %s reset stats: %d MissionRun records deleted",
             admin.username, deleted)
    return RedirectResponse("/stats", status_code=303)


@app.get("/missions", response_class=HTMLResponse)
async def missions_page(request: Request, db: Session = Depends(get_db),
                        current_user: User = Depends(require_user)):
    sessions = (db.query(RobotSession)
                  .filter(RobotSession.user_id == current_user.id)
                  .order_by(RobotSession.started_at.desc()).limit(50).all())
    return templates.TemplateResponse(request, "missions.html", {
        "sessions":     sessions,
        "current_user": current_user,
    })


@app.post("/missions/clear")
async def missions_clear(db: Session = Depends(get_db),
                         current_user: User = Depends(require_user)):
    """Полностью удалить историю сессий текущего пользователя.
    Активная DB-сессия (та, что записывает текущие команды) исключается —
    иначе сломается логирование."""
    sess = get_session(current_user.id)
    keep_id = sess._db_session_id if sess else None

    q = db.query(RobotSession).filter(RobotSession.user_id == current_user.id)
    if keep_id is not None:
        q = q.filter(RobotSession.id != keep_id)
    rows = q.all()
    for r in rows:
        db.delete(r)   # cascade удалит CommandLog и PathPoint
    db.commit()
    return RedirectResponse("/missions", status_code=303)


# ═══════════════════════════════════════════════════════════════════════════════
# Настройки (per-user)
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request,
                        current_user: User = Depends(require_user),
                        db: Session = Depends(get_db)):
    row = _ensure_user_settings(db, current_user.id)
    return templates.TemplateResponse(request, "settings.html", {
        "simulated":    row.simulation_mode,
        "current_user": current_user,
        "cfg": {
            "host":               row.rex_host,
            "port":               row.rex_port,
            "simulation":         row.simulation_mode,
            "move_speed":         row.move_speed,
            "turn_angle":         row.turn_angle,
            "wheel_circ":         row.wheel_circ_cm,
            "speed_at_100":       row.speed_at_100,
            "heading_per_rot":    row.heading_per_rot,
            "turn_speed_ref":     row.turn_speed_ref,
            "danger_zone_radius": row.danger_zone_radius,
            "world_w":            row.world_w_cm,
            "world_h":            row.world_h_cm,
            "wall_thickness":     row.wall_thickness_cm,
            "robot_length":       row.robot_length_cm,
            "robot_width":        row.robot_width_cm,
            "start_x":            row.start_x_cm,
            "start_y":            row.start_y_cm,
            "start_heading":      row.start_heading_deg,
            "sensor_type":        row.sensor_type,
            "sonar_interval_ms":  row.sonar_interval_ms,
            "battery_minutes":    row.battery_minutes,
            "path_cell_size_cm":  row.path_cell_size_cm,
            "autopilot_algo":     row.autopilot_algo,
            "cautious_follow_algo": row.cautious_follow_algo,
            "cautious_slow_curves": row.cautious_slow_curves,
            "wall_turn_strategy":   row.wall_turn_strategy,
        },
    })


@app.post("/api/settings")
async def api_settings_save(
    host:            str   = Form(...),
    port:            int   = Form(...),
    simulation:      str   = Form("0"),
    move_speed:      int   = Form(...),
    turn_angle:      int   = Form(...),
    wheel_circ:      float = Form(...),
    speed_at_100:    float = Form(...),
    heading_per_rot: float = Form(...),
    turn_speed_ref:  int   = Form(...),
    danger_zone_radius: float = Form(...),
    world_w:            float = Form(...),
    world_h:            float = Form(...),
    wall_thickness:     float = Form(...),
    robot_length:       float = Form(20.0),
    robot_width:        float = Form(12.0),
    start_x:            float = Form(0.0),
    start_y:            float = Form(0.0),
    start_heading:      float = Form(0.0),
    sensor_type:        str   = Form("laser"),
    sonar_interval_ms:  int   = Form(100),
    battery_minutes:    int   = Form(60),
    path_cell_size_cm:  int   = Form(10),
    autopilot_algo:     str   = Form("polyline"),
    cautious_follow_algo: str = Form("pure_pursuit"),
    cautious_slow_curves: str = Form("0"),
    wall_turn_strategy:   str = Form("backoff"),
    current_user: User    = Depends(require_user),
    db:           Session = Depends(get_db),
):
    row = _ensure_user_settings(db, current_user.id)

    new_sim     = (simulation == "1")
    reconnect   = (host != row.rex_host or port != row.rex_port or new_sim != row.simulation_mode)

    row.rex_host          = host
    row.rex_port          = port
    row.simulation_mode   = new_sim
    row.move_speed        = max(5, min(100, move_speed))
    row.turn_angle        = max(5, min(45, turn_angle))
    row.wheel_circ_cm     = max(1.0, wheel_circ)
    row.speed_at_100      = max(1.0, speed_at_100)
    row.heading_per_rot   = max(0.1, heading_per_rot)
    row.turn_speed_ref    = max(5, min(100, turn_speed_ref))
    row.danger_zone_radius= max(1.0, min(500.0, danger_zone_radius))
    row.world_w_cm        = max(100.0, world_w)
    row.world_h_cm        = max(100.0, world_h)
    row.wall_thickness_cm = max(0.5, min(50.0, wall_thickness))
    row.robot_length_cm   = max(1.0, min(200.0, robot_length))
    row.robot_width_cm    = max(1.0, min(200.0, robot_width))
    half_len = row.robot_length_cm / 2.0
    hw = row.world_w_cm / 2.0 - half_len
    hh = row.world_h_cm / 2.0 - half_len
    row.start_x_cm        = max(-hw, min(hw, start_x))
    row.start_y_cm        = max(-hh, min(hh, start_y))
    row.start_heading_deg = start_heading % 360
    row.sensor_type       = sensor_type if sensor_type in ("laser", "sonar") else "laser"
    row.sonar_interval_ms = max(10, min(1000, sonar_interval_ms))
    row.battery_minutes   = max(1, min(720, battery_minutes))   # 1 мин — 12 ч
    row.path_cell_size_cm = max(2, min(50, path_cell_size_cm))
    row.autopilot_algo    = (autopilot_algo
                             if autopilot_algo in ("polyline", "smooth")
                             else "polyline")
    row.cautious_follow_algo = (cautious_follow_algo
                                if cautious_follow_algo in (
                                    "pure_pursuit", "stanley",
                                    "linear", "manual")
                                else "pure_pursuit")
    row.cautious_slow_curves = (cautious_slow_curves == "1")
    row.wall_turn_strategy   = (wall_turn_strategy
                                if wall_turn_strategy in ("backoff", "multi_step", "manual")
                                else "backoff")
    db.commit()

    # Если у пользователя есть активная сессия — обновляем ее настройки на лету
    sess = get_session(current_user.id)
    if sess is not None:
        new_cfg = UserCfg.from_row(row)
        await sess.apply_new_cfg(new_cfg, reconnect=reconnect)
        await sess.push_message("Настройки сохранены.", "success")

    return RedirectResponse("/settings", status_code=303)


# ═══════════════════════════════════════════════════════════════════════════════
# API
# ═══════════════════════════════════════════════════════════════════════════════

@app.post("/api/command")
async def api_command(text: str = Form(...),
                      current_user: User = Depends(require_user),
                      db: Session = Depends(get_db)):
    sess = await get_or_create_session(current_user.id, db)
    await sess.handle_command(text, db)
    return {"ok": True}


@app.get("/api/status")
async def api_status(current_user: User = Depends(require_user),
                     db: Session = Depends(get_db)):
    sess = await get_or_create_session(current_user.id, db)
    from world_xy import state_to_dict
    return {
        "robot_online": sess.robot.connected,
        "simulated":    sess.cfg.simulation_mode,
        "robot":        state_to_dict(sess.robot_state),
    }


@app.post("/api/robot/connect")
async def api_robot_connect(current_user: User = Depends(require_user),
                             db: Session = Depends(get_db)):
    sess = await get_or_create_session(current_user.id, db)
    ok = await sess.robot.connect()
    return {"ok": ok, "connected": sess.robot.connected}


@app.get("/api/danger_zones")
async def api_danger_zones(current_user: User = Depends(require_user),
                            db: Session = Depends(get_db)):
    zones = (db.query(DangerZone)
              .filter(DangerZone.user_id == current_user.id)
              .filter(DangerZone.active == True).all())
    return [{"id": z.id, "label": z.label, "x": z.x, "y": z.y, "radius": z.radius}
            for z in zones]


@app.post("/api/danger_zones")
async def api_add_zone(x: float = Form(...), y: float = Form(...),
                        radius: float = Form(50.0),
                        label: str = Form("Зона опасности"),
                        current_user: User = Depends(require_user),
                        db: Session = Depends(get_db)):
    sess = await get_or_create_session(current_user.id, db)
    # Порядковый номер опасной зоны: max display_no в kind='danger' + 1.
    # Считаем по in-memory world (источник истины для активных зон).
    no = sess._next_zone_display_no("danger")
    dz = DangerZone(user_id=current_user.id, label=label, x=x, y=y,
                    radius=radius, kind="danger", display_no=no)
    db.add(dz); db.commit()
    sess.world.add_danger_zone(x, y, radius, label, db_id=dz.id,
                                kind="danger", display_no=no)
    await sess.push_message(
        f"⚠ Опасная #{no} в ({x:.0f}, {y:.0f}), r={radius:.0f}.", "info")
    await sess.push_world()
    return {"ok": True, "id": dz.id, "display_no": no}


@app.delete("/api/danger_zones/{zone_id}")
async def api_delete_zone(zone_id: int,
                           current_user: User = Depends(require_user),
                           db: Session = Depends(get_db)):
    dz = (db.query(DangerZone)
            .filter(DangerZone.id == zone_id)
            .filter(DangerZone.user_id == current_user.id).first())
    if dz:
        dz.active = False
        db.commit()
        sess = get_session(current_user.id)
        if sess is not None:
            sess.world.remove_danger_zone_by_db_id(zone_id)
            await sess.push_world()
    return {"ok": True}


@app.get("/api/session/{session_id}/path")
async def api_session_path(session_id: int, db: Session = Depends(get_db)):
    pts = db.query(PathPoint).filter(PathPoint.session_id == session_id).all()
    return [{"x": p.x, "y": p.y, "heading": p.heading} for p in pts]


# ═══════════════════════════════════════════════════════════════════════════════
# Библиотека маршрутов: свои сохраненные + опубликованные другими
# ═══════════════════════════════════════════════════════════════════════════════

def _count_cmds_in_text(text: str) -> int:
    """Считает CMD-маркеры в тексте программы."""
    from session import UserSession
    return sum(1 for line in text.split("\n") if UserSession._CMD_MARKER.match(line))


@app.get("/library", response_class=HTMLResponse)
async def library_index(request: Request,
                         current_user: User = Depends(require_user),
                         db: Session = Depends(get_db)):
    saved = (db.query(SavedRoute)
               .filter(SavedRoute.owner_id == current_user.id)
               .order_by(SavedRoute.updated_at.desc()).all())
    public = (db.query(PublishedRoute)
                .order_by(PublishedRoute.created_at.desc())
                .limit(200).all())
    return templates.TemplateResponse(request, "library.html", {
        "current_user": current_user,
        "saved_routes":  saved,
        "public_routes": public,
    })


# ── Просмотр / удаление ───────────────────────────────────────────────────────

@app.get("/library/published/{route_id}", response_class=HTMLResponse)
async def library_published_detail(route_id: int, request: Request,
                                    current_user: User = Depends(require_user),
                                    db: Session = Depends(get_db)):
    route = db.query(PublishedRoute).filter(PublishedRoute.id == route_id).first()
    if not route:
        return RedirectResponse("/library", status_code=303)
    return templates.TemplateResponse(request, "library_detail.html", {
        "current_user": current_user,
        "route":        route,
        "kind":         "published",
        "is_owner":     (route.author_id == current_user.id) or current_user.is_admin,
    })


@app.get("/library/saved/{route_id}", response_class=HTMLResponse)
async def library_saved_detail(route_id: int, request: Request,
                                current_user: User = Depends(require_user),
                                db: Session = Depends(get_db)):
    route = (db.query(SavedRoute)
               .filter(SavedRoute.id == route_id)
               .filter(SavedRoute.owner_id == current_user.id).first())
    if not route:
        return RedirectResponse("/library", status_code=303)
    return templates.TemplateResponse(request, "library_detail.html", {
        "current_user": current_user,
        "route":        route,
        "kind":         "saved",
        "is_owner":     True,
    })


@app.post("/library/published/{route_id}/delete")
async def library_published_delete(route_id: int,
                                    current_user: User = Depends(require_user),
                                    db: Session = Depends(get_db)):
    route = db.query(PublishedRoute).filter(PublishedRoute.id == route_id).first()
    if route and (route.author_id == current_user.id or current_user.is_admin):
        db.delete(route); db.commit()
    return RedirectResponse("/library", status_code=303)


@app.post("/library/saved/{route_id}/delete")
async def library_saved_delete(route_id: int,
                                current_user: User = Depends(require_user),
                                db: Session = Depends(get_db)):
    route = (db.query(SavedRoute)
               .filter(SavedRoute.id == route_id)
               .filter(SavedRoute.owner_id == current_user.id).first())
    if route:
        db.delete(route); db.commit()
    return RedirectResponse("/library", status_code=303)


# ── Сохранение текущей программы как личного маршрута ─────────────────────────

@app.get("/library/save/new", response_class=HTMLResponse)
async def library_save_form(request: Request,
                             current_user: User = Depends(require_user),
                             db: Session = Depends(get_db)):
    sess = await get_or_create_session(current_user.id, db)
    return templates.TemplateResponse(request, "library_save.html", {
        "current_user": current_user,
        "code":         sess._program_text(),
        "cmd_count":    len(sess._program),
    })


@app.post("/library/save")
async def library_save_submit(
    title:       str = Form(...),
    description: str = Form(""),
    code:        str = Form(...),
    current_user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    title = title.strip()[:120]
    if not title:
        return RedirectResponse("/library/save/new?err=no_title", status_code=303)
    saved = SavedRoute(
        owner_id    = current_user.id,
        title       = title,
        description = description.strip()[:2000] or None,
        code        = code,
        cmd_count   = _count_cmds_in_text(code),
    )
    db.add(saved); db.commit()
    return RedirectResponse("/library", status_code=303)


# ── Публикация в общий каталог (как раньше) ───────────────────────────────────

@app.get("/library/publish/new", response_class=HTMLResponse)
async def library_publish_form(request: Request,
                                current_user: User = Depends(require_user),
                                db: Session = Depends(get_db)):
    sess = await get_or_create_session(current_user.id, db)
    return templates.TemplateResponse(request, "library_publish.html", {
        "current_user": current_user,
        "code":         sess._program_text(),
        "cmd_count":    len(sess._program),
        "parent_id":    request.query_params.get("from", ""),
    })


@app.post("/library/publish")
async def library_publish_submit(
    title:       str = Form(...),
    description: str = Form(""),
    code:        str = Form(...),
    parent_id:   str = Form(""),
    current_user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    title = title.strip()[:120]
    if not title:
        return RedirectResponse("/library/publish/new?err=no_title", status_code=303)
    parent_int = None
    if parent_id.isdigit():
        parent_int = int(parent_id)
        if not db.query(PublishedRoute).filter(PublishedRoute.id == parent_int).first():
            parent_int = None
    route = PublishedRoute(
        author_id   = current_user.id,
        title       = title,
        description = description.strip()[:2000] or None,
        code        = code,
        cmd_count   = _count_cmds_in_text(code),
        parent_id   = parent_int,
    )
    db.add(route); db.commit(); db.refresh(route)
    return RedirectResponse(f"/library/published/{route.id}", status_code=303)


# ── Загрузка с подтверждением и опциональным сохранением текущей ──────────────

def _fetch_route(kind: str, route_id: int, user_id: int, db: Session):
    """Загружает (saved|published) маршрут с проверкой доступа."""
    if kind == "saved":
        return (db.query(SavedRoute)
                  .filter(SavedRoute.id == route_id)
                  .filter(SavedRoute.owner_id == user_id).first())
    if kind == "published":
        return db.query(PublishedRoute).filter(PublishedRoute.id == route_id).first()
    return None


@app.get("/library/{kind}/{route_id}/load", response_class=HTMLResponse)
async def library_load_dialog(kind: str, route_id: int, request: Request,
                               current_user: User = Depends(require_user),
                               db: Session = Depends(get_db)):
    """Страница подтверждения загрузки. Показывает что грузится и предлагает
    предварительно сохранить ТЕКУЩУЮ программу (опционально)."""
    if kind not in ("saved", "published"):
        return RedirectResponse("/library", status_code=303)
    route = _fetch_route(kind, route_id, current_user.id, db)
    if not route:
        return RedirectResponse("/library", status_code=303)
    sess = await get_or_create_session(current_user.id, db)
    has_current = len(sess._program) > 0
    author_name = None
    if kind == "published" and route.author:
        author_name = route.author.username
    return templates.TemplateResponse(request, "library_load_confirm.html", {
        "current_user":        current_user,
        "route":               route,
        "kind":                kind,
        "author_name":         author_name,
        "has_current":         has_current,
        "current_cmd_count":   len(sess._program),
        "current_code":        sess._program_text(),
        "suggested_title":     f"Бэкап перед загрузкой «{route.title}»",
    })


@app.post("/library/{kind}/{route_id}/load")
async def library_load_submit(kind: str, route_id: int,
                               save_title:       str = Form(""),
                               save_description: str = Form(""),
                               current_code:     str = Form(""),
                               current_user: User = Depends(require_user),
                               db: Session = Depends(get_db)):
    if kind not in ("saved", "published"):
        return RedirectResponse("/library", status_code=303)
    route = _fetch_route(kind, route_id, current_user.id, db)
    if not route:
        return RedirectResponse("/library", status_code=303)

    sess = await get_or_create_session(current_user.id, db)

    # Опционально — сохраняем ТЕКУЩУЮ программу как личный маршрут
    save_title = save_title.strip()[:120]
    if save_title and current_code.strip():
        backup = SavedRoute(
            owner_id    = current_user.id,
            title       = save_title,
            description = save_description.strip()[:2000] or None,
            code        = current_code,
            cmd_count   = _count_cmds_in_text(current_code),
        )
        db.add(backup); db.commit()

    # Подгружаем выбранный маршрут
    if kind == "saved":
        label = f"«{route.title}» (свое)"
    else:
        label = f"«{route.title}»"
        if route.author and route.author.username:
            label += f" от {route.author.username}"
    await sess.load_published_code(route.code, source_label=label)

    return RedirectResponse("/", status_code=303)


# ═══════════════════════════════════════════════════════════════════════════════
# WebSocket
# ═══════════════════════════════════════════════════════════════════════════════

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    user_id = ws.session.get("user_id") if hasattr(ws, "session") else None
    if not user_id:
        await ws.close(code=1008)
        return

    db = SessionLocal()
    # Защита от протухшей cookie: если user был удален (например, после wipe БД),
    # WS-сессию не открываем — клиент получит close, JS пойдет на редирект.
    user_exists = db.query(User).filter(User.id == user_id).first()
    if not user_exists:
        db.close()
        await ws.close(code=1008)
        return

    try:
        sess = await get_or_create_session(user_id, db)
    except Exception as exc:
        log.error("Cannot create session for user %d: %s", user_id, exc)
        db.close()
        await ws.close(code=1011)
        return

    await sess.add_ws(ws)
    try:
        while True:
            data = await ws.receive_json()
            t = data.get("type")
            if t == "command":
                await sess.handle_command(data["text"], db)
            elif t == "run_program":
                await sess._run_program()
            elif t == "run_python_code":
                code = data.get("code", "")
                # Day 2: единственный путь — exec() с фасадом `robot`. Старый
                # парсер хелперов-шаблонов удалён, codegen всегда генерит
                # `robot.X(...)` строки.
                # КРИТИЧНО: запускаем как фоновую задачу. Если await'ить
                # прямо в WS-loop'е, server БЛОКИРОВАН пока exec не
                # закончится → ■ СТОП от пользователя НЕ ПРИХОДИТ.
                from robot_api import run_user_python
                await ws.send_json({"type": "code_exec_result",
                                    "output": "▶ Запускаю Python-код…",
                                    "level":  "info"})
                # Если предыдущий run ещё не закончился — отказ
                # (двойной запуск приведёт к двум exec в разных потоках).
                prev_run = getattr(sess, "_python_run_task", None)
                if prev_run is not None and not prev_run.done():
                    await ws.send_json({
                        "type": "code_exec_result",
                        "output": "⚠ Программа уже выполняется. "
                                  "Нажмите ■ СТОП и попробуйте снова.",
                        "level": "error"})
                else:
                    async def _runner(code_text=code):
                        out = await run_user_python(sess, code_text)
                        is_error = ("Ошибка" in out[:32]) or out.startswith("⏹")
                        await sess.broadcast({
                            "type":   "code_exec_result",
                            "output": out,
                            "level":  "error" if is_error else "ok"})
                    sess._python_run_task = asyncio.create_task(_runner())
            elif t == "sync_code":
                # Клиент синхронизирует текущий текст textarea с сервером —
                # нужно перед «↺ Поле», чтобы reset прочитал актуальные
                # START_X/Y/HEADING_DEG из кода пользователя, а не из настроек.
                code = data.get("code", "")
                if isinstance(code, str):
                    sess._last_python_code = code
            elif t == "clear_program":
                sess._program.clear()
                # При очистке программы забываем кэшированный текст и
                # эффективный старт — преамбула регенерируется из настроек,
                # маркер «домой» возвращается в cfg.start_x_cm/y_cm.
                sess._last_python_code = None
                sess._effective_start_x = None
                sess._effective_start_y = None
                sess._effective_start_heading = None
                sess._save_program()
                await sess.push_program()
                await sess.push_world()  # пнём клиента — пусть перерисует маркер
            elif t == "set_laser":
                enabled = bool(data.get("enabled", True))
                sess.cfg.laser_enabled = enabled
                # Если выключаем дальномер прямо во время движения —
                # снимаем флаг, чтобы физика больше его не учитывала.
                if not enabled:
                    sess.robot_state.laser_stop = False
                await sess.push_message(
                    "Дальномер " + ("включен." if enabled else "выключен."),
                    "info")
            elif t == "set_obstacles":
                # Галочки «Препятствия» (Опасные зоны / Зоны внимания).
                # data["obstacles"] — список из {"danger", "attention"}.
                obs = data.get("obstacles", [])
                # Во время миссии (режим WM) набор препятствий зафиксирован
                # пустым (только стены) — менять нельзя.
                if sess._mission is not None:
                    await sess.push_message(
                        "🎯 Во время миссии препятствия зафиксированы "
                        "(только стены). Заверши миссию, чтобы менять.",
                        "warning")
                    await sess.push_state()
                elif isinstance(obs, list):
                    await sess.apply_obstacles_from_ui(obs)
            elif t == "set_zone_mode":
                # ⛯ «Режим зон» — взаимоисключающий с Инспектором и
                # Осторожно. Включение чистит cautious. При включённом
                # zone_mode handle_command блокирует команды движения.
                active = bool(data.get("active", False))
                s = sess.robot_state
                # Во время миссии ⛯ Зоны заблокирован — обучающийся не
                # может править обстановку, иначе условия миссии ломаются.
                if active and sess._mission is not None:
                    await sess.push_message(
                        "⛯ Во время миссии «Обстановка» недоступна. "
                        "Заверши или останови миссию.",
                        "warning")
                    # Принудительно синкаем клиента (вдруг локально успел
                    # подсветить кнопку).
                    await sess.push_state()
                    continue
                if active:
                    # Прерываем любую активную работу (на всякий случай,
                    # если робот ехал и пользователь решил настроить зоны).
                    await sess._do_stop()
                    # Запоминаем текущий режим, чтобы вернуть его при
                    # выходе. Без этого пользователь, бывший в «Осторожно»,
                    # после ⛯ Зоны попадал в Инспектор и плёлся через зоны.
                    sess._obstacles_before_zone = set(s.obstacles)
                    s.zone_mode = True
                    sess._apply_obstacles(set())
                    s.mode      = "normal"
                    await sess.push_message(
                        "⛯ Обстановка ВКЛ. ЛКМ — поставить, ПКМ — убрать. "
                        "ESC или клик по «⛯ Обстановка» — выход.", "info")
                else:
                    s.zone_mode = False
                    # Восстанавливаем набор препятствий, что был до ⛯ Зоны.
                    # Если в Режим зон не входили (флага нет) — оставляем
                    # как есть (default = только стены).
                    if sess._obstacles_before_zone is not None:
                        sess._apply_obstacles(sess._obstacles_before_zone)
                        sess._obstacles_before_zone = None
                    await sess.push_message(
                        f"⛯ Обстановка выключена. "
                        f"{sess._obstacles_human()}",
                        "info")
                    # При выходе из режима зон — обстановка зафиксирована
                    # и обновляется в коде программы (блок DANGER_ZONES).
                    await sess.broadcast({
                        "type": "obstacle_block",
                        "block": sess._obstacle_block(),
                    })
                await sess.push_state()
            elif t == "program_pause":
                # ⏸ Пауза — мотор глушится, exec замирает на текущей команде.
                await sess._do_program_pause()
            elif t == "program_resume":
                # ▶ Продолжить — восстанавливает speed и отпускает _wait_movement.
                await sess._do_program_resume()
            elif t == "ping":
                await ws.send_json({"type": "pong"})
            # voice_listen игнорируем — голос пока выключен в мульти-режиме
    except WebSocketDisconnect:
        sess.remove_ws(ws)
    except Exception as e:
        log.error("WS error (user %d): %s", user_id, e)
        sess.remove_ws(ws)
    finally:
        db.close()
