from fastapi import FastAPI

app = FastAPI(title="Кулинарный справочник")


# Роут 1 — без параметров, статичные данные
@app.get("/")
def read_root():
    return {
        "project": "Кулинарный справочник",
        "version": "1.0.0",
        "status": "работает"
    }


# Роут 2 — без параметров
@app.get("/categories")
def get_categories():
    return {
        "categories": ["супы", "салаты", "выпечка", "десерты", "напитки"]
    }


# Роут 3 — один параметр типа str
@app.get("/recipe/{dish}")
def get_recipe(dish: str):
    return {
        "dish": dish,
        "message": f"Рецепт блюда '{dish}' найден"
    }


# Роут 4 — один параметр типа int
@app.get("/calories/{product}")
def get_calories(product: str):
    return {
        "product": product,
        "unit": "на 100 г",
        "note": "данные из базы"
    }


# Роут 5 — два параметра разных типов
@app.get("/price/{item}/{quantity}")
def calculate_price(item: str, quantity: int):
    return {
        "item": item,
        "quantity": quantity,
        "message": f"Расчёт стоимости: {quantity} порций блюда '{item}'"
    }

"""
localhost:8000/JSON со статусом проекта
localhost:8000/categories со списком категорий блюд
localhost:8000/recipe/борщ с сообщением о найденном рецепте для блюда "борщ"
localhost:8000/calories/яблоко с сообщением о калорийности продукта "яблоко"
localhost:8000/price/пирог/3 с сообщением о расчёте стоимости 3 порций блюда "пирог"    
""" #