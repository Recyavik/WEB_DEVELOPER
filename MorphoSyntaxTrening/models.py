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
    created_at = Column(DateTime, default=datetime.utcnow)

    teacher = relationship("Teacher", back_populates="trainers")
    sentences = relationship("Sentence", back_populates="trainer",
                             cascade="all, delete-orphan",
                             order_by="Sentence.order")


class Sentence(Base):
    __tablename__ = "sentences"
    __table_args__ = (Index("ix_sentence_trainer_id", "trainer_id"),)
    id = Column(Integer, primary_key=True)
    trainer_id = Column(Integer, ForeignKey("trainers.id"), nullable=False)
    text = Column(Text, nullable=False)
    order = Column(Integer, default=0)
    correct_pos_json = Column(Text, default="[]")

    trainer = relationship("Trainer", back_populates="sentences")

    @property
    def correct_pos(self) -> list[dict]:
        return json.loads(self.correct_pos_json or "[]")

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
