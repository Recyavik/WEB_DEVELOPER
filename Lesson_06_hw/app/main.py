# FastAPI — главный класс для создания веб-приложения
# Request — объект HTTP-запроса (содержит URL, заголовки и т.д.)
# Depends — механизм внедрения зависимостей (например, передача сессии базы данных)
# Form — считывает данные из HTML-формы (POST-запрос)
# HTTPException — позволяет вернуть ошибку с кодом HTTP (например, 404 — не найдено)
# Query — считывает параметры из строки URL (например, ?type=welding)
from fastapi import FastAPI, Request, Depends, Form, HTTPException, Query

# Jinja2Templates — движок шаблонов: подставляет данные из Python в HTML-файлы
from fastapi.templating import Jinja2Templates

# StaticFiles — раздаёт статические файлы (CSS, картинки, JS)
from fastapi.staticfiles import StaticFiles

# RedirectResponse — перенаправляет браузер на другой адрес (например, после сохранения формы)
from fastapi.responses import RedirectResponse

# Session — тип сессии SQLAlchemy для работы с базой данных
from sqlalchemy.orm import Session

# func — набор SQL-функций (например, func.count(), func.avg(), func.max())
from sqlalchemy import func

# Импортируем движок базы данных и генератор сессий из нашего модуля database.py
from app.database import engine, get_db

# Импортируем базовый класс (нужен для создания таблиц) и модель Robot
from app.models import Base, Robot

# Создаём экземпляр FastAPI-приложения
# title — название приложения, отображается в автодокументации (/docs)
app = FastAPI(title="Каталог промышленных роботов")

# Подключаем папку со статическими файлами (CSS, изображения)
# "/static" — URL-путь, по которому браузер будет запрашивать файлы
# directory="app/static" — физическая папка на диске
# name="static" — внутреннее имя для ссылок внутри шаблонов
app.mount("/static", StaticFiles(directory="app/static"), name="static")

# Указываем FastAPI, где искать HTML-шаблоны
# directory="app/templates" — папка с .html файлами
templates = Jinja2Templates(directory="app/templates")

# Список допустимых типов роботов — используется для фильтрации и выпадающего списка в форме
ROBOT_TYPES = ["welding", "assembly", "painting", "logistics", "inspection"]

# Список допустимых статусов эксплуатации
STATUSES = ["active", "maintenance", "retired"]

# Словарь для перевода английских кодов типов в русские названия
# Ключ — код (как хранится в базе), значение — читаемое название для отображения
TYPE_LABELS = {
    "welding":    "Сварочный",
    "assembly":   "Сборочный",
    "painting":   "Покрасочный",
    "logistics":  "Логистический",
    "inspection": "Инспекционный",
}

# Словарь для перевода кодов статусов в русские названия
STATUS_LABELS = {
    "active":      "Активен",
    "maintenance": "Обслуживание",
    "retired":     "Списан",
}


# Декоратор @app.on_event("startup") — регистрирует функцию, которая выполнится
# один раз при старте сервера (до обработки первого запроса)
@app.on_event("startup")
def startup():
    # Создаём все таблицы в базе данных на основе моделей (Robot и другие)
    # bind=engine — использует наш движок для подключения к PostgreSQL
    # Если таблицы уже существуют — пропускает создание (не удаляет данные!)
    Base.metadata.create_all(bind=engine)


# ─── ГЛАВНАЯ СТРАНИЦА — список роботов + фильтр по типу ──────────────────────

