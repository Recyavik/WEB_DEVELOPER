import csv
import io
import json
import random
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Form, Request, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import joinedload

import book as booklib
import config
import email_service
import smtp_settings
from auth import (flash, generate_password, generate_teacher_code,
                  hash_password, pop_flashes, verify_password)
from database import Base, SessionLocal, engine, get_db
from models import Admin, Group, Sentence, Student, Teacher, Trainer, TrainerGroup, TrainerResult
from morpho import (ALL_POS, POS_COLORS, analyze_sentence, full_analyze,
                    LEVEL_INTRODUCTORY, LEVEL_LABELS, LEVEL_COLORS,
                    LEVEL_REQUIRED_FIELDS, SCORED_VAR_FEATURES, SCORED_CONST_FEATURES,
                    analyze_word_as_pos)


# ── App setup ──────────────────────────────────────────────────────────────────

def _run_migrations():
    """Apply incremental schema changes that SQLAlchemy create_all won't add
    to existing databases (ALTER TABLE for new columns)."""
    from sqlalchemy import text, inspect
    with engine.connect() as conn:
        inspector = inspect(engine)

        # trainers: add level, max_sentences, shuffle columns
        trainer_cols = {c["name"] for c in inspector.get_columns("trainers")}
        if "level" not in trainer_cols:
            conn.execute(text(
                "ALTER TABLE trainers ADD COLUMN level VARCHAR(20) DEFAULT 'introductory'"
            ))
        if "max_sentences" not in trainer_cols:
            conn.execute(text(
                "ALTER TABLE trainers ADD COLUMN max_sentences INTEGER DEFAULT 0"
            ))
        if "shuffle" not in trainer_cols:
            conn.execute(text(
                "ALTER TABLE trainers ADD COLUMN shuffle INTEGER DEFAULT 1"
            ))

        # sentences: add status, analysis_json, teacher_analysis_json
        sentence_cols = {c["name"] for c in inspector.get_columns("sentences")}
        if "status" not in sentence_cols:
            conn.execute(text(
                "ALTER TABLE sentences ADD COLUMN status VARCHAR(20) DEFAULT 'analyzed'"
            ))
        if "analysis_json" not in sentence_cols:
            conn.execute(text(
                "ALTER TABLE sentences ADD COLUMN analysis_json TEXT DEFAULT '[]'"
            ))
        if "teacher_analysis_json" not in sentence_cols:
            conn.execute(text(
                "ALTER TABLE sentences ADD COLUMN teacher_analysis_json TEXT"
            ))

        # trainer_groups table (create if absent)
        if not inspector.has_table("trainer_groups"):
            conn.execute(text("""
                CREATE TABLE trainer_groups (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    trainer_id INTEGER NOT NULL REFERENCES trainers(id) ON DELETE CASCADE,
                    group_id   INTEGER NOT NULL REFERENCES groups(id)   ON DELETE CASCADE,
                    UNIQUE (trainer_id, group_id)
                )
            """))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_tg_trainer_id ON trainer_groups(trainer_id)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_tg_group_id   ON trainer_groups(group_id)"))

        conn.commit()

    # Backfill analysis_json for existing sentences that have correct_pos_json
    # but empty analysis_json (first migration only)
    with SessionLocal() as db:
        from sqlalchemy import text as sql_text
        rows = db.execute(sql_text(
            "SELECT id, text FROM sentences WHERE analysis_json IS NULL OR analysis_json = '[]'"
        )).fetchall()
        for row in rows:
            try:
                tokens = full_analyze(row.text, level=LEVEL_INTRODUCTORY)
                db.execute(sql_text(
                    "UPDATE sentences SET analysis_json = :aj WHERE id = :sid"
                ), {"aj": json.dumps(tokens, ensure_ascii=False), "sid": row.id})
            except Exception:
                pass
        db.commit()


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    _run_migrations()
    with SessionLocal() as db:
        if not db.query(Admin).first():
            db.add(Admin(username="admin", password_hash=hash_password("admin123")))
            db.commit()
    yield


app = FastAPI(lifespan=lifespan)

from starlette.middleware.sessions import SessionMiddleware
app.add_middleware(SessionMiddleware, secret_key=config.SECRET_KEY)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


def _featlines(d: dict) -> str:
    """Jinja filter: convert {key: val} → 'key: val\nkey: val'"""
    if not d:
        return ""
    return "\n".join(f"{k}: {v}" for k, v in d.items())

templates.env.filters["featlines"] = _featlines


# ── Helpers ────────────────────────────────────────────────────────────────────

def tpl(request: Request, name: str, ctx: dict | None = None) -> HTMLResponse:
    context = {
        "request": request,
        "flashes": pop_flashes(request),
        "email_enabled": smtp_settings.is_enabled(),
        "level_labels": LEVEL_LABELS,
        "level_colors": LEVEL_COLORS,
    }
    if ctx:
        context.update(ctx)
    return templates.TemplateResponse(name, context)


def redir(url: str) -> RedirectResponse:
    return RedirectResponse(url=url, status_code=303)


def db_session():
    return SessionLocal()


def guard_admin(request: Request) -> Optional[RedirectResponse]:
    if request.session.get("role") != "admin":
        return redir("/admin/login")
    return None


def guard_teacher(request: Request) -> Optional[RedirectResponse]:
    if request.session.get("role") != "teacher":
        return redir("/teacher/login")
    return None


def guard_student(request: Request) -> Optional[RedirectResponse]:
    if request.session.get("role") != "student":
        return redir("/student/login")
    return None


def current_teacher(request: Request, db) -> Optional[Teacher]:
    tid = request.session.get("teacher_id")
    if not tid:
        return None
    return db.query(Teacher).filter_by(id=tid).first()


# ── Public ─────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return tpl(request, "index.html")


@app.get("/about", response_class=HTMLResponse)
async def about(request: Request):
    return tpl(request, "about.html")


# ── Admin: auth ────────────────────────────────────────────────────────────────

@app.get("/admin/login", response_class=HTMLResponse)
async def admin_login_get(request: Request):
    return tpl(request, "admin/login.html")


@app.post("/admin/login")
async def admin_login_post(request: Request,
                            username: str = Form(...),
                            password: str = Form(...)):
    with db_session() as db:
        admin = db.query(Admin).filter_by(username=username).first()
        if not admin or not verify_password(password, admin.password_hash):
            flash(request, "Неверный логин или пароль", "error")
            return redir("/admin/login")
        request.session["role"] = "admin"
        request.session["admin_id"] = admin.id
    return redir("/admin/")


@app.get("/admin/logout")
async def admin_logout(request: Request):
    request.session.clear()
    return redir("/")


# ── Admin: dashboard ───────────────────────────────────────────────────────────

@app.get("/admin/", response_class=HTMLResponse)
async def admin_dashboard(request: Request):
    if g := guard_admin(request): return g
    with db_session() as db:
        teachers = (db.query(Teacher)
                    .order_by(Teacher.created_at.desc()).all())
        total_students = db.query(Student).count()
        total_results = db.query(TrainerResult).count()
    return tpl(request, "admin/dashboard.html",
               {"teachers": teachers,
                "total_students": total_students,
                "total_results": total_results})


