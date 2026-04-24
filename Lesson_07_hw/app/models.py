# ─── app/models.py ─────────────────────────────────────────────────────────
# Модели — это Python-классы, которые описывают таблицы базы данных.
# SQLAlchemy (ORM) превращает работу с БД в работу с обычными объектами Python.
# Вместо SQL: INSERT INTO weather_records ... мы пишем: db.add(WeatherRecord(...))

# datetime — стандартный модуль Python для работы с датой и временем.
# Используем datetime.utcnow() — текущее время в UTC (без часового пояса).
from datetime import datetime

# Column — описывает одну колонку таблицы.
# Integer, String, Float, DateTime, Text — типы данных для колонок.
from sqlalchemy import Column, Integer, String, Float, DateTime, Text

# declarative_base — функция, создающая базовый класс для всех моделей.
# Все наши модели будут наследоваться от Base.
# Base.metadata хранит описание всех таблиц — Alembic использует это для миграций.
from sqlalchemy.orm import declarative_base

# Создаём базовый класс. Все модели, унаследованные от Base,
# автоматически регистрируются в Base.metadata.
# Думайте об этом как об общем «реестре» всех таблиц проекта.
Base = declarative_base()


# WeatherRecord — модель одной записи наблюдения за погодой.
# Наследование от Base говорит SQLAlchemy: «это таблица в базе данных».
class WeatherRecord(Base):

    # __tablename__ — имя таблицы в PostgreSQL.
    # Должно совпадать с именем, которое мы создали в миграции 001_init.py.
    __tablename__ = 'weather_records'

    # id — первичный ключ: уникальный номер каждой записи.
    # Integer — целое число.
    # primary_key=True — это главное поле идентификации записи.
    # autoincrement=True — PostgreSQL сам назначает следующий номер (1, 2, 3...).
    #                      Нам не нужно указывать id при создании записи.
    id = Column(Integer, primary_key=True, autoincrement=True)

    # city — название города.
    # String(100) — строка максимум 100 символов (VARCHAR(100) в SQL).
    # nullable=False — значение ОБЯЗАТЕЛЬНО. Попытка сохранить запись
    #                  без города вызовет ошибку базы данных.
    city = Column(String(100), nullable=False)

    # temperature — температура воздуха в градусах Цельсия.
    # Float — число с плавающей точкой (может быть -5.3, 0.0, 23.7).
    # nullable=False — температура обязательна.
    temperature = Column(Float, nullable=False)

    # recorded_at — дата и время наблюдения.
    # DateTime — тип TIMESTAMP в PostgreSQL (дата + время).
    # default=datetime.utcnow — если не указать время при создании записи,
    #   SQLAlchemy автоматически подставит текущее время UTC.
    #   ВАЖНО: передаём datetime.utcnow без скобок — это ссылка на функцию,
    #   а не её результат. SQLAlchemy вызовет функцию в момент создания записи.
    recorded_at = Column(DateTime, default=datetime.utcnow)

    # description — краткое описание погоды (добавлено миграцией 002_add_description).
    # Text — строка без ограничения длины (TEXT в PostgreSQL).
    #        Используем Text вместо String, потому что описание может быть длинным.
    # nullable=True — поле необязательное. Запись может не иметь описания (NULL).
    description = Column(Text, nullable=True)
