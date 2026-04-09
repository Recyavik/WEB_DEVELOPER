from fastapi import FastAPI, Request
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse
import sys

app = FastAPI(title="Занятие 2 — Jinja2")

# Указываем папку с шаблонами
# __file__ — путь к текущему файлу (main.py)
# Папка templates находится рядом с main.py
templates = Jinja2Templates(directory="app/templates")


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    # Передаём данные в шаблон через словарь context
    # "request" обязателен — Jinja2 использует его для генерации URL
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "name": "Разработчик",
            "project_name": "Python Web Developer",
            "python_version": f"{sys.version_info.major}.{sys.version_info.minor}",
        }
    )


@app.get("/about", response_class=HTMLResponse)
def about(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="about.html",
        context={
            "lesson_number": 2,
            "stack": [
                "FastAPI — веб-фреймворк",
                "Uvicorn — ASGI-сервер",
                "Jinja2 — шаблонизатор",
                "Docker — контейнеризация",
                "PostgreSQL — база данных (следующие занятия)",
            ]
        }
    )


# Роут с параметром — имя из URL подставляется в шаблон
@app.get("/hello/{name}", response_class=HTMLResponse)
def hello(request: Request, name: str):
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "name": name,
            "project_name": "Python Web Developer",
            "python_version": f"{sys.version_info.major}.{sys.version_info.minor}",
        }
    )

# cd M:\WEB_DEVELOPER\Lesson_02
# docker compose up --build