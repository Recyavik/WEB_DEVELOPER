# Импортируем класс FastAPI из библиотеки fastapi
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
import random as rnd

# Создаём экземпляр приложения.
# title= отображается в автодокументации по адресу /docs
app = FastAPI(title="Урок 1 — Первый сервер")


# Сохраняем путь к папке, где лежит текущий файл main.py.
# Через него удобно находить HTML-файл рядом с кодом приложения.
BASE_DIR = Path(__file__).resolve().parent


# Декоратор @app.get("/") регистрирует функцию как обработчик
# HTTP GET-запросов на адрес "/"  (то есть главная страница)
@app.get("/")
def read_root():
    """
    Эта строка станет описанием в документации /docs.
    Функция возвращает словарь — FastAPI автоматически
    преобразует его в JSON.
    """
    return {
        "message": "Привет, мир!",
        "status": "сервер работает",
        "lesson": 1
    }


# Второй роут — страница "о проекте"
# Обратите внимание: каждый путь уникален
@app.get("/about")
def about():
    return {
        "project": "Python Web Developer Course",
        "framework": "FastAPI",
        "lesson": 1
    }


# Этот роут возвращает не JSON, а полноценную HTML-страницу.
# response_class=HTMLResponse подсказывает FastAPI,
# что ответ нужно отдать браузеру как HTML-документ.
@app.get("/html-lesson", response_class=HTMLResponse)
def html_lesson():
    # Считываем учебную страницу из отдельного файла.
    # encoding="utf-8" нужен, чтобы русский текст читался корректно.
    html_path = BASE_DIR / "html_lesson_page.html"
    return html_path.read_text(encoding="utf-8")


# Роут с параметром в пути.
# {name} — это переменная, FastAPI автоматически
# передаёт её в функцию как аргумент
@app.get("/hello/{name}")
def say_hello(name: str):
    return {"message": f"Привет, {name}!"}

@app.get("/multiply")
def multiply():
    a = rnd.randint(1, 10)
    b = rnd.randint(1, 10)
    return {"Пример": f"{a} * {b} = {a * b}"}

@app.get("/recipe/{dish}")
def get_recipe(dish: str):
    return {"dish": dish, "status": "рецепт найден"}

@app.get("/calories/{product}/{grams}")
def calories(product: str, grams: int):
    return {"product": product, "grams": grams}