@app.get("/admin/teachers", response_class=HTMLResponse)
async def admin_teachers(request: Request):
    if g := guard_admin(request): return g
    with db_session() as db:
        teachers = db.query(Teacher).order_by(Teacher.name).all()
    return tpl(request, "admin/teachers.html", {"teachers": teachers})


@app.post("/admin/teachers/add")
async def admin_add_teacher(request: Request,
                             name: str = Form(...),
                             email: str = Form(...),
                             password: str = Form(...)):
    if g := guard_admin(request): return g
    with db_session() as db:
        if db.query(Teacher).filter_by(email=email).first():
            flash(request, f"Учитель с email {email} уже существует", "error")
            return redir("/admin/teachers")
        code = generate_teacher_code(db)
        t = Teacher(code=code, name=name, email=email,
                    password_hash=hash_password(password))
        db.add(t)
        db.commit()
        sent = email_service.send_teacher_welcome(email, name, code, password)
        msg = f"Учитель {name} добавлен. Код: {code}"
        if sent:
            msg += " (приглашение отправлено на email)"
        flash(request, msg, "success")
    return redir("/admin/teachers")


@app.post("/admin/teachers/{tid}/toggle")
async def admin_toggle_teacher(request: Request, tid: int):
    if g := guard_admin(request): return g
    with db_session() as db:
        t = db.query(Teacher).filter_by(id=tid).first()
        if t:
            t.is_active = not t.is_active
            db.commit()
            status = "активирован" if t.is_active else "деактивирован"
            email_service.send_teacher_status_change(t.email, t.name, t.is_active)
            flash(request, f"Учитель {t.name} {status}", "success")
    return redir("/admin/teachers")


@app.post("/admin/teachers/{tid}/reset-password")
async def admin_reset_teacher_password(request: Request, tid: int,
                                        password: str = Form(...)):
    if g := guard_admin(request): return g
    with db_session() as db:
        t = db.query(Teacher).filter_by(id=tid).first()
        if t:
            t.password_hash = hash_password(password)
            db.commit()
            sent = email_service.send_teacher_password_reset(t.email, t.name, password)
            msg = f"Пароль учителя {t.name} изменён"
            if sent:
                msg += " (уведомление отправлено на email)"
            flash(request, msg, "success")
    return redir("/admin/teachers")


@app.post("/admin/teachers/{tid}/send-invite")
async def admin_send_invite(request: Request, tid: int):
    if g := guard_admin(request): return g
    with db_session() as db:
        t = db.query(Teacher).filter_by(id=tid).first()
        if t:
            new_pwd = generate_password()
            t.password_hash = hash_password(new_pwd)
            db.commit()
            sent = email_service.send_teacher_welcome(t.email, t.name, t.code, new_pwd)
            if sent:
                flash(request, f"Приглашение отправлено на {t.email}", "success")
            else:
                flash(request,
                      f"Email не настроен. Новый пароль для {t.name}: {new_pwd}", "warning")
    return redir("/admin/teachers")


@app.post("/admin/teachers/{tid}/delete")
async def admin_delete_teacher(request: Request, tid: int):
    if g := guard_admin(request): return g
    with db_session() as db:
        t = db.query(Teacher).filter_by(id=tid).first()
        if t:
            booklib.delete_book(t.id)
            db.delete(t)
            db.commit()
            flash(request, f"Учитель {t.name} удалён", "success")
    return redir("/admin/teachers")


# ── Admin: SMTP settings ───────────────────────────────────────────────────────

@app.get("/admin/settings", response_class=HTMLResponse)
async def admin_settings_get(request: Request):
    if g := guard_admin(request): return g
    return tpl(request, "admin/settings.html",
               {"s": smtp_settings.load()})


@app.post("/admin/settings")
async def admin_settings_post(request: Request,
                               smtp_host: str = Form(""),
                               smtp_port: int = Form(465),
                               smtp_user: str = Form(""),
                               smtp_password: str = Form(""),
                               smtp_tls: str = Form("ssl")):
    if g := guard_admin(request): return g
    smtp_settings.save(smtp_host, smtp_port, smtp_user, smtp_password, smtp_tls)
    flash(request, "Настройки почты сохранены", "success")
    return redir("/admin/settings")


@app.post("/admin/settings/change-password")
async def admin_change_password(request: Request,
                                 current_password: str = Form(...),
                                 new_password: str = Form(...),
                                 confirm_password: str = Form(...)):
    if g := guard_admin(request): return g
    with db_session() as db:
        admin = db.query(Admin).filter_by(id=request.session["admin_id"]).first()
        if not admin or not verify_password(current_password, admin.password_hash):
            flash(request, "Неверный текущий пароль", "error")
            return redir("/admin/settings")
        if new_password != confirm_password:
            flash(request, "Новые пароли не совпадают", "error")
            return redir("/admin/settings")
        if len(new_password) < 8:
            flash(request, "Пароль должен быть не менее 8 символов", "error")
            return redir("/admin/settings")
        admin.password_hash = hash_password(new_password)
        db.commit()
    flash(request, "Пароль администратора изменён", "success")
    return redir("/admin/settings")


@app.post("/admin/settings/test")
async def admin_settings_test(request: Request, test_email: str = Form(...)):
    if g := guard_admin(request): return g
    ok, msg = email_service.send_test(test_email)
    flash(request, msg, "success" if ok else "error")
    return redir("/admin/settings")


# ── Teacher: auth ──────────────────────────────────────────────────────────────

@app.get("/teacher/login", response_class=HTMLResponse)
async def teacher_login_get(request: Request):
    return tpl(request, "teacher/login.html")


@app.post("/teacher/login")
async def teacher_login_post(request: Request,
                              email: str = Form(...),
                              password: str = Form(...)):
    with db_session() as db:
        t = db.query(Teacher).filter_by(email=email).first()
        if not t or not verify_password(password, t.password_hash):
            flash(request, "Неверный email или пароль", "error")
            return redir("/teacher/login")
        if not t.is_active:
            flash(request, "Ваш аккаунт деактивирован. Обратитесь к администратору.", "error")
            return redir("/teacher/login")
        request.session["role"] = "teacher"
        request.session["teacher_id"] = t.id
        request.session["teacher_name"] = t.name
        request.session["teacher_code"] = t.code
    return redir("/teacher/")


@app.get("/teacher/logout")
async def teacher_logout(request: Request):
    request.session.clear()
    return redir("/")


# ── Teacher: dashboard ─────────────────────────────────────────────────────────

@app.get("/teacher/", response_class=HTMLResponse)
async def teacher_dashboard(request: Request):
    if g := guard_teacher(request): return g
    tid = request.session["teacher_id"]
    with db_session() as db:
        teacher = db.query(Teacher).filter_by(id=tid).first()
        total_groups = db.query(Group).filter_by(teacher_id=tid).count()
        total_students = (db.query(Student)
                          .join(Group)
                          .filter(Group.teacher_id == tid).count())
        total_trainers = db.query(Trainer).filter_by(teacher_id=tid).count()
        total_results = (db.query(TrainerResult)
                         .join(Student).join(Group)
                         .filter(Group.teacher_id == tid).count())
        recent = (db.query(TrainerResult)
                  .join(Student).join(Group)
                  .filter(Group.teacher_id == tid)
                  .options(joinedload(TrainerResult.student).joinedload(Student.group),
                           joinedload(TrainerResult.trainer))
                  .order_by(TrainerResult.completed_at.desc())
                  .limit(10).all())
    return tpl(request, "teacher/dashboard.html",
               {"teacher": teacher,
                "total_groups": total_groups, "total_students": total_students,
                "total_trainers": total_trainers, "total_results": total_results,
                "recent_results": recent})


