from datetime import datetime
from sqlalchemy import Column, Integer, String, Float, DateTime, Text, Boolean, ForeignKey
from sqlalchemy.orm import relationship
from database import Base


class User(Base):
    __tablename__ = "users"

    id            = Column(Integer, primary_key=True, index=True)
    username      = Column(String(64), unique=True, nullable=False, index=True)
    password_hash = Column(String(255), nullable=False)
    created_at    = Column(DateTime, default=datetime.utcnow)
    is_admin      = Column(Boolean, default=False, nullable=False)

    # Временный пароль, выданный администратором при сбросе.
    # Виден админу в /admin до тех пор, пока пользователь сам не сменит пароль.
    # NULL = пользователь сам владеет паролем (админ его НЕ знает).
    temp_password            = Column(String(255), nullable=True)
    password_changed_by_user = Column(Boolean, default=True, nullable=False)


class AppSettings(Base):
    """Глобальные настройки приложения (одна строка с id=1)."""
    __tablename__ = "app_settings"

    id                = Column(Integer, primary_key=True, default=1)
    registration_open = Column(Boolean, default=True, nullable=False)


class PublishedRoute(Base):
    """Опубликованный маршрут робота — Python-программа, доступная всем
    пользователям. Можно загрузить себе, изменить и переопубликовать."""
    __tablename__ = "published_routes"

    id          = Column(Integer, primary_key=True, index=True)
    author_id   = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"),
                         nullable=True, index=True)
    title       = Column(String(120), nullable=False)
    description = Column(Text,        nullable=True)
    code        = Column(Text,        nullable=False)         # полный текст с # CMD маркерами
    cmd_count   = Column(Integer,     default=0, nullable=False)
    parent_id   = Column(Integer, ForeignKey("published_routes.id", ondelete="SET NULL"),
                         nullable=True)                       # форкнуто из другого маршрута
    created_at  = Column(DateTime,    default=datetime.utcnow, nullable=False)

    author = relationship("User", foreign_keys=[author_id])
    parent = relationship("PublishedRoute", foreign_keys=[parent_id], remote_side=[id])


class SavedRoute(Base):
    """Личный сохраненный маршрут пользователя — закладка в его пространстве.
    Видит и удаляет только владелец. Можно загрузить себе обратно или
    позже опубликовать в общий каталог."""
    __tablename__ = "saved_routes"

    id          = Column(Integer, primary_key=True, index=True)
    owner_id    = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"),
                         nullable=False, index=True)
    title       = Column(String(120), nullable=False)
    description = Column(Text,        nullable=True)
    code        = Column(Text,        nullable=False)
    cmd_count   = Column(Integer,     default=0, nullable=False)
    created_at  = Column(DateTime,    default=datetime.utcnow, nullable=False)
    updated_at  = Column(DateTime,    default=datetime.utcnow, onupdate=datetime.utcnow,
                         nullable=False)


