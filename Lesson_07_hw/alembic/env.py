# ─── alembic/env.py ────────────────────────────────────────────────────────
# Этот файл — «мозг» Alembic. Он запускается при каждой команде
# (upgrade, downgrade, current и т.д.) и настраивает подключение к БД.

import os           # Модуль для работы с переменными окружения и файловой системой
import sys          # Модуль для управления путями поиска Python-модулей
from logging.config import fileConfig  # Функция для настройки логирования из файла

from sqlalchemy import engine_from_config, pool  # Инструменты SQLAlchemy для подключения к БД
from alembic import context  # Контекст Alembic — содержит текущую конфигурацию и состояние

# sys.path — список папок, в которых Python ищет модули при import.
# Мы вставляем '/app' на первое место (индекс 0), чтобы Python нашёл
# наш пакет «app» (папку app/ с __init__.py).
# Без этого строка «from app.models import Base» ниже вызвала бы ModuleNotFoundError.
sys.path.insert(0, '/app')

# Импортируем Base — базовый класс всех наших моделей.
# Base.metadata содержит описание всех таблиц, которые мы объявили в models.py.
# Alembic использует эти метаданные для команды autogenerate (автоматическое создание миграций).
# noqa: E402 — подсказка линтеру: игнорировать предупреждение о позднем импорте
#              (импорт стоит не в начале файла — это намеренно, после sys.path.insert).
from app.models import Base  # noqa: E402

# context.config — объект с настройками из alembic.ini.
# Через него можно читать и изменять параметры конфигурации.
config = context.config

# Настраиваем логирование из файла alembic.ini (секции [loggers], [handlers], [formatters]).
# Проверяем, что имя файла конфигурации известно (не None), прежде чем читать его.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# target_metadata — метаданные всех таблиц нашего проекта.
# Alembic сравнивает эти метаданные с реальной схемой БД,
# чтобы генерировать миграции командой «alembic revision --autogenerate».
target_metadata = Base.metadata

# Читаем DATABASE_URL из переменной окружения.
# В Docker она задаётся в docker-compose.yml (environment: DATABASE_URL: ...).
# os.environ.get('DATABASE_URL') — возвращает значение переменной или None, если не задана.
database_url = os.environ.get('DATABASE_URL')

# Если переменная окружения задана — переопределяем URL из alembic.ini.
# Это позволяет использовать разные БД для разработки и продакшена,
# не меняя файл конфигурации.
if database_url:
    # set_main_option — устанавливает параметр в секции [alembic] alembic.ini.
    # 'sqlalchemy.url' — именно тот параметр, который SQLAlchemy использует для подключения.
    config.set_main_option('sqlalchemy.url', database_url)


def run_migrations_offline() -> None:
    """
    Режим «offline» — генерирует SQL-скрипт в файл, не подключаясь к БД.
    Используется командой: alembic upgrade head --sql > migration.sql
    Полезно, когда нет прямого доступа к базе данных.
    """
    # Получаем URL базы данных из конфигурации.
    url = config.get_main_option('sqlalchemy.url')

    # Настраиваем контекст Alembic для работы без реального соединения.
    context.configure(
        url=url,                        # Строка подключения (для генерации правильного SQL-диалекта)
        target_metadata=target_metadata,  # Метаданные таблиц (для autogenerate)
        literal_binds=True,             # Подставлять значения прямо в SQL, не через параметры
        dialect_opts={'paramstyle': 'named'},  # Стиль параметров SQL: :name вместо ?
    )

    # Открываем транзакцию и выполняем миграции (генерируем SQL).
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """
    Режим «online» — подключается к реальной БД и применяет миграции.
    Это обычный режим работы: alembic upgrade head.
    """
    # engine_from_config — создаёт SQLAlchemy engine из настроек alembic.ini.
    # config.get_section(config.config_ini_section, {}) — читает секцию [alembic].
    # prefix='sqlalchemy.' — берёт только параметры, начинающиеся с «sqlalchemy.»
    #                        (например, sqlalchemy.url).
    # poolclass=pool.NullPool — не хранить пул соединений.
    #   Пул соединений — это кеш открытых соединений к БД для повторного использования.
    #   NullPool отключает кеш: каждый раз создаётся новое соединение.
    #   Для миграций это правильно: нам нужно одно соединение, не постоянный пул.
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix='sqlalchemy.',
        poolclass=pool.NullPool,
    )

    # Открываем реальное соединение с БД через контекстный менеджер (with).
    # Это гарантирует, что соединение будет закрыто даже при ошибке.
    with connectable.connect() as connection:
        # Настраиваем контекст Alembic для работы с реальным соединением.
        context.configure(
            connection=connection,          # Реальное соединение с БД
            target_metadata=target_metadata,  # Метаданные таблиц
        )

        # Открываем транзакцию и применяем миграции к БД.
        # Транзакция: если что-то пошло не так — все изменения откатываются.
        with context.begin_transaction():
            context.run_migrations()


# context.is_offline_mode() — возвращает True, если передан флаг --sql.
# В зависимости от режима вызываем нужную функцию.
if context.is_offline_mode():
    run_migrations_offline()  # Генерировать SQL в файл
else:
    run_migrations_online()   # Применить миграции к реальной БД
