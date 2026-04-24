# Занятие 7 — Бортовой журнал МКС
## Тема: Alembic — миграции базы данных

Учебный проект курса **Python Web Developer → ArenaRating**.

---

## Что изучаем

- Alembic — инструмент версионирования схемы БД
- `alembic init` — инициализация
- `alembic revision --autogenerate` — автогенерация миграций
- `alembic upgrade head` — применение миграций
- `alembic downgrade -1` — откат изменений
- Добавление полей к существующим таблицам без потери данных
- Интеграция Alembic с Docker Compose

---

## Три миграции проекта

| Файл | Описание |
|------|----------|
| `001_init.py` | Создаём таблицу `log_entries` (id, author, content, created_at) |
| `002_add_radiation.py` | Добавляем `radiation_level` (nullable=True) |
| `003_add_mission_type.py` | Добавляем `mission_type` (server_default='other') |

---

## Быстрый старт

```bash
# Запуск
docker compose up --build

# Открыть в браузере
http://localhost:8000

# Страницы
/          — главная
/log       — бортовой журнал + форма добавления
/history   — история миграций Alembic
/about     — об Alembic (образовательная)
```

---

## Работа с миграциями

```bash
# Посмотреть текущую версию БД
docker compose exec web alembic current

# Посмотреть историю
docker compose exec web alembic history --verbose

# Откатить последнюю миграцию
docker compose exec web alembic downgrade -1

# Применить все миграции
docker compose exec web alembic upgrade head

# Полный сброс (ОСТОРОЖНО — удаляет все данные!)
docker compose exec web alembic downgrade base
docker compose exec web alembic upgrade head
```

---

## Очистка Docker

```bash
docker compose down
docker rmi lesson_07-web
docker builder prune -f
docker compose up --build
```

---

## Стек

- **FastAPI** 0.135.3
- **Alembic** 1.18.4
- **SQLAlchemy** 2.0.49
- **PostgreSQL** 15
- **psycopg2-binary** 2.9.11
- **python-multipart** 0.0.26
- **Python** 3.12
- **Docker Desktop** 4.69
