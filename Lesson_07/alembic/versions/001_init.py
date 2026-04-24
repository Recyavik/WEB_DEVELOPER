"""create log_entries table

Revision ID: 001_init
Revises: 
Create Date: 2025-01-01 00:00:00.000000

Первая миграция: создаём таблицу для записей бортового журнала МКС.
Эта миграция выполняется при первом запуске проекта.
"""

# Идентификаторы версии — используются Alembic для отслеживания состояния
revision = '001_init'       # Уникальный ID этой версии
down_revision = None        # None означает: это первая миграция в цепочке
branch_labels = None        # Ветки не используем
depends_on = None           # Зависимостей нет

from alembic import op      # op — объект для операций с базой данных
import sqlalchemy as sa     # sa — типы данных SQLAlchemy


def upgrade() -> None:
    """
    upgrade() вызывается при команде: alembic upgrade head
    Создаём таблицу log_entries — основную таблицу журнала МКС.
    """
    op.create_table(
        'log_entries',                          # Имя таблицы в PostgreSQL

        # Первичный ключ — уникальный номер каждой записи
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),

        # Имя космонавта-автора записи (не может быть пустым)
        sa.Column('author', sa.String(length=100), nullable=False),

        # Текст самой записи в журнале (длинный текст)
        sa.Column('content', sa.Text(), nullable=False),

        # Дата и время создания записи (заполняется автоматически)
        sa.Column('created_at', sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    """
    downgrade() вызывается при команде: alembic downgrade -1
    Откатываем изменения: удаляем таблицу log_entries.
    ВНИМАНИЕ: все данные в таблице будут уничтожены!
    """
    op.drop_table('log_entries')                # Удаляем таблицу полностью