class UserSettings(Base):
    """Симуляционные/аппаратные настройки конкретного пользователя.
    Создается при первом входе с дефолтами из config."""
    __tablename__ = "user_settings"

    user_id            = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"),
                                primary_key=True)

    # Соединение с реальным роботом
    rex_host           = Column(String(120), default="192.168.1.100")
    rex_port           = Column(Integer,     default=8765)
    simulation_mode    = Column(Boolean,     default=True)

    # Движение
    move_speed         = Column(Integer,     default=40)
    turn_angle         = Column(Integer,     default=36)

    # Калибровка
    wheel_circ_cm      = Column(Float,       default=28.3)
    speed_at_100       = Column(Float,       default=80.0)
    heading_per_rot    = Column(Float,       default=25.0)
    turn_speed_ref     = Column(Integer,     default=40)

    # Поле и стенки
    world_w_cm         = Column(Float,       default=500.0)
    world_h_cm         = Column(Float,       default=500.0)
    wall_thickness_cm  = Column(Float,       default=5.0)

    # Размер робота
    robot_length_cm    = Column(Float,       default=20.0)
    robot_width_cm     = Column(Float,       default=12.0)

    # Стартовая точка
    start_x_cm         = Column(Float,       default=0.0)
    start_y_cm         = Column(Float,       default=0.0)
    start_heading_deg  = Column(Float,       default=0.0)

    # Датчик расстояния
    sensor_type        = Column(String(20),  default="laser")
    sonar_interval_ms  = Column(Integer,     default=100)

    # Прочее
    danger_zone_radius = Column(Float,       default=10.0)

    # Размер ячейки сетки A* для планировщика обхода зон в режиме «осторожно».
    # Меньше — точнее путь, медленнее счет. 10 см — хороший баланс.
    path_cell_size_cm  = Column(Integer,     default=10, nullable=False)
    # Алгоритм автопилота: "polyline" — ломаная (face+forward),
    # "smooth" — сглаженная (Чайкин + set_course-дуги).
    autopilot_algo     = Column(String(20),  default="polyline", nullable=False)
    # Алгоритм обхода зон в режиме «осторожно»:
    #   "pure_pursuit" — A* + Чайкин + следование по точке впереди (плавная дуга)
    #   "stanley"      — A* + Чайкин + Stanley (учитывает боковое смещение)
    #   "linear"       — A*-углы + face+forward (прямые с поворотом на месте)
    #   "manual"       — стоп + пауза exec, ждём команд от пользователя
    # При неудаче автоматических (pure_pursuit/stanley/linear) — fallback в manual.
    cautious_follow_algo  = Column(String(20),  default="pure_pursuit", nullable=False)
    # Замедлять ли робота на крутых поворотах (улучшает следование)
    cautious_slow_curves  = Column(Boolean,     default=True, nullable=False)
    # Стратегия разворота в тесном пространстве (когда K-turn не помещается
    # по габаритам):
    #   "backoff"    — отъехать назад на нужное расстояние, выполнить один
    #                  большой K-turn, компенсировать отъезд (по умолчанию).
    #                  Быстрее, но требует ≥80-100 см свободного места.
    #   "multi_step" — разбить разворот на N маленьких K-turn'ов; N
    #                  подбирается автоматически по доступному месту.
    #                  Каждый маленький K-turn возвращается в свою точку —
    #                  ноль накопленного дрейфа. Влезает в 8-20 см впереди,
    #                  но идёт ×N дольше.
    #   "manual"     — НЕ отъезжать. Если впереди мало места — стоп с
    #                  сообщением; пользователь сам разруливает.
    wall_turn_strategy    = Column(String(20),  default="backoff", nullable=False)

    # Батарея: на сколько минут активного движения хватает полного заряда.
    # 60 = «на 1 час». Настройка симулятора, реальный робот может игнорировать.
    battery_minutes    = Column(Integer,     default=60, nullable=False)
    # Текущий заряд батареи (0..100). Сохраняется между сессиями и
    # перезапусками сервера: «зарядка» сбрасывается ТОЛЬКО кнопкой
    # «🔋 Зарядить», а не открытием/закрытием страницы.
    battery_pct        = Column(Float,       default=100.0, nullable=False)


class RobotSession(Base):
    __tablename__ = "robot_sessions"

    id         = Column(Integer, primary_key=True, index=True)
    user_id    = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"),
                        index=True, nullable=True)
    started_at = Column(DateTime, default=datetime.utcnow)
    ended_at   = Column(DateTime, nullable=True)
    robot_name = Column(String(100), default="1T REX")
    notes      = Column(Text, nullable=True)
    simulated  = Column(Boolean, default=True)

    commands    = relationship("CommandLog",  back_populates="session", cascade="all, delete-orphan")
    path_points = relationship("PathPoint",   back_populates="session", cascade="all, delete-orphan")


class CommandLog(Base):
    __tablename__ = "command_logs"

    id         = Column(Integer, primary_key=True, index=True)
    session_id = Column(Integer, ForeignKey("robot_sessions.id", ondelete="CASCADE"))
    timestamp  = Column(DateTime, default=datetime.utcnow)
    raw_text   = Column(String(500))
    intent     = Column(String(100))
    success    = Column(Boolean, default=True)
    error_msg  = Column(String(300), nullable=True)

    session = relationship("RobotSession", back_populates="commands")


class PathPoint(Base):
    __tablename__ = "path_points"

    id         = Column(Integer, primary_key=True, index=True)
    session_id = Column(Integer, ForeignKey("robot_sessions.id", ondelete="CASCADE"))
    timestamp  = Column(DateTime, default=datetime.utcnow)
    x          = Column(Float)
    y          = Column(Float)
    heading    = Column(Float)

    session = relationship("RobotSession", back_populates="path_points")


class ProgramCommand(Base):
    __tablename__ = "program_commands"

    id        = Column(Integer, primary_key=True, index=True)
    user_id   = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=True)
    order_num = Column(Integer, index=True)
    raw_text  = Column(String(500))
    intent    = Column(String(100))
    label     = Column(String(200))
    code      = Column(String(500))


