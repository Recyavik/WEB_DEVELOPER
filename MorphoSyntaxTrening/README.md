# MorphoSyntaxTrening

Интерактивный тренажёр морфосинтаксического анализа — веб-приложение для обучения школьников определению частей речи в русских предложениях.

**Хакатон 2026** · Версия 2.1

---

## Описание

Учитель создаёт тренажёры из предложений (вручную или из загруженной книги), назначает их группам учеников. Ученик входит по коду учителя, выбирает своё имя и проходит тренажёр: перетаскивает или кликает части речи на слова. После завершения — детальный разбор ошибок с цветовой индикацией.

### Ключевые возможности

- Автоматическое определение частей речи через **pymorphy3**
- Интерфейс перетаскивания (drag-and-drop) и клика
- Таймер выполнения с автозавершением
- Визуальная обратная связь: правые 20% блока окрашены в цвет ошибочного ответа
- Статистика учеников и групп для учителя
- Генерация предложений из загруженной книги (fb2 / txt)
- Рассылка приглашений по email (SMTP)
- Docker-деплой

---

## Стек технологий

| Компонент | Технология |
|-----------|-----------|
| Backend | FastAPI + Starlette SessionMiddleware |
| ORM | SQLAlchemy (sync), SQLite |
| NLP | pymorphy3 (русский морфологический анализатор) |
| Frontend | Jinja2 шаблоны, vanilla JS, CSS |
| Email | smtplib (SSL / STARTTLS) |
| Деплой | Docker + docker-compose |

---

## Роли и доступ

| Роль | URL | Описание |
|------|-----|----------|
| **Администратор** | `/admin/` | Управление учителями, настройки SMTP, смена пароля |
| **Учитель** | `/teacher/` | Группы, ученики, тренажёры, книга, статистика |
| **Ученик** | `/student/` | Вход по коду учителя, прохождение тренажёров |

---

## Структура проекта

```
MorphoSyntaxTrening/
├── main.py               # Все FastAPI-маршруты
├── models.py             # ORM-модели (Teacher, Group, Student, Trainer, Sentence, TrainerResult)
├── morpho.py             # Обёртка pymorphy3, POS_COLORS, ALL_POS
├── book.py               # Парсинг и извлечение предложений из книги
├── smtp_settings.py      # Загрузка/сохранение SMTP-настроек (instance/smtp_settings.json)
├── email_service.py      # Отправка писем (приглашения, сброс пароля, тест)
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
├── start.sh              # Точка входа контейнера (фиксит DNS Docker Desktop)
├── static/
│   └── style.css
├── templates/
│   ├── base.html
│   ├── index.html
│   ├── admin/            # dashboard, teachers, settings, login
│   ├── teacher/          # dashboard, groups, trainers, trainer_detail, book, stats, export
│   └── student/          # login, dashboard, exercise, results, stats, forgot_password
└── instance/
    ├── morpho.db          # SQLite база данных
    └── smtp_settings.json # SMTP-конфигурация
```

---

## Быстрый старт

### Локально (без Docker)

```bash
cd MorphoSyntaxTrening

# Установить зависимости
pip install -r requirements.txt

# Запустить
uvicorn main:app --reload --port 5000
```

Открыть: http://localhost:5000

### В Docker

```bash
docker-compose up --build
```

Открыть: http://localhost:5000

---

## Первый запуск

1. Перейти на http://localhost:5000/admin/login
2. Войти с логином `admin` и паролем из переменной `ADMIN_PASSWORD` в `.env` (по умолчанию `admin`)
3. Создать учителя на вкладке **Учителя**
4. Настроить SMTP на вкладке **Безопасность** (для отправки приглашений)

---

## Переменные окружения (`.env`)

```env
SECRET_KEY=your-secret-key-here
ADMIN_PASSWORD=admin
```

- `SECRET_KEY` — секрет для подписи сессий (Starlette SessionMiddleware)
- `ADMIN_PASSWORD` — пароль администратора по умолчанию при первом запуске

---

## SMTP (email)

Настраивается через интерфейс администратора (`/admin/settings`).

**Яндекс:**
- Хост: `smtp.yandex.ru`, порт `465`, тип `SSL`
- Логин: полный адрес ящика (`user@yandex.ru`)
- Пароль: **пароль приложения** (не пароль от аккаунта) — создаётся на [id.yandex.ru](https://id.yandex.ru)
- В настройках почты должен быть включён **IMAP**

**Mail.ru:** хост `smtp.mail.ru`, порт `465`, SSL, пароль приложения

**Gmail:** хост `smtp.gmail.com`, порт `587`, STARTTLS, пароль приложения (2FA обязательна)

---

## Известные особенности

- **Паролей менеджер** в браузере не вызывается на странице входа ученика — поле пароля создаётся динамически через JS
- **Docker Desktop / Windows** — DNS внутри контейнера исправляется через `start.sh` (перезаписывает `/etc/resolv.conf` на `8.8.8.8`)
- **bcrypt** должен быть версии `<4.0` — passlib 1.7.4 несовместим с bcrypt ≥ 4.0

---

## Части речи

Тренажёр работает с 13 частями речи:

`Существительное` · `Прилагательное` · `Глагол` · `Наречие` · `Местоимение` · `Предлог` · `Союз` · `Частица` · `Причастие` · `Деепричастие` · `Числительное` · `Междометие`

Каждая часть речи имеет уникальный цвет, используемый во всём интерфейсе.