# ── Teacher: groups & students ─────────────────────────────────────────────────

@app.get("/teacher/groups", response_class=HTMLResponse)
async def teacher_groups(request: Request):
    if g := guard_teacher(request): return g
    tid = request.session["teacher_id"]
    with db_session() as db:
        groups = (db.query(Group)
                  .filter_by(teacher_id=tid)
                  .options(joinedload(Group.students))
                  .order_by(Group.name).all())
    return tpl(request, "teacher/groups.html", {"groups": groups})


@app.post("/teacher/groups/add")
async def add_group(request: Request, name: str = Form(...)):
    if g := guard_teacher(request): return g
    tid = request.session["teacher_id"]
    name = name.strip()
    with db_session() as db:
        if db.query(Group).filter_by(teacher_id=tid, name=name).first():
            flash(request, f"Группа «{name}» уже существует", "error")
        else:
            db.add(Group(teacher_id=tid, name=name))
            db.commit()
            flash(request, f"Группа «{name}» создана", "success")
    return redir("/teacher/groups")


@app.post("/teacher/groups/{gid}/delete")
async def delete_group(request: Request, gid: int):
    if g := guard_teacher(request): return g
    tid = request.session["teacher_id"]
    with db_session() as db:
        grp = db.query(Group).filter_by(id=gid, teacher_id=tid).first()
        if grp:
            db.delete(grp)
            db.commit()
            flash(request, f"Группа «{grp.name}» удалена", "success")
    return redir("/teacher/groups")


@app.post("/teacher/groups/{gid}/transfer")
async def transfer_group(request: Request, gid: int,
                          teacher_code: str = Form(...)):
    if g := guard_teacher(request): return g
    my_id = request.session["teacher_id"]
    code  = teacher_code.strip()
    with db_session() as db:
        grp = db.query(Group).filter_by(id=gid, teacher_id=my_id).first()
        if not grp:
            flash(request, "Группа не найдена", "error")
            return redir("/teacher/groups")
        target = db.query(Teacher).filter_by(code=code).first()
        if not target:
            flash(request, f"Учитель с кодом «{code}» не найден", "error")
            return redir("/teacher/groups")
        if target.id == my_id:
            flash(request, "Нельзя передать самому себе", "error")
            return redir("/teacher/groups")
        if db.query(Group).filter_by(teacher_id=target.id, name=grp.name).first():
            flash(request, f"У учителя {target.name} уже есть группа «{grp.name}». Переименуйте перед передачей.", "error")
            return redir("/teacher/groups")
        grp_name = grp.name
        target_name = target.name
        # Remove cross-teacher trainer assignments for this group
        db.query(TrainerGroup).filter_by(group_id=gid).delete()
        grp.teacher_id = target.id
        db.commit()
        flash(request, f"Группа «{grp_name}» передана учителю {target_name}", "success")
    return redir("/teacher/groups")


@app.post("/teacher/students/add")
async def add_student(request: Request,
                       group_id: int = Form(...),
                       full_name: str = Form(...),
                       email: str = Form("")):
    if g := guard_teacher(request): return g
    tid = request.session["teacher_id"]
    full_name = full_name.strip()
    email = email.strip() or None
    with db_session() as db:
        grp = db.query(Group).filter_by(id=group_id, teacher_id=tid).first()
        if not grp:
            flash(request, "Группа не найдена", "error")
            return redir("/teacher/groups")
        if email and db.query(Student).filter_by(email=email).first():
            flash(request, f"Email {email} уже используется", "error")
            return redir("/teacher/groups")
        pwd = generate_password()
        s = Student(group_id=group_id, full_name=full_name, email=email, password=pwd)
        db.add(s)
        db.commit()
        teacher = db.query(Teacher).filter_by(id=tid).first()
        if email:
            sent = email_service.send_student_welcome(
                email, full_name, teacher.code, grp.name, pwd)
            msg = f"Ученик «{full_name}» добавлен. Пароль: {pwd}"
            if sent:
                msg += " (отправлен на email)"
            flash(request, msg, "success")
        else:
            flash(request, f"Ученик «{full_name}» добавлен. Пароль: {pwd}", "success")
    return redir("/teacher/groups")


@app.post("/teacher/students/{sid}/delete")
async def delete_student(request: Request, sid: int):
    if g := guard_teacher(request): return g
    tid = request.session["teacher_id"]
    with db_session() as db:
        s = (db.query(Student).join(Group)
             .filter(Student.id == sid, Group.teacher_id == tid).first())
        if s:
            db.delete(s)
            db.commit()
            flash(request, f"Ученик «{s.full_name}» удалён", "success")
    return redir("/teacher/groups")


@app.post("/teacher/students/{sid}/invite")
async def invite_student(request: Request, sid: int):
    if g := guard_teacher(request): return g
    teacher_id = request.session["teacher_id"]
    with db_session() as db:
        s = (db.query(Student)
             .options(joinedload(Student.group))
             .join(Group)
             .filter(Student.id == sid, Group.teacher_id == teacher_id).first())
        if not s or not s.email:
            flash(request, "Email ученика не указан", "error")
            return redir("/teacher/groups")
        teacher = db.query(Teacher).filter_by(id=teacher_id).first()
        sent = email_service.send_student_welcome(
            s.email, s.full_name, teacher.code, s.group.name, s.password)
        if sent:
            flash(request, f"Приглашение отправлено на {s.email}", "success")
        else:
            flash(request, "Почта не настроена — приглашение не отправлено", "error")
    return redir("/teacher/groups")


@app.post("/teacher/students/{sid}/regen")
async def regen_password(request: Request, sid: int):
    if g := guard_teacher(request): return g
    tid = request.session["teacher_id"]
    with db_session() as db:
        s = (db.query(Student).join(Group)
             .filter(Student.id == sid, Group.teacher_id == tid).first())
        if s:
            s.password = generate_password()
            db.commit()
            flash(request,
                  f"Новый пароль для «{s.full_name}»: {s.password}", "success")
    return redir("/teacher/groups")


@app.get("/teacher/groups/{gid}/export", response_class=HTMLResponse)
async def export_group(request: Request, gid: int):
    if g := guard_teacher(request): return g
    tid = request.session["teacher_id"]
    with db_session() as db:
        grp = (db.query(Group)
               .filter_by(id=gid, teacher_id=tid)
               .options(joinedload(Group.students))
               .first())
        if not grp:
            flash(request, "Группа не найдена", "error")
            return redir("/teacher/groups")
        teacher = db.query(Teacher).filter_by(id=tid).first()
    return tpl(request, "teacher/export.html",
               {"group": grp, "teacher": teacher})


# ── Teacher: trainers ──────────────────────────────────────────────────────────

@app.get("/teacher/trainers", response_class=HTMLResponse)
async def teacher_trainers(request: Request):
    if g := guard_teacher(request): return g
    tid = request.session["teacher_id"]
    with db_session() as db:
        trainers = (db.query(Trainer)
                    .filter_by(teacher_id=tid)
                    .options(joinedload(Trainer.sentences))
                    .order_by(Trainer.created_at.desc()).all())
    return tpl(request, "teacher/trainers.html", {"trainers": trainers})


