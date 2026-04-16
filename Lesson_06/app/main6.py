from fastapi import FastAPI, Request, Depends, Form, HTTPException
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

# Импортируем наши модули
from app.database import engine, get_db
from app.models import Base, Athlete

# Создаём приложение FastAPI
app = FastAPI(title="Fitness Club — Карточки спортсменов")

# Подключаем статичные файлы (CSS)
app.mount("/static", StaticFiles(directory="app/static"), name="static")

# Подключаем шаблоны Jinja2
templates = Jinja2Templates(directory="app/templates")


# Создаём все таблицы в базе данных при запуске
@app.on_event("startup")
def startup():
    # create_all создаёт таблицы если их ещё нет
    Base.metadata.create_all(bind=engine)


# ─── ГЛАВНАЯ СТРАНИЦА — список всех спортсменов ───────────────────────────────

@app.get("/")
def index(request: Request, db: Session = Depends(get_db)):
    # READ: читаем всех спортсменов из базы
    athletes = db.query(Athlete).order_by(Athlete.id).all()
    return templates.TemplateResponse(
        request, "index.html",
        {"athletes": athletes}
    )


# ─── ФОРМА СОЗДАНИЯ НОВОГО СПОРТСМЕНА ────────────────────────────────────────

@app.get("/create")
def create_form(request: Request):
    # Просто отображаем пустую форму
    return templates.TemplateResponse(request, "create.html")


@app.post("/create")
def create_athlete(
    request: Request,
    name: str = Form(...),
    sport: str = Form(...),
    age: int = Form(...),
    weight: float = Form(...),
    height: float = Form(...),
    level: str = Form(...),
    goals: str = Form(""),
    db: Session = Depends(get_db)
):
    # CREATE: создаём объект модели
    athlete = Athlete(
        name=name,
        sport=sport,
        age=age,
        weight=weight,
        height=height,
        level=level,
        goals=goals if goals else None
    )
    # Добавляем в сессию (ещё не сохранено)
    db.add(athlete)
    # Сохраняем в базу данных
    db.commit()
    # Обновляем объект (получаем id из базы)
    db.refresh(athlete)
    # Перенаправляем на карточку нового спортсмена
    return RedirectResponse(url=f"/athlete/{athlete.id}", status_code=303)


# ─── КАРТОЧКА СПОРТСМЕНА ──────────────────────────────────────────────────────

@app.get("/athlete/{athlete_id}")
def athlete_detail(request: Request, athlete_id: int, db: Session = Depends(get_db)):
    # READ: ищем спортсмена по id
    athlete = db.query(Athlete).filter(Athlete.id == athlete_id).first()
    if not athlete:
        # Если не нашли — ошибка 404
        raise HTTPException(status_code=404, detail="Спортсмен не найден")
    return templates.TemplateResponse(
        request, "detail.html",
        {"athlete": athlete}
    )


# ─── ФОРМА РЕДАКТИРОВАНИЯ ─────────────────────────────────────────────────────

@app.get("/edit/{athlete_id}")
def edit_form(request: Request, athlete_id: int, db: Session = Depends(get_db)):
    # Находим спортсмена для заполнения формы текущими данными
    athlete = db.query(Athlete).filter(Athlete.id == athlete_id).first()
    if not athlete:
        raise HTTPException(status_code=404, detail="Спортсмен не найден")
    return templates.TemplateResponse(
        request, "edit.html",
        {"athlete": athlete}
    )


@app.post("/edit/{athlete_id}")
def edit_athlete(
    request: Request,
    athlete_id: int,
    name: str = Form(...),
    sport: str = Form(...),
    age: int = Form(...),
    weight: float = Form(...),
    height: float = Form(...),
    level: str = Form(...),
    goals: str = Form(""),
    db: Session = Depends(get_db)
):
    # UPDATE: находим спортсмена
    athlete = db.query(Athlete).filter(Athlete.id == athlete_id).first()
    if not athlete:
        raise HTTPException(status_code=404, detail="Спортсмен не найден")

    # Обновляем поля объекта
    athlete.name = name
    athlete.sport = sport
    athlete.age = age
    athlete.weight = weight
    athlete.height = height
    athlete.level = level
    athlete.goals = goals if goals else None

    # Сохраняем изменения в базе
    db.commit()
    # Перенаправляем на обновлённую карточку
    return RedirectResponse(url=f"/athlete/{athlete.id}", status_code=303)


# ─── УДАЛЕНИЕ СПОРТСМЕНА ──────────────────────────────────────────────────────

@app.post("/delete/{athlete_id}")
def delete_athlete(athlete_id: int, db: Session = Depends(get_db)):
    # DELETE: находим спортсмена
    athlete = db.query(Athlete).filter(Athlete.id == athlete_id).first()
    if not athlete:
        raise HTTPException(status_code=404, detail="Спортсмен не найден")

    # Удаляем из базы
    db.delete(athlete)
    # Сохраняем изменение
    db.commit()
    # Возвращаемся на главную
    return RedirectResponse(url="/", status_code=303)
