FROM python:3.12-slim

# Устанавливаем рабочую директорию внутри контейнера
WORKDIR /app

# Копируем список зависимостей
COPY requirements.txt .

# Устанавливаем зависимости
RUN pip install --no-cache-dir -r requirements.txt

# Копируем весь проект
COPY . .

# Запускаем наш сервер — файл main5.py внутри папки app
CMD ["uvicorn", "app.main5_hw:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