@app.post("/teacher/trainers/add")
async def add_trainer(request: Request,
                       name: str = Form(...),
                       description: str = Form(""),
                       time_limit: int = Form(300),
                       level: str = Form("introductory"),
                       max_sentences: int = Form(0),
                       shuffle: Optional[str] = Form(None)):
    if g := guard_teacher(request): return g
    tid = request.session["teacher_id"]
    name = name.strip()
    if not name:
        flash(request, "Введите название тренажёра", "error")
        return redir("/teacher/trainers")
    with db_session() as db:
        t = Trainer(teacher_id=tid, name=name,
                    description=description.strip(), time_limit=time_limit,
                    level=level, max_sentences=max(0, max_sentences),
                    shuffle=(shuffle is not None))
        db.add(t)
        db.commit()
        db.refresh(t)
        new_id = t.id
    flash(request, f"Тренажёр «{name}» создан", "success")
    return redir(f"/teacher/trainers/{new_id}")


@app.get("/teacher/trainers/{tid_}", response_class=HTMLResponse)
async def trainer_detail(request: Request, tid_: int):
    if g := guard_teacher(request): return g
    teacher_id = request.session["teacher_id"]
    with db_session() as db:
        trainer = (db.query(Trainer)
                   .filter_by(id=tid_, teacher_id=teacher_id)
                   .options(joinedload(Trainer.sentences),
                            joinedload(Trainer.group_links))
                   .first())
        if not trainer:
            flash(request, "Тренажёр не найден", "error")
            return redir("/teacher/trainers")
        groups = db.query(Group).filter_by(teacher_id=teacher_id).order_by(Group.name).all()
        assigned_group_ids = {lg.group_id for lg in trainer.group_links}
    return tpl(request, "teacher/trainer_detail.html",
               {"trainer": trainer, "pos_colors": POS_COLORS,
                "book_loaded": booklib.book_exists(teacher_id),
                "groups": groups,
                "assigned_group_ids": assigned_group_ids})


@app.post("/teacher/trainers/{tid_}/share")
async def share_trainer(request: Request, tid_: int,
                         teacher_code: str = Form(...)):
    if g := guard_teacher(request): return g
    my_id = request.session["teacher_id"]
    code  = teacher_code.strip()
    with db_session() as db:
        trainer = (db.query(Trainer)
                   .filter_by(id=tid_, teacher_id=my_id)
                   .options(joinedload(Trainer.sentences))
                   .first())
        if not trainer:
            flash(request, "Тренажёр не найден", "error")
            return redir("/teacher/trainers")
        target = db.query(Teacher).filter_by(code=code).first()
        if not target:
            flash(request, f"Учитель с кодом «{code}» не найден", "error")
            return redir(f"/teacher/trainers/{tid_}")
        if target.id == my_id:
            flash(request, "Нельзя поделиться с самим собой", "error")
            return redir(f"/teacher/trainers/{tid_}")
        new_trainer = Trainer(
            teacher_id=target.id,
            name=trainer.name,
            description=trainer.description,
            time_limit=trainer.time_limit,
            level=trainer.level,
            max_sentences=trainer.max_sentences,
            shuffle=trainer.shuffle,
        )
        db.add(new_trainer)
        db.flush()
        for s in trainer.sentences:
            db.add(Sentence(
                trainer_id=new_trainer.id,
                text=s.text,
                order=s.order,
                correct_pos_json=s.correct_pos_json,
                status=s.status,
                analysis_json=s.analysis_json,
                teacher_analysis_json=s.teacher_analysis_json,
            ))
        target_name = target.name
        trainer_name = trainer.name
        db.commit()
        flash(request, f"Тренажёр «{trainer_name}» скопирован учителю {target_name}", "success")
    return redir(f"/teacher/trainers/{tid_}")


@app.post("/teacher/trainers/{tid_}/assign-groups")
async def assign_groups(request: Request, tid_: int):
    if g := guard_teacher(request): return g
    teacher_id = request.session["teacher_id"]
    form = await request.form()
    # group_ids is a multi-value list of checked group IDs
    group_ids = {int(v) for v in form.getlist("group_ids")}
    with db_session() as db:
        trainer = db.query(Trainer).filter_by(id=tid_, teacher_id=teacher_id).first()
        if not trainer:
            return redir("/teacher/trainers")
        # Verify groups belong to this teacher
        valid = {g.id for g in db.query(Group).filter_by(teacher_id=teacher_id).all()}
        group_ids &= valid
        # Replace all assignments
        db.query(TrainerGroup).filter_by(trainer_id=tid_).delete()
        for gid in group_ids:
            db.add(TrainerGroup(trainer_id=tid_, group_id=gid))
        db.commit()
        if group_ids:
            flash(request, f"Тренажёр назначен {len(group_ids)} группам", "success")
        else:
            flash(request, "Тренажёр доступен всем группам (без ограничений)", "success")
    return redir(f"/teacher/trainers/{tid_}")


@app.post("/teacher/trainers/{tid_}/update")
async def update_trainer(request: Request, tid_: int,
                          name: str = Form(...),
                          description: str = Form(""),
                          time_limit: int = Form(300),
                          level: str = Form("introductory"),
                          max_sentences: int = Form(0),
                          shuffle: Optional[str] = Form(None)):
    if g := guard_teacher(request): return g
    teacher_id = request.session["teacher_id"]
    with db_session() as db:
        t = db.query(Trainer).filter_by(id=tid_, teacher_id=teacher_id).first()
        if t:
            t.name = name.strip() or t.name
            t.description = description.strip()
            t.time_limit = time_limit
            t.level = level
            t.max_sentences = max(0, max_sentences)
            t.shuffle = shuffle is not None
            db.commit()
            flash(request, "Тренажёр обновлён", "success")
    return redir(f"/teacher/trainers/{tid_}")


@app.post("/teacher/trainers/{tid_}/delete")
async def delete_trainer(request: Request, tid_: int):
    if g := guard_teacher(request): return g
    teacher_id = request.session["teacher_id"]
    with db_session() as db:
        t = db.query(Trainer).filter_by(id=tid_, teacher_id=teacher_id).first()
        if t:
            db.delete(t)
            db.commit()
            flash(request, f"Тренажёр «{t.name}» удалён", "success")
    return redir("/teacher/trainers")


@app.post("/teacher/trainers/{tid_}/add-sentence")
async def add_sentence(request: Request, tid_: int, text: str = Form(...)):
    if g := guard_teacher(request): return g
    teacher_id = request.session["teacher_id"]
    text = text.strip()
    with db_session() as db:
        trainer = db.query(Trainer).filter_by(id=tid_, teacher_id=teacher_id).first()
        if not trainer:
            return redir("/teacher/trainers")
        if not text:
            flash(request, "Введите текст предложения", "error")
        else:
            level = trainer.level or LEVEL_INTRODUCTORY
            analysis = analyze_sentence(text)
            full = full_analyze(text, level=level)
            if not analysis:
                flash(request, "Предложение не содержит слов для анализа", "error")
            else:
                order = db.query(Sentence).filter_by(trainer_id=tid_).count()
                db.add(Sentence(
                    trainer_id=tid_, text=text, order=order,
                    correct_pos_json=json.dumps(analysis, ensure_ascii=False),
                    analysis_json=json.dumps(full, ensure_ascii=False),
                    status="analyzed",
                ))
                db.commit()
                flash(request, "Предложение добавлено", "success")
    return redir(f"/teacher/trainers/{tid_}")


