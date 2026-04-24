FROM python:3.12-slim

# Устанавливаем рабочую директорию внутри контейнера
WORKDIR /app

# Копируем файл зависимостей первым — чтобы Docker кэшировал слой
COPY requirements.txt .

# Устанавливаем все зависимости Python
RUN pip install --no-cache-dir -r requirements.txt

# Копируем весь остальной код проекта
COPY . .

# ВАЖНО: сначала применяем миграции Alembic, потом запускаем сервер
# Если миграции уже применены — Alembic просто пропустит их, это безопасно
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main7:app --host 0.0.0.0 --port 8000 --reload"]
