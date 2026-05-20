import os
from pathlib import Path
from dotenv import load_dotenv

# override=True — пользовательские правки в .env через UI должны побеждать
# жестко прописанные значения в docker-compose.yml environment:.
load_dotenv(override=True)

# Версия прошивки робота 1T REX (отображается в шапке UI).
ROBOT_VERSION = "3.6.2"

# Папка с пользовательскими данными (БД, загруженные файлы и т. п.).
# В Docker/Coolify сюда монтируется persistent volume — переживает rebuild.
BASE_DIR     = Path(__file__).parent
INSTANCE_DIR = BASE_DIR / "instance"
INSTANCE_DIR.mkdir(exist_ok=True)

# По-умолчанию SQLite в instance/. Для Postgres задай DATABASE_URL в env.
DATABASE_URL     = os.getenv("DATABASE_URL",
                             f"sqlite:///{INSTANCE_DIR / 'vegarex.db'}")
REX_SERVER_HOST  = os.getenv("REX_SERVER_HOST", "192.168.1.100")
REX_SERVER_PORT  = int(os.getenv("REX_SERVER_PORT", "8765"))
SIMULATION_MODE  = os.getenv("SIMULATION_MODE", "1") == "1"

# Секрет для подписи cookie-сессий (cookie со значением user_id).
# В .env обязательно задать длинное случайное значение в продакшене.
SESSION_SECRET = os.getenv("SESSION_SECRET", "vegarex-default-insecure-secret-change-me")

WHISPER_MODEL    = os.getenv("WHISPER_MODEL", "base")
LANGUAGE         = "ru"
TRY_CUDA         = os.getenv("TRY_CUDA", "0") == "1"

WAKE_WORD        = "вега"
WORLD_WIDTH_CM   = float(os.getenv("WORLD_WIDTH_CM", "500"))
WORLD_HEIGHT_CM  = float(os.getenv("WORLD_HEIGHT_CM", "500"))
WALL_THICKNESS_CM = float(os.getenv("WALL_THICKNESS_CM", "5.0"))

# Размер робота (см). (x, y) робота = координаты НОСА.
# Длина учитывается при оценке свободного пространства для движения задом.
ROBOT_LENGTH_CM = float(os.getenv("ROBOT_LENGTH_CM", "20.0"))
ROBOT_WIDTH_CM  = float(os.getenv("ROBOT_WIDTH_CM",  "12.0"))

# Стартовая точка робота (просто координаты в системе мира + курс в градусах).
# Используется при очистке поля и подставляется в сгенерированный Python-код
# как константа. Имена env-переменных без `_CM` суффикса; для обратной
# совместимости со старыми .env поддерживается и `START_X_CM` / `START_Y_CM`.
START_X           = float(os.getenv("START_X",  os.getenv("START_X_CM",  "0.0")))
START_Y           = float(os.getenv("START_Y",  os.getenv("START_Y_CM",  "0.0")))
START_HEADING_DEG = float(os.getenv("START_HEADING_DEG", "0.0"))

# ── Движение ─────────────────────────────────────────────────────────────────

MOVE_SPEED   = int(os.getenv("MOVE_SPEED", "40"))    # % мощности по умолчанию
TURN_ANGLE   = int(os.getenv("TURN_ANGLE", "36"))    # угол руля в градусах (-45..45)
# Минимальная допустимая скорость в %. На реальном 1Т REX мотор не тянет
# < 30: рывки, остановки. Всякая попытка установить меньшее значение
# (через UI / голос / API) поднимается до этого порога с предупреждением.
MIN_SPEED_PCT = int(os.getenv("MIN_SPEED_PCT", "30"))

LIGHT_INDEX = int(os.getenv("LIGHT_INDEX", "0"))
# Задержка между установкой каналов R/G/B в robot.set_rgb (третий параметр).
# Default API = 1.2 с — это плавный fade; для индикации режима нам нужен
# мгновенный отклик. 0.0 = зажигание без задержки.
LIGHT_DELAY_SEC = float(os.getenv("LIGHT_DELAY_SEC", "0.0"))
LIGHT_DEFAULT_COLOR = tuple(int(x) for x in os.getenv("LIGHT_DEFAULT_COLOR", "255,255,255").split(","))

# Колесо D90: диаметр 90 мм → длина окружности π × 9 см ≈ 28.3 см за оборот
WHEEL_CIRCUMFERENCE_CM = float(os.getenv("WHEEL_CIRCUMFERENCE_CM", "28.3"))

# Скорость при 100% мощности, см/с (калибруется на реальной машинке)
SPEED_CM_PER_S_AT_100 = float(os.getenv("SPEED_CM_PER_S_AT_100", "80.0"))

# Изменение курса за один оборот колеса при полном угле руля (градусы)
# Зависит от колесной базы и радиуса поворота — уточнить при калибровке
HEADING_DEG_PER_ROT = float(os.getenv("HEADING_DEG_PER_ROT", "25.0"))

# Скорость, при которой снята калибровка поворота (%).
# При скорости ниже — дуга теснее; выше — дуга шире.
TURN_SPEED_REF = int(os.getenv("TURN_SPEED_REF", "40"))

# ── Датчик расстояния ────────────────────────────────────────────────────────

# "laser" — лазерный дальномер (обновляется каждый тик физики).
# "sonar" — ультразвуковой эхолокатор (дискретные импульсы + задержка эха).
SENSOR_TYPE       = os.getenv("SENSOR_TYPE", "laser")
SONAR_INTERVAL_MS = int(os.getenv("SONAR_INTERVAL_MS", "100"))   # мс между импульсами
SOUND_SPEED_CM_S  = 34000.0                                       # скорость звука, см/с

# ── Зоны опасности ────────────────────────────────────────────────────────────

# Радиус зоны опасности по умолчанию (см).
DANGER_ZONE_RADIUS_CM = float(os.getenv("DANGER_ZONE_RADIUS_CM", "10.0"))

