from sqlalchemy import Column, Integer, String, Float, Text, DateTime
from sqlalchemy.orm import DeclarativeBase
from datetime import datetime


# Базовый класс для всех моделей — SQLAlchemy требует этого
class Base(DeclarativeBase):
    pass


# Модель спортсмена — описывает таблицу в базе данных
class Athlete(Base):
    # Имя таблицы в PostgreSQL
    __tablename__ = "athletes"

    # Уникальный идентификатор — автоматически увеличивается
    id = Column(Integer, primary_key=True, autoincrement=True)

    # Полное имя спортсмена
    name = Column(String(100), nullable=False)

    # Вид спорта / дисциплина
    sport = Column(String(50), nullable=False)

    # Возраст спортсмена
    age = Column(Integer, nullable=False)

    # Вес в килограммах
    weight = Column(Float, nullable=False)

    # Рост в сантиметрах
    height = Column(Float, nullable=False)

    # Уровень подготовки: beginner / intermediate / advanced
    level = Column(String(20), nullable=False, default="beginner")

    # Цели тренировок — произвольный текст
    goals = Column(Text, nullable=True)

    # Дата регистрации в клубе
    created_at = Column(DateTime, default=datetime.utcnow)