# @app.get("/") — регистрирует эту функцию как обработчик GET-запроса на адрес "/"
# GET — вид запроса, когда браузер просто открывает страницу
@app.get("/")
def index(
    request: Request,                               # объект запроса — нужен для рендеринга шаблона
    robot_type: str = Query(default="", alias="type"),  # читаем параметр ?type= из URL; default="" — если не передан
    db: Session = Depends(get_db)                   # FastAPI автоматически создаёт сессию БД и передаёт сюда
):
    # Формируем базовый запрос к таблице robots, сортируем по id (по порядку добавления)
    query = db.query(Robot).order_by(Robot.id)

    # Если параметр ?type= передан и является допустимым значением — добавляем фильтрацию
    if robot_type and robot_type in ROBOT_TYPES:
        # filter() добавляет условие WHERE в SQL-запрос
        # Robot.robot_type == robot_type → WHERE robot_type = 'welding' (например)
        query = query.filter(Robot.robot_type == robot_type)

    # Выполняем запрос и получаем список всех подходящих роботов
    robots = query.all()

    # Считаем количество роботов в каждом статусе для мини-статистики внизу страницы
    # group_by(Robot.status) → GROUP BY status в SQL
    # func.count(Robot.id) → COUNT(id) — считаем количество строк в каждой группе
    stats_status = db.query(Robot.status, func.count(Robot.id)).group_by(Robot.status).all()

    # Рендерим HTML-шаблон "index.html" и передаём в него переменные
    return templates.TemplateResponse(
        request,        # объект запроса (обязателен для Jinja2 в FastAPI)
        "index.html",   # имя файла шаблона в папке app/templates
        {
            "robots":        robots,        # список объектов Robot для отображения в таблице
            "robot_types":   ROBOT_TYPES,   # список типов для кнопок-фильтров
            "type_labels":   TYPE_LABELS,   # словарь перевода типов на русский
            "status_labels": STATUS_LABELS, # словарь перевода статусов на русский
            "current_type":  robot_type,    # выбранный тип фильтра (чтобы подсветить активную кнопку)
            "stats_status":  stats_status,  # статистика по статусам для нижней панели
        }
    )


# ─── СТРАНИЦА СТАТИСТИКИ ──────────────────────────────────────────────────────

# @app.get("/stats") — обрабатывает GET-запрос на адрес "/stats"
@app.get("/stats")
def stats(request: Request, db: Session = Depends(get_db)):
    # Считаем количество роботов каждого типа
    # Результат: список пар (тип, количество), например: [("welding", 3), ("assembly", 2)]
    stats_type = db.query(Robot.robot_type, func.count(Robot.id)).group_by(Robot.robot_type).all()

    # Считаем количество роботов в каждом статусе
    stats_status = db.query(Robot.status, func.count(Robot.id)).group_by(Robot.status).all()

    # Считаем количество роботов у каждого производителя
    # order_by(func.count(Robot.id).desc()) → сортируем по убыванию (самый популярный производитель первый)
    stats_mfr = db.query(Robot.manufacturer, func.count(Robot.id)).group_by(Robot.manufacturer).order_by(func.count(Robot.id).desc()).all()

    # func.avg() → AVG() в SQL — среднее значение грузоподъёмности
    # scalar() → возвращает одно число (не список), например: 14.7
    avg_payload = db.query(func.avg(Robot.payload_kg)).scalar()

    # func.max() → MAX() в SQL — максимальный радиус действия среди всех роботов
    max_reach = db.query(func.max(Robot.reach_mm)).scalar()

    # func.count() → COUNT() в SQL — общее количество роботов
    total = db.query(func.count(Robot.id)).scalar()

    # Рендерим страницу статистики, передавая все вычисленные данные в шаблон
    return templates.TemplateResponse(
        request,
        "stats.html",
        {
            "stats_type":   stats_type,    # статистика по типам
            "stats_status": stats_status,  # статистика по статусам
            "stats_mfr":    stats_mfr,     # статистика по производителям
            "type_labels":  TYPE_LABELS,   # словарь перевода типов на русский
            "status_labels":STATUS_LABELS, # словарь перевода статусов на русский
            # round(avg_payload, 1) → округляем до 1 знака после запятой
            # if avg_payload else 0 → если нет роботов (avg вернул None), показываем 0
            "avg_payload":  round(avg_payload, 1) if avg_payload else 0,
            # max_reach or 0 → если нет роботов (max вернул None), показываем 0
            "max_reach":    max_reach or 0,
            # total or 0 → если таблица пуста (count вернул None), показываем 0
            "total":        total or 0,
        }
    )


# ─── ФОРМА СОЗДАНИЯ ───────────────────────────────────────────────────────────

