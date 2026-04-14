@echo off
chcp 65001 >nul
echo ================================================
echo  Занятие 5 — Калькулятор ИМТ
echo  Создание структуры проекта Lesson_05
echo ================================================
echo.

echo [1/2] Создаём папки...
mkdir Lesson_05
mkdir Lesson_05\app
mkdir Lesson_05\app\static
mkdir Lesson_05\app\static\css
mkdir Lesson_05\app\templates
echo      OK

echo [2/2] Создаём файлы...
type nul > Lesson_05\docker-compose.yml
type nul > Lesson_05\Dockerfile
type nul > Lesson_05\requirements.txt
type nul > Lesson_05\app\__init__.py
type nul > Lesson_05\app\database.py
type nul > Lesson_05\app\main5.py
type nul > Lesson_05\app\static\css\style.css
type nul > Lesson_05\app\templates\base.html
type nul > Lesson_05\app\templates\index.html
type nul > Lesson_05\app\templates\result.html
type nul > Lesson_05\app\templates\history.html
echo      OK

echo.
tree Lesson_05 /F
echo.
echo ================================================
echo  Готово! Заполни файлы кодом из конспекта.
echo  Версия: psycopg2-binary==2.9.11
echo ================================================
pause
