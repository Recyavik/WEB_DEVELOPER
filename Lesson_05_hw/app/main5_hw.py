from fastapi import FastAPI, Request, Form
# FastAPI — главный класс нашего веб-приложения (как движок автомобиля)
# Request — объект HTTP-запроса от браузера (содержит URL, заголовки, тело)
# Form  — говорит FastAPI, что данные придут из HTML-формы (не из URL)

from fastapi.templating import Jinja2Templates
# Jinja2Templates — движок для HTML-шаблонов
# Позволяет писать {{ переменная }} прямо в HTML
# (Jinja2 — популярный шаблонизатор, используется в Flask/Django)

from fastapi.staticfiles import StaticFiles
# StaticFiles — служит статические файлы: CSS, картинки, JS
# Без этого браузер не сможет загрузить наш style.css

from fastapi.responses import RedirectResponse
# RedirectResponse — ответ-перенаправление (браузер переходит на другую страницу)
# Например, после отправки формы → перенаправить на страницу результата

from contextlib import asynccontextmanager
# asynccontextmanager — декоратор для создания «менеджера контекста»
# Позволяет выполнить код при старте И при остановке приложения

from app.database import (    # импортируем функции из нашего файла database.py
    init_db,                  # создать таблицу при старте (если нет)
    determine_status,         # определить статус по температуре
    insert_measurement,       # сохранить измерение в БД
    get_history,              # получить историю замеров из БД
)


@asynccontextmanager              # этот декоратор превращает функцию в «хук жизненного цикла»
async def lifespan(app: FastAPI): # app: FastAPI — само наше приложение (передаётся автоматически)
    """
    Функция жизненного цикла приложения.
    Код ДО yield  — выполняется ОДИН РАЗ при запуске сервера.
    Код ПОСЛЕ yield — выполняется ОДИН РАЗ при остановке сервера.
    Аналогия: открытие и закрытие магазина — утром готовим, вечером убираем.
    """
    init_db()   # создаём таблицу в БД при старте (если ещё не существует)
    yield       # ← здесь приложение «живёт» и обрабатывает запросы
                # (yield = пауза; всё что ниже — выполнится при остановке)
    # место для кода при остановке (например: закрыть пул соединений)
    # в нашем проекте это не нужно, поэтому блок пустой


# Создаём экземпляр приложения FastAPI
app = FastAPI(
    title="Мониторинг температуры серверов",
    # title — название приложения (отображается в автодокументации /docs)

    lifespan=lifespan
    # lifespan — передаём функцию жизненного цикла
    # FastAPI вызовет её при старте и остановке сервера
)

app.mount(
    "/static",                         # URL-префикс: браузер будет запрашивать /static/css/style.css
    StaticFiles(directory="app/static"),# directory — папка на диске с нашими файлами
    name="static"                       # name — внутреннее имя маршрута (используется в шаблонах)
)
# mount() — «подключаем» папку с файлами к URL-адресу
# Теперь запрос GET /static/css/style.css → файл app/static/css/style.css

templates = Jinja2Templates(directory="app/templates")
# Создаём объект шаблонов и указываем папку с HTML-файлами
# При вызове templates.TemplateResponse("index.html", ...) он найдёт app/templates/index.html


@app.get("/")             # декоратор: обрабатывает GET-запросы на адрес "/"
async def index(          # async — асинхронная функция (не блокирует сервер в ожидании)
    request: Request      # request — объект запроса от браузера (обязателен для шаблонов)
):
    """
    Маршрут главной страницы — показывает форму для ввода данных.
    GET / → возвращает HTML-страницу с формой.
    Аналогия: витрина магазина — клиент просто смотрит, ничего не меняет.
    """
    return templates.TemplateResponse(
        request,                                   # передаём объект запроса (Jinja2 требует его)
        "index.html",                              # имя файла шаблона в папке templates/
        {"title": "Мониторинг температуры"}        # словарь с переменными для шаблона
        # в шаблоне можно написать {{ title }} — получится "Мониторинг температуры"
    )


