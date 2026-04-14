from fastapi import FastAPI, Request, Form  # основные компоненты FastAPI
from fastapi.templating import Jinja2Templates  # для HTML-шаблонов
from fastapi.staticfiles import StaticFiles    # для CSS/картинок
from fastapi.responses import RedirectResponse # для редиректа после формы
from contextlib import asynccontextmanager     # для кода при старте приложения

from app.database import (       # импортируем все наши функции работы с БД
    init_db,                     # создать таблицу при старте
    calculate_bmi,               # вычислить ИМТ
    insert_measurement,          # сохранить измерение в БД
    get_history,                 # получить историю из БД
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Код в этом блоке выполняется ОДИН РАЗ при запуске сервера.
    Аналогия: утренний ритуал повара — подготовка кухни до открытия ресторана.
    """
    init_db()   # создаём таблицу, если её ещё нет
    yield       # здесь приложение работает (принимает запросы)
    # после yield — код при остановке (здесь он нам не нужен)


# Создаём приложение FastAPI и передаём функцию жизненного цикла
app = FastAPI(title="Калькулятор ИМТ", lifespan=lifespan)

# Подключаем статические файлы (CSS, картинки) из папки static
app.mount("/static", StaticFiles(directory="app/static"), name="static")

# Указываем папку с HTML-шаблонами
templates = Jinja2Templates(directory="app/templates")


@app.get("/")
async def index(request: Request):
    """
    Главная страница — показывает форму для ввода данных.
    Аналогия: витрина магазина с пустой корзиной — просто смотрим.
    """
    return templates.TemplateResponse(
        request,           # передаём объект запроса (нужен для шаблона)
        "index.html",      # имя файла шаблона
        {"title": "Калькулятор ИМТ"}  # данные для шаблона
    )


@app.post("/calculate")
async def calculate(
    request: Request,
    weight: float = Form(...),   # вес из HTML-формы, обязательное поле
    height: float = Form(...),   # рост из HTML-формы, обязательное поле
):
    """
    Принимает данные формы, вычисляет ИМТ, сохраняет в БД.
    Аналогия: кассир принимает заказ, считает сумму и записывает в журнал.
    """
    # Вычисляем ИМТ и категорию
    bmi, category = calculate_bmi(weight, height)

    # Сохраняем результат в базу данных
    insert_measurement(weight, height, bmi, category)

    # Показываем страницу с результатом
    return templates.TemplateResponse(
        request,
        "result.html",
        {
            "title": "Результат",
            "weight": weight,      # вес для отображения
            "height": height,      # рост для отображения
            "bmi": bmi,            # вычисленный ИМТ
            "category": category,  # категория ВОЗ
        }
    )


@app.get("/history")
async def history(request: Request):
    """
    Страница истории — показывает все сохранённые измерения из БД.
    Аналогия: страница журнала с записями всех пациентов.
    """
    rows = get_history(limit=20)   # получаем последние 20 записей из БД

    # Преобразуем кортежи в список словарей — удобнее для шаблона
    measurements = []
    for row in rows:
        measurements.append({
            "weight": row[0],       # вес
            "height": row[1],       # рост
            "bmi": row[2],          # ИМТ
            "category": row[3],     # категория
            "measured_at": row[4],  # дата и время
        })

    return templates.TemplateResponse(
        request,
        "history.html",
        {
            "title": "История измерений",
            "measurements": measurements,  # передаём список в шаблон
            "count": len(measurements),    # общее количество записей
        }
    )
