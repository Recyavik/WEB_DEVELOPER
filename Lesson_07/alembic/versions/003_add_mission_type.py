"""add mission_type to log_entries

Revision ID: 003_add_mission_type
Revises: 002_add_radiation
Create Date: 2025-01-03 00:00:00.000000

Третья миграция: добавляем тип миссии — категорию записи.
Это позволит фильтровать журнал по типам операций на МКС.

Типы миссий:
  - science   — научный эксперимент
  - repair    — техническое обслуживание
  - comms     — сеанс связи
  - medical   — медицинская процедура
  - other     — прочее
"""

# Идентификаторы версии
revision = '003_add_mission_type'    # ID этой миграции
down_revision = '002_add_radiation'  # Предыдущая версия
branch_labels = None
depends_on = None

from alembic import op               # Операции с БД
import sqlalchemy as sa              # Типы данных


def upgrade() -> None:
    """
    Добавляем поле mission_type — тип операции на МКС.
    server_default='other' означает: PostgreSQL сам поставит 'other'
    для всех старых строк — не нужен nullable=True!
    """
    op.add_column(
        'log_entries',
        sa.Column(
            'mission_type',                     # Название новой колонки
            sa.String(length=20),               # Короткая строка — тип миссии
            nullable=False,                     # NOT NULL — всегда должен быть тип
            server_default='other',             # PostgreSQL заполнит 'other' для старых строк
        )
    )


def downgrade() -> None:
    """
    Откатываем: удаляем колонку mission_type.
    Данные в author, content, radiation_level и created_at сохраняются.
    """
    op.drop_column('log_entries', 'mission_type')  # Убираем только эту колонку