@app.post("/teacher/sentences/{sid}/edit")
async def edit_sentence(request: Request, sid: int, text: str = Form(...)):
    if g := guard_teacher(request): return g
    teacher_id = request.session["teacher_id"]
    with db_session() as db:
        s = (db.query(Sentence).join(Trainer)
             .filter(Sentence.id == sid, Trainer.teacher_id == teacher_id)
             .options(joinedload(Sentence.trainer)).first())
        if s and text.strip():
            s.text = text.strip()
            level = s.trainer.level if s.trainer else LEVEL_INTRODUCTORY
            s.correct_pos_json = json.dumps(analyze_sentence(s.text), ensure_ascii=False)
            s.analysis_json = json.dumps(full_analyze(s.text, level=level), ensure_ascii=False)
            s.teacher_analysis_json = None  # reset teacher corrections on text change
            s.status = "analyzed"
            db.commit()
            flash(request, "Предложение обновлено", "success")
            return redir(f"/teacher/trainers/{s.trainer_id}")
    return redir("/teacher/trainers")


@app.post("/teacher/sentences/{sid}/delete")
async def delete_sentence(request: Request, sid: int):
    if g := guard_teacher(request): return g
    teacher_id = request.session["teacher_id"]
    with db_session() as db:
        s = (db.query(Sentence).join(Trainer)
             .filter(Sentence.id == sid, Trainer.teacher_id == teacher_id).first())
        if s:
            tid_ = s.trainer_id
            db.delete(s)
            db.commit()
            flash(request, "Предложение удалено", "success")
            return redir(f"/teacher/trainers/{tid_}")
    return redir("/teacher/trainers")


@app.get("/teacher/sentences/{sid}/analysis", response_class=HTMLResponse)
async def sentence_analysis_get(request: Request, sid: int):
    if g := guard_teacher(request): return g
    teacher_id = request.session["teacher_id"]
    with db_session() as db:
        s = (db.query(Sentence).join(Trainer)
             .filter(Sentence.id == sid, Trainer.teacher_id == teacher_id)
             .options(joinedload(Sentence.trainer)).first())
        if not s:
            flash(request, "Предложение не найдено", "error")
            return redir("/teacher/trainers")
        # Use teacher-corrected analysis if available, else AI analysis
        tokens = s.final_analysis
        ai_tokens = s.analysis
        trainer = s.trainer
    return tpl(request, "teacher/sentence_analysis.html", {
        "sentence":      s,
        "trainer":       trainer,
        "tokens":        tokens,
        "ai_tokens_json": json.dumps(ai_tokens, ensure_ascii=False),
        "pos_colors":    POS_COLORS,
        "pos_colors_json": json.dumps(POS_COLORS, ensure_ascii=False),
        "all_pos":       ALL_POS,
    })


@app.post("/teacher/sentences/{sid}/save-analysis")
async def sentence_analysis_save(request: Request, sid: int):
    if request.session.get("role") != "teacher":
        from fastapi.responses import JSONResponse
        return JSONResponse({"ok": False, "error": "Не авторизован"}, status_code=403)
    teacher_id = request.session["teacher_id"]
    body = await request.json()
    tokens = body.get("tokens", [])
    with db_session() as db:
        s = (db.query(Sentence).join(Trainer)
             .filter(Sentence.id == sid, Trainer.teacher_id == teacher_id).first())
        if not s:
            from fastapi.responses import JSONResponse
            return JSONResponse({"ok": False, "error": "Предложение не найдено"}, status_code=404)
        s.teacher_analysis_json = json.dumps(tokens, ensure_ascii=False)
        s.status = "reviewed"
        # Keep correct_pos_json in sync with teacher's POS choices
        pos_only = [{"word": t["word"], "pos": t["pos"], "index": t["index"]}
                    for t in tokens]
        s.correct_pos_json = json.dumps(pos_only, ensure_ascii=False)
        db.commit()
    return {"ok": True}


@app.post("/teacher/trainers/{tid_}/reanalyze")
async def reanalyze_trainer(request: Request, tid_: int):
    if g := guard_teacher(request): return g
    teacher_id = request.session["teacher_id"]
    with db_session() as db:
        trainer = (db.query(Trainer)
                   .filter_by(id=tid_, teacher_id=teacher_id)
                   .options(joinedload(Trainer.sentences)).first())
        if trainer:
            level = trainer.level or LEVEL_INTRODUCTORY
            for s in trainer.sentences:
                s.correct_pos_json = json.dumps(analyze_sentence(s.text), ensure_ascii=False)
                s.analysis_json = json.dumps(full_analyze(s.text, level=level), ensure_ascii=False)
                s.status = "analyzed"
            db.commit()
            flash(request,
                  f"Разбор пересчитан для {len(trainer.sentences)} предложений",
                  "success")
    return redir(f"/teacher/trainers/{tid_}")


@app.post("/teacher/trainers/{tid_}/analyze-all")
async def analyze_all_sentences(request: Request, tid_: int):
    """JSON endpoint: run full morphological analysis on all sentences
    of a trainer and persist results.  Returns {ok, count, errors}."""
    if request.session.get("role") != "teacher":
        from fastapi.responses import JSONResponse
        return JSONResponse({"ok": False, "error": "Не авторизован"}, status_code=403)
    teacher_id = request.session["teacher_id"]
    with db_session() as db:
        trainer = (db.query(Trainer)
                   .filter_by(id=tid_, teacher_id=teacher_id)
                   .options(joinedload(Trainer.sentences)).first())
        if not trainer:
            from fastapi.responses import JSONResponse
            return JSONResponse({"ok": False, "error": "Тренажёр не найден"}, status_code=404)
        level = trainer.level or LEVEL_INTRODUCTORY
        count = 0
        errors = []
        for s in trainer.sentences:
            try:
                tokens = full_analyze(s.text, level=level)
                pos_only = [{"word": t["word"], "pos": t["pos"], "index": t["index"]}
                            for t in tokens]
                s.analysis_json = json.dumps(tokens, ensure_ascii=False)
                s.correct_pos_json = json.dumps(pos_only, ensure_ascii=False)
                s.status = "analyzed"
                count += 1
            except Exception as exc:
                errors.append({"sentence_id": s.id, "error": str(exc)})
        db.commit()
    return {"ok": True, "count": count, "errors": errors}


# ── Teacher: book ──────────────────────────────────────────────────────────────

@app.get("/teacher/book", response_class=HTMLResponse)
async def teacher_book(request: Request):
    if g := guard_teacher(request): return g
    tid = request.session["teacher_id"]
    return tpl(request, "teacher/book.html",
               {"info": booklib.book_info(tid)})


@app.post("/teacher/book/upload")
async def upload_book(request: Request, book_file: UploadFile = File(...)):
    if g := guard_teacher(request): return g
    tid = request.session["teacher_id"]
    fname = book_file.filename.lower()
    if not (fname.endswith(".txt") or fname.endswith(".pdf")):
        flash(request, "Поддерживаются только .txt и .pdf файлы", "error")
        return redir("/teacher/book")
    try:
        count = booklib.save_book(tid, book_file.file, fname)
        flash(request, f"Книга загружена: найдено {count} предложений", "success")
    except Exception as e:
        flash(request, f"Ошибка обработки файла: {e}", "error")
    return redir("/teacher/book")


