from fastapi import FastAPI, Request
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

app = FastAPI(title="Занятие 4 — Статика")

app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={}
    )


@app.get("/about", response_class=HTMLResponse)
def about(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="about.html",
        context={
            "lesson_number": 4,
            "stack": [
                "FastAPI — веб-фреймворк",
                "Jinja2 — шаблонизатор",
                "StaticFiles — раздача CSS и изображений",
                "Docker — контейнеризация",
                "PostgreSQL — база данных (следующие занятия)",
            ]
        }
    )

# Самостоятельная работа: роут для страницы /ai
@app.get("/ai", response_class=HTMLResponse)
def ai(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="ai.html",
        context={
            "facts": [
                {
                    "title": "Первая нейросеть",
                    "text": "Первый искусственный нейрон создан в 1943 году."
                },
                {
                    "title": "Что такое обучение модели",
                    "text": "Модель ML — математическая функция с миллионами параметров."
                },
                {
                    "title": "Почему GPU быстрее CPU",
                    "text": "GPU содержит тысячи ядер для параллельных вычислений матриц."
                },
            ]
        }
    )


# docker compose down
# docker rmi lesson_04-web
# docker builder prune -f
# docker compose up --build 
