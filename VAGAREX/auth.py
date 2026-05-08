"""
auth.py — простая аутентификация пользователей VEGAREX.

— passlib (pbkdf2_sha256) для хеширования пароля.
  Раньше использовался bcrypt, но passlib 1.7.4 несовместим с bcrypt ≥4.1
  из-за их теста backend паролем >72 байт. pbkdf2_sha256 — pure Python,
  без нативных зависимостей и без ограничения длины пароля.
— starlette SessionMiddleware хранит user_id в подписанной cookie
— get_current_user() читает user_id из request.session
"""
from typing import Optional

from fastapi import Depends, HTTPException, Request, status
from passlib.context import CryptContext
from sqlalchemy.orm import Session

from database import get_db
from models import User

pwd_context = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")


def hash_password(plain: str) -> str:
    return pwd_context.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return pwd_context.verify(plain, hashed)
    except Exception:
        return False


def login_user(request: Request, user: User) -> None:
    """Сохраняет user_id в cookie-сессии."""
    request.session["user_id"] = user.id


def logout_user(request: Request) -> None:
    request.session.pop("user_id", None)


def get_current_user(request: Request, db: Session = Depends(get_db)) -> Optional[User]:
    """Возвращает текущего пользователя или None, если не залогинен.
    Используется на страницах, где user может отсутствовать (login/register)."""
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    return db.query(User).filter(User.id == user_id).first()


def require_user(request: Request, db: Session = Depends(get_db)) -> User:
    """Жесткая зависимость: нужен залогиненный И существующий пользователь.
    Если cookie протухла (DB чистая, а cookie от прошлой жизни) —
    очищает сессию и редиректит на /login."""
    user_id = request.session.get("user_id")
    if user_id:
        user = db.query(User).filter(User.id == user_id).first()
        if user:
            return user
        # cookie указывает на несуществующего юзера — чистим
        request.session.pop("user_id", None)
    raise HTTPException(
        status_code=status.HTTP_303_SEE_OTHER,
        detail="Login required",
        headers={"Location": "/login"},
    )


def require_admin(user: User = Depends(require_user)) -> User:
    """Зависимость для админских ручек. Возвращает User или 403."""
    if not user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Доступ только для администратора.",
        )
    return user
