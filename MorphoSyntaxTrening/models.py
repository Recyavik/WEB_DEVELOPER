import json
from datetime import datetime
from sqlalchemy import (Boolean, Column, DateTime, Float, ForeignKey,
                        Index, Integer, String, Text, UniqueConstraint)
from sqlalchemy.orm import relationship
from database import Base


class Admin(Base):
    __tablename__ = "admins"
    id = Column(Integer, primary_key=True)
    username = Column(String(100), unique=True, nullable=False)
    password_hash = Column(String(200), nullable=False)


class Teacher(Base):
    __tablename__ = "teachers"
    id = Column(Integer, primary_key=True)
    code = Column(String(6), unique=True, nullable=False, index=True)
    name = Column(String(200), nullable=False)
    email = Column(String(200), unique=True, nullable=False)
    password_hash = Column(String(200), nullable=False)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    groups = relationship("Group", back_populates="teacher", cascade="all, delete-orphan")
    trainers = relationship("Trainer", back_populates="teacher", cascade="all, delete-orphan")


class Group(Base):
    __tablename__ = "groups"
    __table_args__ = (
        UniqueConstraint("teacher_id", "name", name="uq_group_teacher_name"),
        Index("ix_group_teacher_id", "teacher_id"),
    )
    id = Column(Integer, primary_key=True)
    teacher_id = Column(Integer, ForeignKey("teachers.id"), nullable=False)
    name = Column(String(100), nullable=False)

    teacher = relationship("Teacher", back_populates="groups")
    students = relationship("Student", back_populates="group", cascade="all, delete-orphan")


class Student(Base):
    __tablename__ = "students"
    __table_args__ = (Index("ix_student_group_id", "group_id"),)
    id = Column(Integer, primary_key=True)
    group_id = Column(Integer, ForeignKey("groups.id"), nullable=False)
    full_name = Column(String(200), nullable=False)
    email = Column(String(200), nullable=True, unique=True)
    password = Column(String(20), nullable=False)
    is_online = Column(Boolean, default=False)

    group = relationship("Group", back_populates="students")
    results = relationship("TrainerResult", back_populates="student",
                           cascade="all, delete-orphan")


class Trainer(Base):
    __tablename__ = "trainers"
    __table_args__ = (Index("ix_trainer_teacher_id", "teacher_id"),)
    id = Column(Integer, primary_key=True)
    teacher_id = Column(Integer, ForeignKey("teachers.id"), nullable=False)
    name = Column(String(200), nullable=False)
    description = Column(Text, default="")
    time_limit = Column(Integer, default=300)
    # introductory | elementary | basic | advanced | expert
    level = Column(String(20), default="introductory")
    # 0 = show all sentences; N > 0 = show only N sentences
    max_sentences = Column(Integer, default=0)
    # whether to randomise sentence order for each student
    shuffle = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    teacher = relationship("Teacher", back_populates="trainers")
    sentences = relationship("Sentence", back_populates="trainer",
                             cascade="all, delete-orphan",
                             order_by="Sentence.order")
    group_links = relationship("TrainerGroup", back_populates="trainer",
                               cascade="all, delete-orphan")


class TrainerGroup(Base):
    """Many-to-many: trainer assigned to group.
    If a trainer has no rows here → available to ALL groups of that teacher."""
    __tablename__ = "trainer_groups"
    __table_args__ = (
        UniqueConstraint("trainer_id", "group_id", name="uq_trainer_group"),
        Index("ix_tg_trainer_id", "trainer_id"),
        Index("ix_tg_group_id",   "group_id"),
    )
    id         = Column(Integer, primary_key=True)
    trainer_id = Column(Integer, ForeignKey("trainers.id"), nullable=False)
    group_id   = Column(Integer, ForeignKey("groups.id"),   nullable=False)

    trainer = relationship("Trainer", back_populates="group_links")
    group   = relationship("Group")


class Sentence(Base):
    __tablename__ = "sentences"
    __table_args__ = (Index("ix_sentence_trainer_id", "trainer_id"),)
    id = Column(Integer, primary_key=True)
    trainer_id = Column(Integer, ForeignKey("trainers.id"), nullable=False)
    text = Column(Text, nullable=False)
    order = Column(Integer, default=0)
    # kept for backward-compat with Ознакомительный level
    correct_pos_json = Column(Text, default="[]")
    # draft | analyzed | reviewed
    status = Column(String(20), default="analyzed")
    # full morphological analysis (list[TokenAnalysis] as JSON)
    analysis_json = Column(Text, default="[]")
    # teacher corrections over ai analysis (same schema, nullable)
    teacher_analysis_json = Column(Text, nullable=True)

    trainer = relationship("Trainer", back_populates="sentences")

    @property
    def correct_pos(self) -> list[dict]:
        return json.loads(self.correct_pos_json or "[]")

    @property
    def analysis(self) -> list[dict]:
        return json.loads(self.analysis_json or "[]")

    @property
    def teacher_analysis(self) -> list[dict] | None:
        if self.teacher_analysis_json is None:
            return None
        return json.loads(self.teacher_analysis_json)

    @property
    def final_analysis(self) -> list[dict]:
        """Return teacher-corrected analysis if available, else AI analysis."""
        return self.teacher_analysis or self.analysis

    @property
    def word_count(self) -> int:
        return len(self.correct_pos)


class TrainerResult(Base):
    __tablename__ = "trainer_results"
    __table_args__ = (
        Index("ix_tr_student_id", "student_id"),
        Index("ix_tr_trainer_id", "trainer_id"),
        Index("ix_tr_completed_at", "completed_at"),
    )
    id = Column(Integer, primary_key=True)
    student_id = Column(Integer, ForeignKey("students.id"), nullable=False)
    trainer_id = Column(Integer, ForeignKey("trainers.id"), nullable=False)
    completed_at = Column(DateTime, default=datetime.utcnow)
    total_stars = Column(Integer, default=0)
    max_stars = Column(Integer, default=0)
    percentage = Column(Float, default=0.0)
    details_json = Column(Text, default="[]")

    student = relationship("Student", back_populates="results")
    trainer = relationship("Trainer",
                           backref="results")