# GET /create → открывает пустую HTML-форму для добавления нового робота
@app.get("/create")
def create_form(request: Request):
    # Рендерим страницу с формой, передаём списки типов и статусов для выпадающих меню
    return templates.TemplateResponse(
        request,
        "create.html",
        {
            "robot_types":   ROBOT_TYPES,   # список типов для <select> в форме
            "type_labels":   TYPE_LABELS,   # русские названия типов
            "statuses":      STATUSES,      # список статусов для <select> в форме
            "status_labels": STATUS_LABELS, # русские названия статусов
        }
    )


# POST /create → принимает данные из формы и сохраняет нового робота в базу
@app.post("/create")
def create_robot(
    request: Request,
    # Form(...) — обязательное поле формы; ... означает «без значения по умолчанию»
    name: str = Form(...),             # название модели робота
    manufacturer: str = Form(...),     # производитель
    robot_type: str = Form(...),       # тип робота (welding, assembly и т.д.)
    payload_kg: float = Form(...),     # грузоподъёмность в кг (дробное число)
    reach_mm: int = Form(...),         # радиус действия в мм (целое число)
    axes: int = Form(...),             # количество осей (целое число)
    status: str = Form(...),           # статус эксплуатации
    description: str = Form(""),       # описание — необязательное, по умолчанию пустая строка
    db: Session = Depends(get_db)      # сессия базы данных (FastAPI создаёт автоматически)
):
    # Создаём Python-объект Robot с данными из формы
    # Пока это только объект в памяти — в базу данных он ещё не сохранён
    robot = Robot(
        name=name,                # название
        manufacturer=manufacturer,# производитель
        robot_type=robot_type,    # тип
        payload_kg=payload_kg,    # грузоподъёмность
        reach_mm=reach_mm,        # радиус
        axes=axes,                # оси
        status=status,            # статус
        # Если описание не пустое — сохраняем его, иначе сохраняем None (NULL в базе)
        description=description if description else None,
    )
    db.add(robot)      # Сообщаем SQLAlchemy: «запомни этот объект, его нужно сохранить»
    db.commit()        # Выполняем INSERT INTO robots (...) — фактически записываем в PostgreSQL
    db.refresh(robot)  # Обновляем объект из базы, чтобы получить присвоенный id

    # Перенаправляем браузер на страницу детальной карточки нового робота
    # status_code=303 (See Other) — стандартный код после POST-запроса, говорит браузеру делать GET
    return RedirectResponse(url=f"/robot/{robot.id}", status_code=303)


# ─── КАРТОЧКА РОБОТА ─────────────────────────────────────────────────────────

# {robot_id} — динамическая часть URL: /robot/1, /robot/2 и т.д.
# robot_id: int — FastAPI автоматически преобразует строку из URL в целое число
@app.get("/robot/{robot_id}")
def robot_detail(request: Request, robot_id: int, db: Session = Depends(get_db)):
    # Ищем робота с указанным id в базе данных
    # filter(Robot.id == robot_id) → WHERE id = robot_id в SQL
    # first() → возвращает первый результат или None (если ничего не найдено)
    robot = db.query(Robot).filter(Robot.id == robot_id).first()

    # Если робот с таким id не существует — возвращаем ошибку 404 Not Found
    if not robot:
        raise HTTPException(status_code=404, detail="Робот не найден")

    # Рендерим страницу с карточкой одного робота
    return templates.TemplateResponse(
        request,
        "detail.html",
        {
            "robot":         robot,         # объект Robot с данными из базы
            "type_labels":   TYPE_LABELS,   # для отображения русского названия типа
            "status_labels": STATUS_LABELS, # для отображения русского названия статуса
        }
    )


# ─── РЕДАКТИРОВАНИЕ ───────────────────────────────────────────────────────────

