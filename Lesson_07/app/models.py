"""
app/models.py — SQLAlchemy-модели (описания таблиц БД).

Эта версия модели соответствует состоянию после всех трёх миграций:
  - 001_init:             id, author, content, created_at
  - 002_add_radiation:    + radiation_level
  - 003_add_mission_type: + mission_type

Alembic сравнивает эту модель с реальной БД при --autogenerate.
"""

from sqlalchemy import Column, Integer, String, Text, DateTime, Float
#                                                                   ↑ Float добавили в миграции 002
from sqlalchemy.orm import declarative_base     # Базовый класс для всех моделей
from datetime import datetime                   # Для работы с датой и временем

# ─── Базовый класс ────────────────────────────────────────────────────────────
# Base — родитель для всех наших таблиц.
# Он хранит метаданные (Base.metadata), которые читает Alembic.
Base = declarative_base()                       # Создаём базовый класс


# ─── Модель: запись в бортовом журнале ───────────────────────────────────────
class LogEntry(Base):
    """
    Одна запись в бортовом журнале МКС.

    История изменений схемы:
    - v1 (001_init):             id, author, content, created_at
    - v2 (002_add_radiation):    добавили radiation_level
    - v3 (003_add_mission_type): добавили mission_type
    """

    __tablename__ = 'log_entries'               # Имя таблицы в PostgreSQL

    # ── Поля версии 1 ─────────────────────────────────────────────────────────
    id = Column(
        Integer,                                # Тип: целое число
        primary_key=True,                       # Первичный ключ — уникальный ID
        autoincrement=True                      # PostgreSQL сам увеличивает значение
    )

    author = Column(
        String(100),                            # Строка до 100 символов
        nullable=False                          # Обязательное поле — автор должен быть указан
    )

    content = Column(
        Text,                                   # Текст неограниченной длины
        nullable=False                          # Обязательное поле — текст записи
    )

    created_at = Column(
        DateTime,                               # Дата и время
        default=datetime.utcnow,                # По умолчанию: момент создания объекта
        nullable=True                           # Разрешаем NULL (заполняется в коде)
    )

    # ── Поле версии 2 (миграция 002_add_radiation) ────────────────────────────
    radiation_level = Column(
        Float,                                  # Дробное число (мкЗв/ч — микрозиверты в час)
        nullable=True,                          # NULL разрешён: старые записи без значения
        default=0.0                             # По умолчанию — 0 (нет данных о радиации)
    )

    # ── Поле версии 3 (миграция 003_add_mission_type) ─────────────────────────
    mission_type = Column(
        String(20),                             # Короткая строка: 'science', 'repair' и т.д.
        nullable=False,                         # Обязательное поле
        default='other',                        # По умолчанию: «прочее»
        server_default='other'                  # PostgreSQL ставит 'other' для старых строк
    )

    def __repr__(self):
        """Строковое представление объекта — удобно для отладки."""
        return f"<LogEntry id={self.id} author='{self.author}' type='{self.mission_type}'>"
