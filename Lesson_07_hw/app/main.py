# ─── app/main.py ───────────────────────────────────────────────────────────
# Главный файл приложения. Здесь описаны все маршруты (routes) —
# то, что происходит, когда пользователь открывает ту или иную страницу.

# datetime — для получения текущего времени при сохранении записи.
from datetime import datetime

# FastAPI  — главный класс фреймворка, создаём объект приложения.
# Depends  — механизм внедрения зависимостей: FastAPI сам вызовет get_db()
#            и передаст результат в параметр функции.
# Form     — извлекает данные из HTML-формы (тег <form method="post">).
# Request  — объект HTTP-запроса (нужен шаблонизатору для построения URL).
from fastapi import FastAPI, Depends, Form, Request

# RedirectResponse — ответ-перенаправление: браузер получает команду
# перейти на другой URL. Используем после POST, чтобы при обновлении
# страницы форма не отправлялась повторно (паттерн POST→Redirect→GET).
from fastapi.responses import RedirectResponse

# Jinja2Templates — шаблонизатор: читает .html файлы, подставляет переменные,
# возвращает готовый HTML браузеру.
from fastapi.templating import Jinja2Templates

# Session — тип SQLAlchemy-сессии, используется только для аннотации типов.
from sqlalchemy.orm import Session

# get_db — генератор сессии из database.py.
from app.database import get_db

# WeatherRecord — класс модели из models.py (одна строка таблицы weather_records).
from app.models import WeatherRecord

# Создаём объект приложения FastAPI.
# title="Weather Diary" — название в автодокументации (/docs).
app = FastAPI(title="Weather Diary")

# Настраиваем шаблонизатор.
# directory="app/templates" — папка с HTML-файлами.
# Путь относительный: от /app (рабочей директории в Docker).
templates = Jinja2Templates(directory="app/templates")


# ─── Маршрут: / ────────────────────────────────────────────────────────────
# @app.get("/") — декоратор: «эта функция отвечает на GET-запрос к адресу /».
# GET — тип HTTP-запроса: браузер просто «получает» страницу (не отправляет данные).
@app.get("/")
def root():
    """Корневой адрес — перенаправляет на /weather."""

    # RedirectResponse — отвечает браузеру кодом 303 и заголовком Location: /weather.
    # Браузер автоматически переходит на /weather.
    # status_code=303 — «See Other»: стандартный код для редиректа после обработки формы.
    return RedirectResponse(url="/weather", status_code=303)


# ─── Маршрут: GET /weather ──────────────────────────────────────────────────
@app.get("/weather")
def weather_page(
    request: Request,               # Объект запроса — нужен шаблонизатору
    db: Session = Depends(get_db),  # FastAPI вызовет get_db() и передаст сессию сюда
):
    """Показывает страницу со списком всех наблюдений и формой добавления."""

    # db.query(WeatherRecord) — начинает запрос SELECT к таблице weather_records.
    # .order_by(WeatherRecord.recorded_at.desc()) — сортировка по дате:
    #   .desc() — по убыванию (самые новые записи сверху).
    # .all() — выполняет запрос и возвращает список объектов WeatherRecord.
    records = db.query(WeatherRecord).order_by(WeatherRecord.recorded_at.desc()).all()

    # templates.TemplateResponse — рендерит шаблон и возвращает HTML.
    # Аргументы (новый API Starlette):
    #   request         — объект запроса (обязателен для шаблонизатора)
    #   "weather.html"  — имя файла в папке app/templates/
    #   {"records": records} — словарь переменных, доступных в шаблоне.
    #     В шаблоне можно писать {{ records }}, {% for r in records %} и т.д.
    return templates.TemplateResponse(request, "weather.html", {"records": records})


# ─── Маршрут: POST /weather/add ────────────────────────────────────────────
# @app.post — обрабатывает POST-запрос (отправка данных из HTML-формы).
@app.post("/weather/add")
def add_weather(
    # Form(...) — читает поле из тела POST-запроса (данные формы).
    # ... (многоточие) — поле ОБЯЗАТЕЛЬНО. FastAPI вернёт 422 если не передано.
    city: str = Form(...),

    # float — тип данных: FastAPI автоматически преобразует строку «-5.3» в число -5.3.
    temperature: float = Form(...),

    # Form(None) — поле необязательное, по умолчанию None (пустое поле формы).
    description: str = Form(None),

    # Внедряем сессию БД через Depends (так же, как в weather_page).
    db: Session = Depends(get_db),
):
    """Принимает данные из формы и сохраняет новое наблюдение в БД."""

    # Создаём объект модели — одну строку таблицы (ещё не в БД).
    record = WeatherRecord(
        city=city,                # Название города из формы
        temperature=temperature,  # Температура из формы (уже float)

        # Обрабатываем description: если пользователь ничего не ввёл или
        # ввёл только пробелы — сохраняем NULL вместо пустой строки.
        # description.strip() — убирает пробелы по краям строки.
        # Условие: если description не None И не пустая строка → сохраняем как есть,
        #          иначе → None (NULL в БД).
        description=description if description and description.strip() else None,

        # datetime.utcnow() — текущее время в UTC (без часового пояса).
        # UTC — мировое стандартное время. Все серверы хранят время в UTC,
        # а отображают пользователю в его часовом поясе.
        recorded_at=datetime.utcnow(),
    )

    # db.add(record) — добавляет объект в сессию (в «очередь» для сохранения).
    # Данные ещё НЕ в базе данных — они ждут commit().
    db.add(record)

    # db.commit() — сохраняет все изменения в базе данных (отправляет INSERT).
    # После commit() данные реально появляются в PostgreSQL.
    db.commit()

    # После POST всегда делаем редирект (паттерн POST→Redirect→GET).
    # Это важно: если пользователь нажмёт F5 (обновить страницу),
    # браузер повторит GET /weather, а не POST /weather/add.
    # Без редиректа обновление страницы добавило бы запись ещё раз!
    return RedirectResponse(url="/weather", status_code=303)
