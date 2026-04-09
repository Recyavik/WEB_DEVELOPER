# Form — специальный класс для чтения данных из HTML-формы
from fastapi import FastAPI, Request, Form
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse
 
app = FastAPI(title="Занятие 3 — Формы")
templates = Jinja2Templates(directory="app/templates")
 
 
# GET-роут — просто показываем форму
@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={}
    )
 
 
# POST-роут — получаем данные формы и показываем результат
# @app.post — роут реагирует только на POST-запросы
@app.post("/submit", response_class=HTMLResponse)
def submit(
    request: Request,
    # Form(...) — читать значение из тела POST-запроса
    # Три точки (...) означают: поле обязательно
    # Имя параметра должно совпадать с name= в HTML-форме
    username: str = Form(...),
    email: str = Form(...),
    # FastAPI автоматически преобразует строку '25' в число 25
    age: int = Form(...)
):
    return templates.TemplateResponse(
        request=request,
        name="result.html",
        context={
            "username": username,
            "email": email,
            "age": age,
        }
    )
