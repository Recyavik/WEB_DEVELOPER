"""
alembic/env.py — главный файл настройки Alembic.

Этот файл запускается при каждой команде alembic (upgrade, downgrade, revision).
Его задача — подключить наши модели SQLAlchemy и настроить соединение с БД.
"""

import os                                           # Для чтения переменных окружения
import sys                                          # Для управления путями Python
from logging.config import fileConfig               # Настройка логирования из alembic.ini

from sqlalchemy import engine_from_config           # Создаём движок БД из конфига
from sqlalchemy import pool                         # Пул соединений с БД

from alembic import context                         # Контекст выполнения Alembic

# ─── Добавляем путь к нашему приложению ──────────────────────────────────────
# Это нужно, чтобы Python мог найти наши модули (app/models.py и т.д.)
sys.path.insert(0, '/app')                          # Добавляем /app в начало sys.path

# ─── Импортируем модели ───────────────────────────────────────────────────────
# КРИТИЧЕСКИ ВАЖНО: без этого импорта autogenerate не увидит наши таблицы!
from app.models import Base                         # Base содержит метаданные всех таблиц

# ─── Настройка из alembic.ini ─────────────────────────────────────────────────
config = context.config                             # Читаем конфигурацию из alembic.ini

# Настраиваем логирование из секции [loggers] в alembic.ini
if config.config_file_name is not None:
    fileConfig(config.config_file_name)             # Применяем настройки логирования

# ─── Метаданные наших таблиц ──────────────────────────────────────────────────
# target_metadata — это то, с чем Alembic сравнивает текущую схему БД
# при команде --autogenerate
target_metadata = Base.metadata                     # Указываем метаданные наших моделей

# ─── Переопределяем URL из переменной окружения ───────────────────────────────
# DATABASE_URL задаётся в docker-compose.yml через environment
db_url = os.environ.get(
    'DATABASE_URL',                                 # Имя переменной окружения
    'postgresql://mks_user:mks_pass@db:5432/mks_db' # Значение по умолчанию
)
config.set_main_option('sqlalchemy.url', db_url)    # Устанавливаем URL в конфиг


def run_migrations_offline() -> None:
    """
    Запуск миграций в 'offline' режиме — без реального подключения к БД.
    Используется для генерации SQL-скриптов.
    """
    url = config.get_main_option("sqlalchemy.url")  # Берём URL из конфига
    context.configure(
        url=url,                                    # URL подключения
        target_metadata=target_metadata,            # Наши модели
        literal_binds=True,                         # Значения прямо в SQL
        dialect_opts={"paramstyle": "named"},       # Стиль параметров
    )

    with context.begin_transaction():               # Начинаем транзакцию
        context.run_migrations()                    # Запускаем миграции


def run_migrations_online() -> None:
    """
    Запуск миграций в 'online' режиме — с реальным подключением к БД.
    Это основной режим, используется при alembic upgrade/downgrade.
    """
    # Создаём движок БД из конфигурации alembic.ini
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),  # Секция [alembic] из ini
        prefix="sqlalchemy.",                       # Префикс для параметров SQLAlchemy
        poolclass=pool.NullPool,                    # NullPool — без кэша соединений (лучше для миграций)
    )

    # Открываем соединение с БД
    with connectable.connect() as connection:
        context.configure(
            connection=connection,                  # Передаём соединение
            target_metadata=target_metadata,        # Наши модели для autogenerate
        )

        with context.begin_transaction():           # Оборачиваем в транзакцию
            context.run_migrations()                # Выполняем миграции


# ─── Выбираем режим запуска ───────────────────────────────────────────────────
if context.is_offline_mode():
    run_migrations_offline()                        # Offline: только SQL-скрипты
else:
    run_migrations_online()                         # Online: реальное подключение к БД
