"""add radiation_level to log_entries

Revision ID: 002_add_radiation
Revises: 001_init
Create Date: 2025-01-02 00:00:00.000000

Вторая миграция: добавляем поле radiation_level к существующей таблице.
ВАЖНО: используем nullable=True, чтобы старые записи не сломались —
они получат значение NULL в новом поле. Данные полностью сохраняются!
"""

# Идентификаторы версии
revision = '002_add_radiation'   # ID этой миграции
down_revision = '001_init'       # Ссылка на предыдущую версию (цепочка!)
branch_labels = None
depends_on = None

from alembic import op           # Операции с БД
import sqlalchemy as sa          # Типы данных


def upgrade() -> None:
    """
    upgrade() — добавляем новую колонку к существующей таблице.
    Используем op.add_column вместо пересоздания таблицы.
    Существующие строки получат radiation_level = NULL (безопасно!).
    """
    op.add_column(
        'log_entries',                          # Имя таблицы, к которой добавляем
        sa.Column(
            'radiation_level',                  # Имя новой колонки
            sa.Float(),                         # Тип: дробное число (мкЗв/ч)
            nullable=True,                      # ВАЖНО: разрешаем NULL для старых строк
        )
    )


def downgrade() -> None:
    """
    downgrade() — откатываем добавление колонки.
    При откате колонка radiation_level удаляется, но остальные данные сохраняются!
    """
    op.drop_column('log_entries', 'radiation_level')  # Удаляем только эту колонку
