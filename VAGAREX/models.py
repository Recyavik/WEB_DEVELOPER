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
    # Алгоритм следования за рассчитанным путем:
    #   "pure_pursuit" — смотрит вперед на N см, плавно срезает углы (по умолчанию)
    #   "stanley"      — учитывает боковое смещение, тянет робота на путь точнее
    cautious_follow_algo  = Column(String(20),  default="pure_pursuit", nullable=False)
    # Замедлять ли робота на крутых поворотах (улучшает следование)
    cautious_slow_curves  = Column(Boolean,     default=True, nullable=False)

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
    # 'danger' = красная зона обстановки, 'algorithm' = желтая пунктирная зона алгоритма
    kind       = Column(String(20), default="danger", nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    active     = Column(Boolean, default=True)


class Mission(Base):
    __tablename__ = "missions"

    id          = Column(Integer, primary_key=True, index=True)
    title       = Column(String(200))
    description = Column(Text)
    target_x    = Column(Float)
    target_y    = Column(Float)
    time_limit  = Column(Integer, default=300)
    difficulty  = Column(String(20), default="easy")
    created_at  = Column(DateTime, default=datetime.utcnow)

    results = relationship("MissionResult", back_populates="mission", cascade="all, delete-orphan")


class MissionResult(Base):
    __tablename__ = "mission_results"

    id           = Column(Integer, primary_key=True, index=True)
    mission_id   = Column(Integer, ForeignKey("missions.id", ondelete="CASCADE"))
    completed_at = Column(DateTime, default=datetime.utcnow)
    duration     = Column(Float)
    success      = Column(Boolean)
    score        = Column(Float)
    notes        = Column(Text, nullable=True)

    mission = relationship("Mission", back_populates="results")