@app.post("/log")           # декоратор: обрабатывает POST-запросы на адрес "/log"
                             # POST — метод отправки данных формы (в отличие от GET, данные скрыты)
async def log_temperature(
    request: Request,                          # объект запроса (нужен для шаблона)
    server_name: str = Form(...),              # имя сервера из HTML-формы
    # str — тип данных: строка
    # Form(...) — читаем значение из тела POST-запроса (из формы)
    # ... — означает "обязательный параметр" (без значения по умолчанию)

    temperature: float = Form(...),            # температура из HTML-формы
    # float — дробное число (например, 65.5)
    # Form(...) — обязательный параметр из формы
):
    """
    Принимает данные из HTML-формы, определяет статус, сохраняет в БД.
    POST /log → обрабатывает, сохраняет, показывает страницу результата.
    Аналогия: кассир принимает заказ, записывает в журнал, выдаёт чек.
    """
    status = determine_status(temperature)
    # вызываем нашу функцию из database.py
    # она вернёт 'Норма', 'Предупреждение' или 'Критично'

    insert_measurement(server_name, temperature, status)
    # сохраняем все три значения в таблицу temperature_logs в PostgreSQL

    return templates.TemplateResponse(
        request,          # объект запроса
        "result.html",    # HTML-шаблон страницы результата
        {
            "title": "Результат замера",   # заголовок вкладки браузера
            "server_name": server_name,    # имя сервера → в шаблоне: {{ server_name }}
            "temperature": temperature,    # температура → в шаблоне: {{ temperature }}
            "status": status,              # статус → в шаблоне: {{ status }}
        }
    )


@app.get("/logs")                      # обрабатывает GET-запросы на адрес "/logs"
async def logs(
    request: Request,                  # объект запроса
    server_filter: str = ""            # необязательный параметр из URL-строки
    # Например: /logs?server_filter=srv-01
    # str — тип: строка
    # = "" — значение по умолчанию (пустая строка = нет фильтра)
):
    """
    Страница истории — показывает последние 20 замеров из БД.
    GET /logs → запрашивает данные из PostgreSQL, показывает таблицу.
    Аналогия: страница журнала наблюдений со всеми прошлыми записями.
    """
    rows = get_history(limit=20)
    # вызываем функцию из database.py с явным параметром limit=20
    # получаем список кортежей: [('srv-01', 65.5, 'Предупреждение', datetime(...)), ...]

    measurements = []  # создаём пустой список для словарей
    for row in rows:   # проходим по каждой строке из базы данных
        measurements.append({
            # append() — добавляем словарь в конец списка
            # словарь удобнее кортежа: row["server_name"] понятнее, чем row[0]
            "server_name": row[0],   # row[0] — первое поле: имя сервера
            "temperature": row[1],   # row[1] — второе поле: температура
            "status":      row[2],   # row[2] — третье поле: статус
            "recorded_at": row[3],   # row[3] — четвёртое поле: дата и время
        })

    if server_filter:  # если фильтр не пустой (пользователь что-то ввёл)
        measurements = [
            m                                               # оставляем запись m
            for m in measurements                          # перебираем все записи
            if server_filter.lower()                       # если фильтр (в нижнем регистре)
               in m["server_name"].lower()                 # содержится в имени сервера
        ]
        # Это «list comprehension» — компактный способ написать цикл с условием
        # .lower() — переводим в нижний регистр, чтобы поиск был без учёта регистра
        # SRV-01 и srv-01 найдут одинаково

    return templates.TemplateResponse(
        request,          # объект запроса
        "history.html",   # шаблон страницы истории
        {
            "title": "История замеров температуры",  # заголовок страницы
            "measurements": measurements,             # список словарей → {{ measurements }} в шаблоне
            "count": len(measurements),               # количество записей (len = длина списка)
            "server_filter": server_filter,           # текущий фильтр → показываем в форме
        }
    )