# GET /edit/{robot_id} → открывает форму редактирования с заполненными текущими данными
@app.get("/edit/{robot_id}")
def edit_form(request: Request, robot_id: int, db: Session = Depends(get_db)):
    # Ищем робота по id; если не найден — возвращаем 404
    robot = db.query(Robot).filter(Robot.id == robot_id).first()
    if not robot:
        raise HTTPException(status_code=404, detail="Робот не найден")

    # Рендерим форму редактирования, передавая текущие данные робота
    return templates.TemplateResponse(
        request,
        "edit.html",
        {
            "robot":         robot,         # текущие данные для заполнения полей формы
            "robot_types":   ROBOT_TYPES,   # список типов для <select>
            "type_labels":   TYPE_LABELS,   # русские названия типов
            "statuses":      STATUSES,      # список статусов для <select>
            "status_labels": STATUS_LABELS, # русские названия статусов
        }
    )


# POST /edit/{robot_id} → принимает изменённые данные из формы и обновляет запись в базе
@app.post("/edit/{robot_id}")
def edit_robot(
    request: Request,
    robot_id: int,                         # id робота берётся из URL
    name: str = Form(...),                 # новое название из формы
    manufacturer: str = Form(...),         # новый производитель
    robot_type: str = Form(...),           # новый тип
    payload_kg: float = Form(...),         # новая грузоподъёмность
    reach_mm: int = Form(...),             # новый радиус
    axes: int = Form(...),                 # новое количество осей
    status: str = Form(...),               # новый статус
    description: str = Form(""),           # новое описание (необязательно)
    db: Session = Depends(get_db)          # сессия базы данных
):
    # Находим робота в базе; если не найден — возвращаем 404
    robot = db.query(Robot).filter(Robot.id == robot_id).first()
    if not robot:
        raise HTTPException(status_code=404, detail="Робот не найден")

    # Обновляем каждое поле объекта — SQLAlchemy замечает изменения
    robot.name = name                              # обновляем название
    robot.manufacturer = manufacturer              # обновляем производителя
    robot.robot_type = robot_type                  # обновляем тип
    robot.payload_kg = payload_kg                  # обновляем грузоподъёмность
    robot.reach_mm = reach_mm                      # обновляем радиус
    robot.axes = axes                              # обновляем количество осей
    robot.status = status                          # обновляем статус
    # Если описание пустое — сохраняем NULL, иначе сохраняем новое описание
    robot.description = description if description else None

    # Выполняем UPDATE в базе данных: SQLAlchemy сам сформирует SQL-запрос
    db.commit()

    # Перенаправляем на карточку отредактированного робота
    return RedirectResponse(url=f"/robot/{robot.id}", status_code=303)


# ─── УДАЛЕНИЕ ─────────────────────────────────────────────────────────────────

# POST /delete/{robot_id} → удаляет робота из базы данных
# Используем POST (а не DELETE), потому что HTML-формы поддерживают только GET и POST
@app.post("/delete/{robot_id}")
def delete_robot(robot_id: int, db: Session = Depends(get_db)):
    # Ищем робота по id
    robot = db.query(Robot).filter(Robot.id == robot_id).first()
    if not robot:
        raise HTTPException(status_code=404, detail="Робот не найден")

    # Помечаем объект к удалению
    db.delete(robot)

    # Выполняем DELETE FROM robots WHERE id = robot_id в PostgreSQL
    db.commit()

    # После удаления возвращаемся на главную страницу
    return RedirectResponse(url="/", status_code=303)


# ─── БЫСТРАЯ СМЕНА СТАТУСА ────────────────────────────────────────────────────

# POST /toggle-status/{robot_id} → переключает статус робота по кругу
@app.post("/toggle-status/{robot_id}")
def toggle_status(robot_id: int, db: Session = Depends(get_db)):
    # Ищем робота по id
    robot = db.query(Robot).filter(Robot.id == robot_id).first()
    if not robot:
        raise HTTPException(status_code=404, detail="Робот не найден")

    # Словарь циклического перехода между статусами:
    # active → maintenance → retired → active → ... (по кругу)
    cycle = {"active": "maintenance", "maintenance": "retired", "retired": "active"}

    # Получаем следующий статус из словаря
    # cycle.get(robot.status, "active") — если текущий статус не найден в словаре, ставим "active"
    robot.status = cycle.get(robot.status, "active")

    # Сохраняем новый статус в базе данных (выполняется UPDATE)
    db.commit()

    # Возвращаемся на главную страницу (откуда пришёл запрос)
    return RedirectResponse(url="/", status_code=303)