class DangerZone(Base):
    __tablename__ = "danger_zones"

    id         = Column(Integer, primary_key=True, index=True)
    user_id    = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=True)
    label      = Column(String(200), default="Опасная зона")
    x          = Column(Float)
    y          = Column(Float)
    radius     = Column(Float, default=50.0)
    # 'danger' = опасная зона обстановки, 'algorithm' = желтая пунктирная зона алгоритма
    kind       = Column(String(20), default="danger", nullable=False)
    # Порядковый номер зоны В ПРЕДЕЛАХ своего kind для данного user_id —
    # «опасная #1», «опасная #2», «внимания #1», … Назначается ОДИН раз
    # при создании (max+1), не переиспользуется после удаления. Виден
    # только в сообщениях журнала.
    display_no = Column(Integer, default=0, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    active     = Column(Boolean, default=True)


class Mission(Base):
    """Сгенерированная миссия для робота: траектория из waypoints, опасные
    зоны, обязательные действия (поставить/удалить зоны), эталонное решение.
    Создаётся пользователем (через генератор) и может быть опубликована
    для прохождения другими."""
    __tablename__ = "missions"

    id          = Column(Integer, primary_key=True, index=True)
    owner_id    = Column(Integer,
                         ForeignKey("users.id", ondelete="CASCADE"),
                         nullable=False, index=True)
    title       = Column(String(200), nullable=False)
    description = Column(Text, nullable=True)
    level       = Column(Integer, nullable=False)        # 1..5

    # Геометрия и цели — JSON-поля.
    # waypoints: [[x, y], ...] — точки, которые надо посетить (в любом порядке).
    # danger_zones: [[x, y, r], ...] — пред-расставленные опасные зоны
    #               (рисуются красным, удаляются только по action_required).
    # actions_required: [{type, x, y, r}, ...] — обязательные действия:
    #   {"type": "place_attention", "x": ..., "y": ..., "r": ...}
    #   {"type": "remove_danger",   "x": ..., "y": ...}
    #   {"type": "remove_attention","x": ..., "y": ...}
    waypoints        = Column(Text, nullable=False)
    danger_zones     = Column(Text, nullable=False, default="[]")
    actions_required = Column(Text, nullable=False, default="[]")
    # Полная траектория для визуализации (плотная цепочка точек, по
    # которой проходит дашед-линия превью). Для кастомных миссий —
    # сэмпл path_history. Для сгенерированных — обычно совпадает с
    # waypoints (линии между ними). [] = используем waypoints.
    path             = Column(Text, nullable=False, default="[]")
    # Набор препятствий, при которых строилась траектория (кастомные
    # миссии): JSON-список ⊆ ["danger", "attention"]. Решающий миссию
    # должен пройти её с теми же галочками «Препятствия». Стены — всегда.
    # [] = только стены.
    obstacles        = Column(Text, nullable=False, default="[]")

    # Эталонное решение (видно админу как «подсказка»).
    reference_voice = Column(Text, nullable=True)        # JSON: список голосовых фраз
    reference_code  = Column(Text, nullable=True)        # готовый Python-код

    # Снимок safety_margin на момент генерации — чтобы оценка была
    # одинаковой у всех проходящих миссию, даже если глобальная настройка
    # сменилась.
    safety_margin_cm = Column(Float, nullable=False, default=5.0)

    published   = Column(Boolean, default=False, nullable=False)
    created_at  = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at  = Column(DateTime, default=datetime.utcnow,
                         onupdate=datetime.utcnow, nullable=False)

    owner = relationship("User", foreign_keys=[owner_id])
    runs  = relationship("MissionRun",
                         back_populates="mission",
                         cascade="all, delete-orphan")


class MissionRun(Base):
    """Один прогон миссии пользователем. Записывается при завершении или
    отмене. Хранит набранные звёзды, итоговое «Качество прохождения»,
    выполненные действия и посещённые точки."""
    __tablename__ = "mission_runs"

    id            = Column(Integer, primary_key=True, index=True)
    mission_id    = Column(Integer,
                           ForeignKey("missions.id", ondelete="CASCADE"),
                           nullable=False, index=True)
    user_id       = Column(Integer,
                           ForeignKey("users.id", ondelete="CASCADE"),
                           nullable=False, index=True)
    started_at    = Column(DateTime, default=datetime.utcnow, nullable=False)
    completed_at  = Column(DateTime, nullable=True)

    stars             = Column(Integer, default=0, nullable=False)
    # «Качество прохождения» 0..1. Имя столбца историческое (был
    # coefficient точности) — переименование потребовало бы миграции.
    coefficient       = Column(Float,   default=0.0, nullable=False)
    deviations        = Column(Integer, default=0, nullable=False)
    duration_sec      = Column(Float,   default=0.0, nullable=False)    # время задания: активация → стоп
    algo_duration_sec = Column(Float,   default=0.0, nullable=False)    # время алгоритма: ▶ Запуск → конец очереди
    waypoints_visited = Column(Text,    default="[]", nullable=False)   # JSON: индексы
    actions_done      = Column(Text,    default="[]", nullable=False)   # JSON: индексы
    success           = Column(Boolean, default=False, nullable=False)

    mission = relationship("Mission", back_populates="runs")
    user    = relationship("User", foreign_keys=[user_id])
