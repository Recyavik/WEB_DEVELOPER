import psycopg2          # библиотека для подключения к PostgreSQL
import os                 # чтобы читать переменные окружения

def get_connection():
    """
    Создаёт и возвращает соединение с базой данных.
    Аналогия: как открыть дверь в архив — получаем доступ к хранилищу.
    """
    conn = psycopg2.connect(
        host=os.environ.get("DB_HOST", "localhost"),      # адрес сервера БД
        dbname=os.environ.get("DB_NAME", "bmi_db"),       # имя базы данных
        user=os.environ.get("DB_USER", "student"),         # пользователь
        password=os.environ.get("DB_PASSWORD", "secret123"),  # пароль
        port=int(os.environ.get("DB_PORT", 5432))          # порт PostgreSQL
    )
    return conn  # возвращаем соединение — наш «пропуск в архив»


def init_db():
    """
    Создаёт таблицу измерений, если она ещё не существует.
    Аналогия: как поставить пустую картотеку перед первым рабочим днём.
    """
    conn = get_connection()   # открываем соединение
    try:
        cur = conn.cursor()   # создаём курсор — «ручку» для записи в БД
        cur.execute("""
            CREATE TABLE IF NOT EXISTS measurements (
                id          SERIAL PRIMARY KEY,         -- автоматический номер записи
                weight      NUMERIC(5,2) NOT NULL,      -- вес в кг (например 72.50)
                height      NUMERIC(5,2) NOT NULL,      -- рост в см (например 175.00)
                bmi         NUMERIC(5,2) NOT NULL,      -- значение ИМТ
                category    VARCHAR(30)  NOT NULL,      -- категория (Норма, Избыток и т.д.)
                measured_at TIMESTAMPTZ DEFAULT NOW()   -- дата и время измерения
            )
        """)
        conn.commit()         # сохраняем изменения — «подписываем документ»
    finally:
        conn.close()          # всегда закрываем соединение, даже если была ошибка


def calculate_bmi(weight: float, height_cm: float) -> tuple[float, str]:
    """
    Вычисляет ИМТ и определяет категорию.
    Формула: ИМТ = вес (кг) / рост² (м)
    Возвращает кортеж (значение_имт, категория).
    """
    height_m = height_cm / 100          # переводим сантиметры в метры
    bmi = weight / (height_m ** 2)      # вычисляем ИМТ по формуле ВОЗ
    bmi = round(bmi, 2)                 # округляем до 2 знаков после запятой

    # Определяем категорию по шкале ВОЗ
    if bmi < 18.5:
        category = "Дефицит массы"      # ниже нормы
    elif bmi < 25.0:
        category = "Норма"              # идеальный диапазон
    elif bmi < 30.0:
        category = "Избыточный вес"     # выше нормы
    else:
        category = "Ожирение"           # значительно выше нормы

    return bmi, category                # возвращаем оба значения сразу


def insert_measurement(weight: float, height: float, bmi: float, category: str):
    """
    Сохраняет одно измерение в базу данных (INSERT).
    Аналогия: как положить карточку пациента в картотеку.
    """
    conn = get_connection()   # открываем соединение с БД
    try:
        cur = conn.cursor()   # создаём курсор для выполнения запросов

        # НИКОГДА не подставляйте данные напрямую в строку SQL!
        # Используем %s — плейсхолдеры, psycopg2 экранирует сам (защита от SQL-инъекций)
        cur.execute(
            """
            INSERT INTO measurements (weight, height, bmi, category)
            VALUES (%s, %s, %s, %s)
            """,
            (weight, height, bmi, category)   # данные передаём отдельным кортежем
        )
        conn.commit()          # фиксируем транзакцию — без этого данные не сохранятся!
    finally:
        conn.close()           # закрываем соединение в любом случае


def get_history(limit: int = 10) -> list[tuple]:
    """
    Получает последние измерения из базы данных (SELECT).
    Аналогия: как попросить архивариуса показать последние N карточек.
    Возвращает список строк таблицы.
    """
    conn = get_connection()    # открываем соединение
    try:
        cur = conn.cursor()    # создаём курсор

        cur.execute(
            """
            SELECT weight, height, bmi, category, measured_at
            FROM measurements
            ORDER BY measured_at DESC   -- сначала самые свежие
            LIMIT %s                    -- не больше N записей
            """,
            (limit,)           # передаём limit как кортеж (важна запятая!)
        )

        rows = cur.fetchall()  # получаем все строки результата в виде списка кортежей
        return rows            # возвращаем данные
    finally:
        conn.close()           # закрываем соединение