@app.post("/teacher/book/delete")
async def delete_book_route(request: Request):
    if g := guard_teacher(request): return g
    booklib.delete_book(request.session["teacher_id"])
    flash(request, "Книга удалена", "success")
    return redir("/teacher/book")


@app.get("/teacher/api/debug-analyze")
async def debug_analyze(request: Request, text: str = ""):
    """Dev helper: run full_analyze on a sentence and return raw result + flags."""
    if request.session.get("role") != "teacher":
        return {"error": "Не авторизован"}
    from morpho import NATASHA_AVAILABLE, MORPH_AVAILABLE
    tokens = full_analyze(text, "advanced") if text else []
    return {
        "natasha_available": NATASHA_AVAILABLE,
        "morph_available":   MORPH_AVAILABLE,
        "text":   text,
        "tokens": tokens,
    }


@app.get("/teacher/api/random-sentence")
async def api_random_sentence(request: Request,
                               min_words: int = 5, max_words: int = 15):
    if request.session.get("role") != "teacher":
        return {"error": "Не авторизован", "sentence": None}
    tid = request.session["teacher_id"]
    if min_words > max_words:
        return {"error": "Минимум не может быть больше максимума", "sentence": None}
    s = booklib.get_random_sentence(tid, min_words, max_words)
    if s is None:
        return {"error": "Нет подходящих предложений в книге", "sentence": None}
    return {"sentence": s}


# ── Teacher: stats ─────────────────────────────────────────────────────────────

@app.get("/teacher/stats", response_class=HTMLResponse)
async def teacher_stats(request: Request):
    if g := guard_teacher(request): return g
    teacher_id = request.session["teacher_id"]

    group_id = request.query_params.get("group_id", "")
    student_id = request.query_params.get("student_id", "")
    trainer_id = request.query_params.get("trainer_id", "")
    date_from = request.query_params.get("date_from", "")
    date_to = request.query_params.get("date_to", "")

    with db_session() as db:
        groups = (db.query(Group).filter_by(teacher_id=teacher_id)
                  .order_by(Group.name).all())
        trainers = (db.query(Trainer).filter_by(teacher_id=teacher_id)
                    .order_by(Trainer.name).all())

        q = (db.query(TrainerResult)
             .join(Student).join(Group)
             .filter(Group.teacher_id == teacher_id)
             .options(joinedload(TrainerResult.student).joinedload(Student.group),
                      joinedload(TrainerResult.trainer)))

        if group_id:
            q = q.filter(Group.id == int(group_id))
        if student_id:
            q = q.filter(TrainerResult.student_id == int(student_id))
        if trainer_id:
            q = q.filter(TrainerResult.trainer_id == int(trainer_id))
        if date_from:
            q = q.filter(TrainerResult.completed_at >= date_from)
        if date_to:
            q = q.filter(TrainerResult.completed_at <= date_to + " 23:59:59")

        results = q.order_by(TrainerResult.completed_at.desc()).all()

        students_for_group = []
        if group_id:
            students_for_group = (db.query(Student)
                                  .filter_by(group_id=int(group_id))
                                  .order_by(Student.full_name).all())

    return tpl(request, "teacher/stats.html",
               {"results": results, "groups": groups, "trainers": trainers,
                "students_for_group": students_for_group,
                "sel_group": group_id, "sel_student": student_id,
                "sel_trainer": trainer_id,
                "date_from": date_from, "date_to": date_to})


@app.get("/teacher/stats/export.csv")
async def teacher_stats_csv(request: Request):
    if g := guard_teacher(request): return g
    teacher_id = request.session["teacher_id"]

    group_id   = request.query_params.get("group_id", "")
    student_id = request.query_params.get("student_id", "")
    trainer_id = request.query_params.get("trainer_id", "")
    date_from  = request.query_params.get("date_from", "")
    date_to    = request.query_params.get("date_to", "")

    with db_session() as db:
        q = (db.query(TrainerResult)
             .join(Student).join(Group)
             .filter(Group.teacher_id == teacher_id)
             .options(joinedload(TrainerResult.student).joinedload(Student.group),
                      joinedload(TrainerResult.trainer)))
        if group_id:
            q = q.filter(Group.id == int(group_id))
        if student_id:
            q = q.filter(TrainerResult.student_id == int(student_id))
        if trainer_id:
            q = q.filter(TrainerResult.trainer_id == int(trainer_id))
        if date_from:
            q = q.filter(TrainerResult.completed_at >= date_from)
        if date_to:
            q = q.filter(TrainerResult.completed_at <= date_to + " 23:59:59")
        results = q.order_by(TrainerResult.completed_at.desc()).all()

        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["ID", "Ученик", "Группа", "Тренажёр", "Уровень",
                    "Звёзды", "Макс. звёзды", "Результат %", "Дата"])
        for r in results:
            level_label = LEVEL_LABELS.get(r.trainer.level, r.trainer.level)
            w.writerow([
                r.id,
                r.student.full_name,
                r.student.group.name,
                r.trainer.name,
                level_label,
                r.total_stars,
                r.max_stars,
                r.percentage,
                r.completed_at.strftime("%d.%m.%Y %H:%M"),
            ])

    content = "﻿" + buf.getvalue()  # BOM for Excel UTF-8
    return Response(
        content=content.encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=\"results.csv\""},
    )


@app.get("/teacher/results/{rid}", response_class=HTMLResponse)
async def teacher_result_detail(request: Request, rid: int):
    if g := guard_teacher(request): return g
    teacher_id = request.session["teacher_id"]
    with db_session() as db:
        r = (db.query(TrainerResult).join(Student).join(Group)
             .filter(TrainerResult.id == rid, Group.teacher_id == teacher_id)
             .options(joinedload(TrainerResult.student).joinedload(Student.group),
                      joinedload(TrainerResult.trainer))
             .first())
        if not r:
            flash(request, "Результат не найден", "error")
            return redir("/teacher/stats")
        details = json.loads(r.details_json or "[]")
    return tpl(request, "teacher/result_detail.html",
               {"result": r, "details": details, "pos_colors": POS_COLORS})


@app.post("/teacher/results/{rid}/delete")
async def delete_result(request: Request, rid: int):
    if g := guard_teacher(request): return g
    teacher_id = request.session["teacher_id"]
    with db_session() as db:
        r = (db.query(TrainerResult).join(Student).join(Group)
             .filter(TrainerResult.id == rid, Group.teacher_id == teacher_id)
             .first())
        if r:
            db.delete(r)
            db.commit()
            flash(request, "Результат удалён", "success")
    return redir("/teacher/stats")


@app.get("/teacher/api/students-by-group")
async def teacher_students_by_group(request: Request, group_id: int = 0):
    if request.session.get("role") != "teacher":
        return []
    teacher_id = request.session["teacher_id"]
    with db_session() as db:
        grp = db.query(Group).filter_by(id=group_id,
                                         teacher_id=teacher_id).first()
        if not grp:
            return []
        students = (db.query(Student).filter_by(group_id=group_id)
                    .order_by(Student.full_name).all())
    return [{"id": s.id, "name": s.full_name} for s in students]


