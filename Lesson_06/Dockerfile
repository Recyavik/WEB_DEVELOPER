FROM python:3.12-slim

# Рабочая директория внутри контейнера
WORKDIR /app

# Копируем список зависимостей
COPY requirements.txt .

# Устанавливаем зависимости
RUN pip install --no-cache-dir -r requirements.txt

# Копируем весь код проекта
COPY . .

# Запускаем сервер — файл main6.py в папке app
CMD ["uvicorn", "app.main6:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
