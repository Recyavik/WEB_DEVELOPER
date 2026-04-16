# Импортируем типы колонок из SQLAlchemy — они описывают, какие данные хранятся в каждом столбце таблицы
# Column — сам столбец, Integer — целое число, String — текст, Float — число с дробью, Text — длинный текст, DateTime — дата и время
from sqlalchemy import Column, Integer, String, Float, Text, DateTime

# DeclarativeBase — базовый класс для всех моделей (таблиц) в стиле SQLAlchemy 2.x
from sqlalchemy.orm import DeclarativeBase

# datetime — стандартный модуль Python для работы с датой и временем
from datetime import datetime


# Создаём базовый класс-регистратор: все модели, которые наследуются от него,
# будут автоматически зарегистрированы как таблицы в базе данных
class Base(DeclarativeBase):
    pass  # Тело пустое — Base нужен только как «родитель» для других классов


# Описываем модель (таблицу) промышленного робота
# Каждый атрибут класса Robot = один столбец таблицы robots в PostgreSQL
class Robot(Base):

    # Имя таблицы в базе данных — именно так она будет называться в PostgreSQL
    __tablename__ = "robots"

    # Первичный ключ — уникальный номер каждой записи (робота)
    # primary_key=True  → этот столбец — главный идентификатор строки
    # autoincrement=True → база сама присваивает следующий номер: 1, 2, 3...
    id = Column(Integer, primary_key=True, autoincrement=True)

    # Название модели робота, например: «KUKA KR 6 R900»
    # String(100) → текст длиной не более 100 символов
    # nullable=False → поле обязательно, пустым быть не может
    name = Column(String(100), nullable=False)

    # Производитель: KUKA, ABB, FANUC, Yaskawa и т.д.
    # String(100) → максимум 100 символов
    # nullable=False → нельзя оставить пустым
    manufacturer = Column(String(100), nullable=False)

    # Тип робота: welding (сварочный), assembly (сборочный), painting (покрасочный) и т.д.
    # String(50) → максимум 50 символов
    robot_type = Column(String(50), nullable=False)

    # Грузоподъёмность в килограммах (может быть дробным числом, например 6.5)
    # Float → число с плавающей точкой (дробное)
    payload_kg = Column(Float, nullable=False)

    # Радиус действия манипулятора в миллиметрах (целое число, например 900)
    # Integer → целое число без дроби
    reach_mm = Column(Integer, nullable=False)

    # Количество осей движения (обычно 4, 6 или 7)
    # Integer → целое число
    axes = Column(Integer, nullable=False)

    # Статус эксплуатации: active (активен), maintenance (на обслуживании), retired (списан)
    # default="active" → если не указать статус, автоматически ставится «active»
    status = Column(String(20), nullable=False, default="active")

    # Описание применения — необязательное поле (можно оставить пустым)
    # Text → длинный текст без ограничения длины
    # nullable=True → разрешено хранить NULL (пустое значение)
    description = Column(Text, nullable=True)

    # Дата и время добавления в каталог — заполняется автоматически при создании записи
    # DateTime → тип «дата + время»
    # default=datetime.utcnow → при создании строки записывается текущее время UTC
    # ВАЖНО: datetime.utcnow написано БЕЗ скобок () — мы передаём саму функцию,
    # а не её результат, чтобы время вычислялось в момент создания каждой записи
    added_at = Column(DateTime, default=datetime.utcnow)