@app.post("/teacher/api/word-features")
async def teacher_word_features(request: Request):
    if request.session.get("role") != "teacher":
        return {"ok": False}
    body = await request.json()
    word = (body.get("word") or "").strip()
    pos  = (body.get("pos")  or "").strip()
    if not word or not pos:
        return {"ok": False, "error": "word and pos required"}
    result = analyze_word_as_pos(word, pos)
    return {"ok": True, **result}


@app.post("/teacher/reset-sessions")
async def reset_sessions(request: Request):
    if g := guard_teacher(request): return g
    teacher_id = request.session["teacher_id"]
    with db_session() as db:
        (db.query(Student)
         .filter(Student.group_id.in_(
             db.query(Group.id).filter_by(teacher_id=teacher_id)))
         .update({"is_online": False}, synchronize_session=False))
        db.commit()
    flash(request, "Все сессии учеников сброшены", "success")
    return redir("/teacher/")


# ── Student: auth ──────────────────────────────────────────────────────────────

@app.get("/student/login", response_class=HTMLResponse)
async def student_login_get(request: Request):
    return tpl(request, "student/login.html")


@app.get("/student/api/groups-by-code")
async def groups_by_code(request: Request, code: str = ""):
    with db_session() as db:
        t = db.query(Teacher).filter_by(code=code, is_active=True).first()
        if not t:
            return {"teacher": None, "groups": []}
        groups = (db.query(Group).filter_by(teacher_id=t.id)
                  .order_by(Group.name).all())
    return {
        "teacher": {"id": t.id, "name": t.name, "code": t.code},
        "groups": [{"id": g.id, "name": g.name} for g in groups],
    }


@app.get("/student/api/students-by-group")
async def students_by_group(request: Request, group_id: int = 0):
    with db_session() as db:
        students = (db.query(Student).filter_by(group_id=group_id)
                    .order_by(Student.full_name).all())
    return [{"id": s.id, "name": s.full_name} for s in students]


@app.post("/student/login")
async def student_login_post(request: Request,
                              teacher_code: str = Form(...),
                              student_id: int = Form(...),
                              password: str = Form(...)):
    with db_session() as db:
        t = db.query(Teacher).filter_by(code=teacher_code, is_active=True).first()
        if not t:
            flash(request, "Неверный код учителя", "error")
            return redir("/student/login")
        s = (db.query(Student).join(Group)
             .filter(Student.id == student_id, Group.teacher_id == t.id).first())
        if not s:
            flash(request, "Ученик не найден", "error")
            return redir("/student/login")
        if s.password != password:
            flash(request, "Неверный пароль", "error")
            return redir("/student/login")
        if s.is_online:
            flash(request, "Предыдущая сессия сброшена. Добро пожаловать!", "warning")
        s.is_online = True
        db.commit()
        request.session["role"] = "student"
        request.session["student_id"] = s.id
        request.session["student_name"] = s.full_name
        request.session["teacher_code"] = t.code
    return redir("/student/")


@app.get("/student/logout")
async def student_logout(request: Request):
    sid = request.session.get("student_id")
    if sid:
        with db_session() as db:
            s = db.query(Student).filter_by(id=sid).first()
            if s:
                s.is_online = False
                db.commit()
    request.session.clear()
    return redir("/")


# ── Student: forgot password ───────────────────────────────────────────────────

@app.get("/student/forgot-password", response_class=HTMLResponse)
async def forgot_password_get(request: Request):
    return tpl(request, "student/forgot_password.html")


@app.post("/student/forgot-password")
async def forgot_password_post(request: Request,
                                teacher_code: str = Form(...),
                                email: str = Form(...)):
    with db_session() as db:
        t = db.query(Teacher).filter_by(code=teacher_code, is_active=True).first()
        if not t:
            flash(request, "Код учителя не найден", "error")
            return redir("/student/forgot-password")
        s = (db.query(Student).join(Group)
             .filter(Student.email == email.strip(),
                     Group.teacher_id == t.id).first())
        if not s:
            flash(request,
                  "Ученик с таким email не найден в группах этого учителя", "error")
            return redir("/student/forgot-password")
        new_pwd = generate_password()
        s.password = new_pwd
        db.commit()
        sent = email_service.send_password_reset(email, s.full_name, t.code, new_pwd)
        if sent:
            flash(request, "Новый пароль отправлен на email", "success")
        else:
            flash(request, f"Новый пароль: {new_pwd} (email не настроен)", "info")
    return redir("/student/login")


# ── Student: dashboard ─────────────────────────────────────────────────────────

@app.get("/student/", response_class=HTMLResponse)
async def student_dashboard(request: Request):
    if g := guard_student(request): return g
    sid = request.session["student_id"]
    with db_session() as db:
        student = (db.query(Student).options(joinedload(Student.group)).filter_by(id=sid).first())
        teacher_id = student.group.teacher_id
        group_id   = student.group_id

        # Trainers available to this student:
        # — trainers explicitly assigned to their group, OR
        # — trainers with NO group assignments at all (available to everyone)
        from sqlalchemy import exists, and_
        assigned_to_group = (db.query(Trainer)
                               .filter_by(teacher_id=teacher_id)
                               .join(TrainerGroup,
                                     and_(TrainerGroup.trainer_id == Trainer.id,
                                          TrainerGroup.group_id == group_id))
                               .options(joinedload(Trainer.sentences))
                               .all())
        unassigned = (db.query(Trainer)
                        .filter_by(teacher_id=teacher_id)
                        .filter(~exists().where(TrainerGroup.trainer_id == Trainer.id))
                        .options(joinedload(Trainer.sentences))
                        .all())
        trainers = sorted(assigned_to_group + unassigned, key=lambda t: t.name)
        recent = (db.query(TrainerResult)
                  .filter_by(student_id=sid)
                  .options(joinedload(TrainerResult.trainer))
                  .order_by(TrainerResult.completed_at.desc())
                  .limit(5).all())
    return tpl(request, "student/dashboard.html",
               {"student": student, "trainers": trainers, "recent": recent})


# ── Student: exercise ──────────────────────────────────────────────────────────

