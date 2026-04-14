# Занятие 5 — Калькулятор ИМТ
## PostgreSQL · psycopg2 · SELECT / INSERT · История измерений

---

## Структура проекта

```
Lesson_05/
├── docker-compose.yml       ← два сервиса: db (PostgreSQL) + web (FastAPI)
├── Dockerfile               ← образ для FastAPI-приложения
├── requirements.txt         ← зависимости Python
└── app/
    ├── __init__.py
    ├── main5.py             ← FastAPI: роуты GET / POST
    ├── database.py          ← psycopg2: подключение, SQL-запросы
    ├── static/css/
    │   └── style.css        ← стили (палитра Lilac Mist)
    └── templates/
        ├── base.html        ← базовый шаблон с навигацией
        ├── index.html       ← главная: форма ввода
        ├── result.html      ← страница результата
        └── history.html     ← история из БД
```

---

## Запуск

```bash
# 1. Перейди в папку занятия
cd Lesson_05

# 2. Запусти (первый раз — скачает образы и соберёт контейнер)
docker compose up --build

# 3. Открой в браузере
http://localhost:8000
```

---

## Страницы

| URL | Метод | Описание |
|-----|-------|----------|
| `/` | GET | Форма ввода веса и роста |
| `/calculate` | POST | Расчёт ИМТ + сохранение в БД |
| `/history` | GET | Таблица всех измерений из БД |

---

## Как работает PostgreSQL в этом проекте

```
Браузер  →  FastAPI (main5.py)  →  database.py  →  PostgreSQL
  форма        роут /calculate      INSERT           таблица measurements
  запрос       роут /history        SELECT           возвращает строки
```

---

## Очистка Docker

```bash
docker compose down
docker rmi lesson_05-web
docker builder prune -f
docker compose up --build
```

---

## Новые концепции занятия

| Концепция | Файл | Что делает |
|-----------|------|------------|
| `psycopg2.connect()` | database.py | Открывает соединение с PostgreSQL |
| `conn.cursor()` | database.py | Создаёт «ручку» для запросов |
| `cur.execute(sql, (params,))` | database.py | Выполняет параметризованный SQL |
| `conn.commit()` | database.py | Сохраняет транзакцию |
| `cur.fetchall()` | database.py | Получает все строки результата |
| `lifespan` | main5.py | Код при старте сервера |
| `CREATE TABLE IF NOT EXISTS` | database.py | DDL: создаём таблицу |
| `INSERT INTO ... VALUES (%s)` | database.py | DML: записываем в БД |
| `SELECT ... ORDER BY ... LIMIT` | database.py | DML: читаем из БД |
