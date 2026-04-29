import json
from datetime import datetime
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


class Group(db.Model):
    __tablename__ = 'groups'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False, unique=True)
    students = db.relationship('Student', backref='group', lazy='select',
                                cascade='all, delete-orphan')


class Student(db.Model):
    __tablename__ = 'students'
    __table_args__ = (db.Index('ix_student_group_id', 'group_id'),)
    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey('groups.id'), nullable=False)
    full_name = db.Column(db.String(200), nullable=False)
    password = db.Column(db.String(20), nullable=False)
    is_online = db.Column(db.Boolean, default=False)


class Trainer(db.Model):
    __tablename__ = 'trainers'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text, default='')
    time_limit = db.Column(db.Integer, default=300)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    sentences = db.relationship('Sentence', backref='trainer', lazy='select',
                                 cascade='all, delete-orphan',
                                 order_by='Sentence.order')


class Sentence(db.Model):
    __tablename__ = 'sentences'
    __table_args__ = (db.Index('ix_sentence_trainer_id', 'trainer_id'),)
    id = db.Column(db.Integer, primary_key=True)
    trainer_id = db.Column(db.Integer, db.ForeignKey('trainers.id'), nullable=False)
    text = db.Column(db.Text, nullable=False)
    order = db.Column(db.Integer, default=0)
    correct_pos_json = db.Column(db.Text, default='[]')

    @property
    def correct_pos(self) -> list[dict]:
        return json.loads(self.correct_pos_json or '[]')

    @property
    def word_count(self) -> int:
        return len(self.correct_pos)


class TrainerResult(db.Model):
    __tablename__ = 'trainer_results'
    __table_args__ = (
        db.Index('ix_tr_student_id', 'student_id'),
        db.Index('ix_tr_trainer_id', 'trainer_id'),
        db.Index('ix_tr_completed_at', 'completed_at'),
    )
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey('students.id'), nullable=False)
    trainer_id = db.Column(db.Integer, db.ForeignKey('trainers.id'), nullable=False)
    completed_at = db.Column(db.DateTime, default=datetime.utcnow)
    total_stars = db.Column(db.Integer, default=0)
    max_stars = db.Column(db.Integer, default=0)
    percentage = db.Column(db.Float, default=0.0)
    details_json = db.Column(db.Text, default='[]')
    student = db.relationship('Student',
                             backref=db.backref('results', cascade='save-update, merge, delete', lazy='select'))
    trainer = db.relationship('Trainer',
                             backref=db.backref('results', cascade='save-update, merge, delete', lazy='select'))