@app.get("/student/exercise/{tid_}", response_class=HTMLResponse)
async def student_exercise(request: Request, tid_: int):
    if g := guard_student(request): return g
    sid = request.session["student_id"]
    with db_session() as db:
        student = db.query(Student).options(joinedload(Student.group)).filter_by(id=sid).first()
        trainer = (db.query(Trainer)
                   .filter_by(id=tid_, teacher_id=student.group.teacher_id)
                   .options(joinedload(Trainer.sentences)).first())
        if not trainer:
            flash(request, "Тренажёр не найден", "error")
            return redir("/student/")
        if not trainer.sentences:
            flash(request, "В этом тренажёре пока нет предложений", "error")
            return redir("/student/")

        level = trainer.level or LEVEL_INTRODUCTORY
        req_fields = LEVEL_REQUIRED_FIELDS.get(level, ["pos"])
        sentences_data = []

        sentences = list(trainer.sentences)
        if trainer.shuffle:
            random.shuffle(sentences)
        limit = trainer.max_sentences or len(sentences)
        sentences = sentences[:limit]

        for s in sentences:
            tokens = s.final_analysis if level != LEVEL_INTRODUCTORY else s.correct_pos
            words = []
            for i, tok in enumerate(tokens):
                w: dict = {
                    "word":        tok["word"] if isinstance(tok, dict) else tok["word"],
                    "index":       i,
                    "correct_pos": tok.get("pos", "") if isinstance(tok, dict) else tok["pos"],
                }
                if level != LEVEL_INTRODUCTORY and isinstance(tok, dict):
                    w["correct_lemma"]  = tok.get("lemma", "")
                    w["correct_var"]    = tok.get("var_features", {})
                    w["correct_const"]  = tok.get("const_features", {})
                    w["correct_syntax"] = tok.get("syntax_role", "")
                words.append(w)
            sentences_data.append({"id": s.id, "text": s.text, "words": words})

    ctx = {
        "trainer":          trainer,
        "level":            level,
        "req_fields":       req_fields,
        "sentences_json":   json.dumps(sentences_data, ensure_ascii=False),
        "pos_colors_json":  json.dumps(POS_COLORS, ensure_ascii=False),
        "all_pos":          ALL_POS,
        "pos_colors":       POS_COLORS,
    }
    template = "student/exercise.html" if level == LEVEL_INTRODUCTORY else "student/exercise_full.html"
    return tpl(request, template, ctx)


def _score_word(tok: dict, ans: dict, level: str) -> tuple[int, int, dict]:
    """Score one word. Returns (1, 1, detail) if ALL required fields correct, else (0, 1, detail).
    field_results is always populated for per-field feedback display."""
    req    = LEVEL_REQUIRED_FIELDS.get(level, ["pos"])
    pos_ru = tok.get("pos", "")
    detail: dict = {
        "word":          tok.get("word", ""),
        "correct_pos":   pos_ru,
        "student_pos":   ans.get("pos", "") if isinstance(ans, dict) else str(ans),
        "correct":       False,
        "field_results": [],
    }

    def _chk(field: str, correct_val, student_val) -> bool:
        ok = str(correct_val).strip().lower() == str(student_val or "").strip().lower()
        detail["field_results"].append({"field": field, "ok": ok,
                                        "correct": correct_val, "student": student_val or ""})
        return ok

    student_pos = ans.get("pos", "") if isinstance(ans, dict) else str(ans)
    all_ok = _chk("pos", pos_ru, student_pos)

    INVARIABLE  = {"Предлог", "Союз", "Частица", "Наречие", "Междометие"}
    SERVICE_POS = {"Предлог", "Союз", "Частица", "Междометие"}
    if "lemma" in req and pos_ru not in INVARIABLE:
        detail["correct_lemma"] = tok.get("lemma", "")
        detail["student_lemma"] = ans.get("lemma", "") if isinstance(ans, dict) else ""
        all_ok &= _chk("lemma", tok.get("lemma", ""), detail["student_lemma"])

    if "var_features" in req:
        correct_var  = tok.get("var_features", {})
        student_var  = ans.get("var_features", {}) if isinstance(ans, dict) else {}
        detail["correct_var"] = correct_var
        detail["student_var"] = student_var
        for key in SCORED_VAR_FEATURES.get(pos_ru, []):
            if key in correct_var:
                all_ok &= _chk(f"var:{key}", correct_var[key], student_var.get(key, ""))

    if "const_features" in req:
        correct_const = tok.get("const_features", {})
        student_const = ans.get("const_features", {}) if isinstance(ans, dict) else {}
        detail["correct_const"] = correct_const
        detail["student_const"] = student_const
        for key in SCORED_CONST_FEATURES.get(pos_ru, []):
            if key in correct_const:
                all_ok &= _chk(f"const:{key}", correct_const[key], student_const.get(key, ""))

    if "syntax_role" in req and pos_ru not in SERVICE_POS:
        detail["correct_syntax"] = tok.get("syntax_role", "")
        detail["student_syntax"] = ans.get("syntax_role", "") if isinstance(ans, dict) else ""
        all_ok &= _chk("syntax_role", detail["correct_syntax"], detail["student_syntax"])

    detail["correct"] = bool(all_ok)
    correct_fields = sum(1 for fr in detail["field_results"] if fr["ok"])
    total_fields = len(detail["field_results"])
    return correct_fields, total_fields, detail


@app.post("/student/submit-exercise")
async def submit_exercise(request: Request):
    if request.session.get("role") != "student":
        return {"error": "not authenticated"}
    data = await request.json()
    trainer_id = data.get("trainer_id")
    results_data = data.get("results", [])
    total_stars = max_stars = 0
    verified = []
    with db_session() as db:
        trainer = db.query(Trainer).filter_by(id=trainer_id).first()
        level = (trainer.level or LEVEL_INTRODUCTORY) if trainer else LEVEL_INTRODUCTORY

        for r in results_data:
            sentence = db.query(Sentence).filter_by(id=r.get("sentence_id")).first()
            if not sentence:
                continue
            tokens = sentence.final_analysis if level != LEVEL_INTRODUCTORY else []
            # Fall back to correct_pos for introductory
            if not tokens or level == LEVEL_INTRODUCTORY:
                tokens = [{"word": it["word"], "pos": it["pos"],
                           "lemma": "", "var_features": {}, "const_features": {},
                           "syntax_role": ""}
                          for it in sentence.correct_pos]

            stars = word_max = 0
            word_details = []
            answers = r.get("answers", {})
            for i, tok in enumerate(tokens):
                ans = answers.get(str(i), {})
                ws, wm, detail = _score_word(tok, ans, level)
                stars += ws
                word_max += wm
                word_details.append(detail)

            total_stars += stars
            max_stars += word_max
            verified.append({
                "sentence_id":   sentence.id,
                "sentence_text": sentence.text,
                "stars":         stars,
                "max_stars":     word_max,
                "level":         level,
                "word_details":  word_details,
            })

        pct = round(total_stars / max_stars * 100, 1) if max_stars else 0.0
        tr = TrainerResult(student_id=request.session["student_id"],
                           trainer_id=trainer_id,
                           total_stars=total_stars, max_stars=max_stars,
                           percentage=pct,
                           details_json=json.dumps(verified, ensure_ascii=False))
        db.add(tr)
        db.commit()
        db.refresh(tr)
        return {"result_id": tr.id}


# ── Student: results & stats ───────────────────────────────────────────────────

@app.get("/student/results/{rid}", response_class=HTMLResponse)
async def student_results(request: Request, rid: int):
    if g := guard_student(request): return g
    sid = request.session["student_id"]
    with db_session() as db:
        result = (db.query(TrainerResult)
                  .filter_by(id=rid, student_id=sid)
                  .options(joinedload(TrainerResult.trainer))
                  .first())
        if not result:
            return redir("/student/")
        details = json.loads(result.details_json or "[]")
    return tpl(request, "student/results.html",
               {"result": result, "details": details, "pos_colors": POS_COLORS})


@app.get("/student/stats", response_class=HTMLResponse)
async def student_stats(request: Request):
    if g := guard_student(request): return g
    sid = request.session["student_id"]
    with db_session() as db:
        results = (db.query(TrainerResult)
                   .filter_by(student_id=sid)
                   .options(joinedload(TrainerResult.trainer))
                   .order_by(TrainerResult.completed_at.desc()).all())
    return tpl(request, "student/stats.html", {"results": results})
