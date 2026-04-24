"""add description to weather_records

Revision ID: 002_add_description
Revises: 001_init
Create Date: 2026-04-24
"""
# ─── Миграция 002: добавление колонки description ──────────────────────────
# Это ВТОРАЯ миграция — она изменяет уже существующую таблицу.
# Таблица weather_records уже создана миграцией 001_init.
# Мы просто добавляем новую колонку, не трогая существующие данные.

from typing import Sequence, Union  # Типы для аннотаций (см. комментарии в 001_init.py)
from alembic import op              # Объект операций Alembic (create_table, add_column и т.д.)
import sqlalchemy as sa             # SQLAlchemy для описания типов колонок

# ─── Идентификаторы миграции ────────────────────────────────────────────────

# revision — уникальный ID этой миграции.
# Alembic запишет '002_add_description' в таблицу alembic_version после применения.
revision: str = '002_add_description'

# down_revision — ID предыдущей миграции.
# '001_init' означает: эта миграция применяется ПОСЛЕ 001_init.
# Это и есть «цепочка»: Alembic знает порядок применения миграций.
# Если запустить «alembic upgrade head» на пустой БД:
#   1) сначала применится 001_init (down_revision=None)
#   2) затем 002_add_description (down_revision='001_init')
down_revision: Union[str, None] = '001_init'

# branch_labels и depends_on — не используем (None), см. 001_init.py
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Применяем миграцию: добавляем колонку description в таблицу."""

    # op.add_column — добавляет новую колонку в существующую таблицу.
    # Первый аргумент — имя таблицы (она уже существует после 001_init).
    op.add_column(
        'weather_records',

        # sa.Column — описание новой колонки.
        # 'description' — имя колонки в БД.
        # sa.Text() — тип TEXT в PostgreSQL: строка без ограничения длины.
        #             Отличие от String(N): нет максимальной длины.
        #             Подходит для произвольных описаний («Пасмурно, туман»).
        # nullable=True — колонка НЕОБЯЗАТЕЛЬНАЯ: старые строки получат NULL,
        #                 а новые записи могут не содержать описание.
        #                 ВАЖНО: нельзя добавить NOT NULL колонку в таблицу
        #                 с данными без server_default — PostgreSQL запретит это,
        #                 потому что не знает, что поставить в существующие строки.
        sa.Column('description', sa.Text(), nullable=True),
    )


def downgrade() -> None:
    """Отменяем миграцию: удаляем колонку description из таблицы."""

    # op.drop_column — удаляет одну колонку из таблицы.
    # Первый аргумент  — имя таблицы.
    # Второй аргумент  — имя колонки, которую удалить.
    #
    # Ключевое свойство downgrade: удаляется ТОЛЬКО колонка description.
    # Все остальные данные (id, city, temperature, recorded_at) СОХРАНЯЮТСЯ.
    # Это и проверяет задание: «alembic downgrade -1 удаляет только колонку,
    # остальные данные сохраняются».
    op.drop_column('weather_records', 'description')
