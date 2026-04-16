from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
import os

# Читаем строку подключения из переменной окружения
# Формат: postgresql://пользователь:пароль@хост:порт/база
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://postgres:postgres@db:5432/fitness"
)

# Движок — главный объект, который знает как говорить с PostgreSQL
engine = create_engine(DATABASE_URL)

# Фабрика сессий — каждая сессия это одно «соединение» с базой
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)


# Функция-генератор для dependency injection в FastAPI
def get_db():
    # Открываем сессию
    db = SessionLocal()
    try:
        # Возвращаем сессию роуту
        yield db
    finally:
        # Закрываем сессию после запроса — всегда, даже при ошибке
        db.close()
