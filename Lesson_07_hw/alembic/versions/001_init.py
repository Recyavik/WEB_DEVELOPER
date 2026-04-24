"""init weather_records table

Revision ID: 001_init
Revises:
Create Date: 2026-04-24
"""
# ─── Миграция 001: создание таблицы weather_records ────────────────────────
# Это ПЕРВАЯ миграция — она создаёт таблицу с нуля.
# Каждая миграция — отдельный файл с двумя функциями:
#   upgrade()   — применить изменение (двигаться «вперёд»)
#   downgrade() — отменить изменение (двигаться «назад»)

# Union — тип из typing, означает «одно из»: Union[str, None] = либо строка, либо None.
# Sequence — тип из typing, означает «последовательность» (список, кортеж и т.д.).
from typing import Sequence, Union

# op — объект операций Alembic: create_table, add_column, drop_table и т.д.
from alembic import op

# sa — псевдоним для sqlalchemy. Используем sa.Column, sa.Integer и т.д.
import sqlalchemy as sa

# ─── Идентификаторы миграции ────────────────────────────────────────────────

# revision — уникальный ID этой миграции.
# Alembic использует его для отслеживания: до какой версии дошла БД.
# Сохраняется в таблице alembic_version в самой БД.
revision: str = '001_init'

# down_revision — ID предыдущей миграции (от которой эта зависит).
# None означает: это первая миграция, предшественника нет.
# Именно эта переменная строит цепочку: 001 → 002 → 003 ...
down_revision: Union[str, None] = None

# branch_labels — метки для разветвлённых историй миграций.
# None — мы используем простую линейную цепочку, ветвления нет.
branch_labels: Union[str, Sequence[str], None] = None

# depends_on — зависимость от другой миграции (не по цепочке).
# None — нет дополнительных зависимостей.
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Применяем миграцию: создаём таблицу weather_records."""

    # op.create_table — создаёт новую таблицу в базе данных.
    # Первый аргумент — имя таблицы в БД (строка).
    # Следующие аргументы — описание колонок через sa.Column().
    op.create_table(
        'weather_records',  # Имя таблицы в PostgreSQL

        # sa.Column(имя, тип, параметры) — описывает одну колонку таблицы.

        # id — первичный ключ: уникальный номер каждой записи.
        # sa.Integer() — целое число (1, 2, 3, ...).
        # nullable=False — значение ОБЯЗАТЕЛЬНО (не может быть NULL/пустым).
        sa.Column('id', sa.Integer(), nullable=False),

        # city — название города, строка максимум 100 символов.
        # sa.String(100) — VARCHAR(100) в PostgreSQL.
        # nullable=False — город нельзя не указать.
        sa.Column('city', sa.String(100), nullable=False),

        # temperature — температура в градусах Цельсия.
        # sa.Float() — число с плавающей точкой (например: -5.3, 23.7).
        # nullable=False — температура обязательна.
        sa.Column('temperature', sa.Float(), nullable=False),

        # recorded_at — дата и время наблюдения.
        # sa.DateTime() — тип TIMESTAMP в PostgreSQL.
        # nullable=True — можно не указывать (поле необязательное).
        sa.Column('recorded_at', sa.DateTime(), nullable=True),

        # sa.PrimaryKeyConstraint('id') — объявляет колонку 'id' первичным ключом.
        # Первичный ключ: значения уникальны, PostgreSQL создаёт индекс автоматически.
        # autoincrement будет добавлен моделью SQLAlchemy (autoincrement=True).
        sa.PrimaryKeyConstraint('id'),
    )


def downgrade() -> None:
    """Отменяем миграцию: удаляем таблицу weather_records целиком."""

    # op.drop_table — удаляет таблицу из базы данных со всеми данными.
    # ВНИМАНИЕ: все записи в таблице будут УНИЧТОЖЕНЫ безвозвратно!
    # Вызывается командой: alembic downgrade -1 (когда текущая версия = 001_init)
    op.drop_table('weather_records')
