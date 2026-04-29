import random
import string
from passlib.context import CryptContext
from fastapi import Request
from fastapi.responses import RedirectResponse

pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(plain: str) -> str:
    return pwd_ctx.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_ctx.verify(plain, hashed)


def generate_password(length: int = 8) -> str:
    chars = string.ascii_letters + string.digits
    return "".join(random.choices(chars, k=length))


def generate_teacher_code(db) -> str:
    from models import Teacher
    while True:
        code = "".join(random.choices(string.digits, k=6))
        if not db.query(Teacher).filter_by(code=code).first():
            return code


# ── Session helpers ───────────────────────────────────────────────────────────

def flash(request: Request, message: str, category: str = "info") -> None:
    request.session.setdefault("_flashes", []).append([category, message])


def pop_flashes(request: Request) -> list:
    msgs = request.session.get("_flashes", [])
    if msgs:
        request.session["_flashes"] = []
    return msgs


def redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url=url, status_code=303)


# ── Auth guards ───────────────────────────────────────────────────────────────

def require_admin(request: Request):
    if request.session.get("role") != "admin":
        return redirect("/admin/login")
    return None


def require_teacher(request: Request):
    if request.session.get("role") != "teacher":
        return redirect("/teacher/login")
    return None


def require_student(request: Request):
    if request.session.get("role") != "student":
        return redirect("/student/login")
    return None
