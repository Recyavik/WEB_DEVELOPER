"""
app/main7.py — главный файл FastAPI-приложения «Бортовой журнал МКС».

Страницы:
  GET  /          — главная страница (приветствие и навигация)
  GET  /log       — список всех записей журнала
  POST /log/add   — добавить новую запись
  GET  /history   — история версий БД (alembic history)
  GET  /about     — об Alembic и миграциях
"""

import subprocess                               # Для вызова команд alembic в терминале
from datetime import datetime, timezone         # Работа с датой и временем

from fastapi import FastAPI, Request, Depends, Form  # Основные компоненты FastAPI
from fastapi.responses import RedirectResponse       # Перенаправление после POST
from fastapi.templating import Jinja2Templates        # Шаблонизатор Jinja2
from fastapi.staticfiles import StaticFiles           # Раздача статических файлов
from sqlalchemy.orm import Session                    # Тип сессии SQLAlchemy

from app.database import get_db                 # Генератор сессии БД
from app.models import LogEntry                 # Модель записи в журнале

# ─── Создаём приложение ───────────────────────────────────────────────────────
app = FastAPI(
    title='Бортовой журнал МКС',                # Название для документации
    description='Занятие 7 — Alembic и миграции базы данных'
)

# ─── Статические файлы (CSS) ──────────────────────────────────────────────────
app.mount(
    '/static',                                  # URL-путь для статики
    StaticFiles(directory='app/static'),        # Папка на диске
    name='static'                               # Имя для url_for('static', path=...)
)

# ─── Шаблоны Jinja2 ──────────────────────────────────────────────────────────
templates = Jinja2Templates(directory='app/templates')  # Папка с HTML-шаблонами

# ─── Типы миссий для выпадающего списка ──────────────────────────────────────
MISSION_TYPES = {
    'science':  'Научный эксперимент',          # Эксперименты на борту
    'repair':   'Техническое обслуживание',     # Ремонт и техработы
    'comms':    'Сеанс связи',                  # Связь с ЦУП
    'medical':  'Медицинская процедура',        # Медицина на борту
    'other':    'Прочее',                       # Всё остальное
}


# ═══════════════════════════════════════════════════════════════════════════════
# ГЛАВНАЯ СТРАНИЦА
# ═══════════════════════════════════════════════════════════════════════════════

@app.get('/')
async def index(request: Request, db: Session = Depends(get_db)):
    total = db.query(LogEntry).count()
    return templates.TemplateResponse(request, 'index.html', {'total': total})


# ═══════════════════════════════════════════════════════════════════════════════
# СТРАНИЦА ЖУРНАЛА
# ═══════════════════════════════════════════════════════════════════════════════

@app.get('/log')
async def show_log(
    request: Request,
    mission: str = None,                        # Фильтр по типу миссии (необязательный)
    db: Session = Depends(get_db)               # Инъекция сессии БД
):
    """
    Страница со списком всех записей бортового журнала.
    Поддерживает фильтрацию по типу миссии через ?mission=science
    """
    # Начинаем запрос — берём все записи
    query = db.query(LogEntry)                  # SELECT * FROM log_entries

    # Если задан фильтр — добавляем WHERE
    if mission and mission in MISSION_TYPES:
        query = query.filter(                   # Добавляем фильтр
            LogEntry.mission_type == mission    # WHERE mission_type = ?
        )

    # Сортируем: новые записи сверху
    entries = query.order_by(
        LogEntry.created_at.desc()              # ORDER BY created_at DESC
    ).all()                                     # Получаем все записи

    return templates.TemplateResponse(request, 'log.html', {
        'entries': entries,
        'mission_types': MISSION_TYPES,
        'current_mission': mission,
    })


# ═══════════════════════════════════════════════════════════════════════════════
# ДОБАВЛЕНИЕ ЗАПИСИ
# ═══════════════════════════════════════════════════════════════════════════════

@app.post('/log/add')
async def add_entry(
    author: str = Form(...),                    # Имя автора из формы (обязательно)
    content: str = Form(...),                   # Текст записи из формы (обязательно)
    radiation_level: float = Form(default=0.0), # Уровень радиации (необязательно)
    mission_type: str = Form(default='other'),  # Тип миссии (по умолчанию: прочее)
    db: Session = Depends(get_db)               # Инъекция сессии БД
):
    """
    Обрабатываем форму добавления новой записи в журнал.
    После сохранения перенаправляем обратно на страницу журнала.
    """
    # Создаём объект новой записи
    entry = LogEntry(
        author=author,                          # Имя космонавта
        content=content,                        # Текст записи
        radiation_level=radiation_level,        # Уровень радиации в мкЗв/ч
        mission_type=mission_type,              # Тип операции
        created_at=datetime.now(timezone.utc)   # Текущее время (UTC, timezone-aware)
    )

    db.add(entry)                               # Добавляем объект в сессию (INSERT)
    db.commit()                                 # Сохраняем в БД (COMMIT)

    # После POST-запроса делаем редирект (паттерн POST/Redirect/GET)
    # status_code=303 означает «смотри другой адрес через GET»
    return RedirectResponse(url='/log', status_code=303)


# ═══════════════════════════════════════════════════════════════════════════════
# ИСТОРИЯ МИГРАЦИЙ
# ═══════════════════════════════════════════════════════════════════════════════

@app.get('/history')
async def migration_history(request: Request):
    """
    Страница истории миграций Alembic.
    Запускает 'alembic history' и 'alembic current' через subprocess
    и показывает результат в браузере.

    Это наглядно демонстрирует работу системы версионирования прямо в UI!
    """

    # Запускаем 'alembic current' — текущая версия БД
    try:
        result_current = subprocess.run(
            ['alembic', 'current'],             # Команда и аргументы
            capture_output=True,                # Перехватываем stdout и stderr
            text=True,                          # Декодируем как текст (не байты)
            cwd='/app'                          # Рабочая директория
        )
        current_version = result_current.stdout.strip() or 'Нет данных'
    except Exception as e:
        current_version = f'Ошибка: {e}'       # Если что-то пошло не так

    # Запускаем 'alembic history' — полная история версий
    try:
        result_history = subprocess.run(
            ['alembic', 'history', '--verbose'], # --verbose для подробного вывода
            capture_output=True,
            text=True,
            cwd='/app'
        )
        history_text = result_history.stdout.strip() or 'История пуста'
    except Exception as e:
        history_text = f'Ошибка: {e}'

    return templates.TemplateResponse(request, 'history.html', {
        'current_version': current_version,
        'history_text': history_text,
    })
