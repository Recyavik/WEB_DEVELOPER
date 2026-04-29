import json
import os
import random
import sqlite3
import string
from datetime import datetime
from functools import wraps

from flask import Flask, flash, jsonify, redirect, render_template, request, session, url_for
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import joinedload

from models import Group, Sentence, Student, Trainer, TrainerResult, db
from morpho import ALL_POS, POS_COLORS, analyze_sentence
import book as booklib

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'morpho-trening-2024-xK9p')
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///morpho.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db.init_app(app)


@event.listens_for(Engine, "connect")
def _set_sqlite_pragmas(dbapi_conn, _):
    if isinstance(dbapi_conn, sqlite3.Connection):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA cache_size=-32000")   # 32 МБ кэш страниц
        cur.execute("PRAGMA temp_store=MEMORY")
        cur.execute("PRAGMA mmap_size=268435456") # 256 МБ memory-mapped I/O
        cur.close()

TEACHER_PASSWORD = 'teacher'


def generate_password(length: int = 8) -> str:
    chars = string.ascii_letters + string.digits
    return ''.join(random.choices(chars, k=length))


def teacher_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if session.get('role') != 'teacher':
            return redirect(url_for('teacher_login'))
        return f(*args, **kwargs)
    return decorated


def student_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if session.get('role') != 'student':
            return redirect(url_for('student_login'))
        return f(*args, **kwargs)
    return decorated


# ── Home ──────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')


# ── Teacher: Auth ─────────────────────────────────────────────────────────────

@app.route('/teacher/login', methods=['GET', 'POST'])
def teacher_login():
    if request.method == 'POST':
        if request.form.get('password') == TEACHER_PASSWORD:
            session['role'] = 'teacher'
            return redirect(url_for('teacher_dashboard'))
        flash('Неверный пароль', 'error')
    return render_template('teacher/login.html')


@app.route('/teacher/logout')
def teacher_logout():
    session.clear()
    return redirect(url_for('index'))


# ── Teacher: Dashboard ────────────────────────────────────────────────────────

@app.route('/teacher/')
@teacher_required
def teacher_dashboard():
    return render_template('teacher/dashboard.html',
                           total_groups=Group.query.count(),
                           total_students=Student.query.count(),
                           total_trainers=Trainer.query.count(),
                           total_results=TrainerResult.query.count(),
                           recent_results=TrainerResult.query
                               .options(
                                   joinedload(TrainerResult.student).joinedload(Student.group),
                                   joinedload(TrainerResult.trainer))
                               .order_by(TrainerResult.completed_at.desc())
                               .limit(10).all())


# ── Teacher: Groups & Students ────────────────────────────────────────────────

@app.route('/teacher/groups')
@teacher_required
def teacher_groups():
    groups = Group.query.order_by(Group.name).all()
    return render_template('teacher/groups.html', groups=groups)


@app.route('/teacher/groups/add', methods=['POST'])
@teacher_required
def add_group():
    name = request.form.get('name', '').strip()
    if not name:
        flash('Введите название группы', 'error')
    elif Group.query.filter_by(name=name).first():
        flash(f'Группа «{name}» уже существует', 'error')
    else:
        db.session.add(Group(name=name))
        db.session.commit()
        flash(f'Группа «{name}» создана', 'success')
    return redirect(url_for('teacher_groups'))


@app.route('/teacher/groups/<int:gid>/delete', methods=['POST'])
@teacher_required
def delete_group(gid):
    g = db.get_or_404(Group, gid)
    db.session.delete(g)
    db.session.commit()
    flash(f'Группа «{g.name}» удалена', 'success')
    return redirect(url_for('teacher_groups'))


@app.route('/teacher/students/add', methods=['POST'])
@teacher_required
def add_student():
    group_id = request.form.get('group_id', type=int)
    full_name = request.form.get('full_name', '').strip()
    if group_id and full_name:
        pwd = generate_password()
        db.session.add(Student(group_id=group_id, full_name=full_name, password=pwd))
        db.session.commit()
        flash(f'Ученик «{full_name}» добавлен', 'success')
    else:
        flash('Заполните все поля', 'error')
    return redirect(url_for('teacher_groups'))


@app.route('/teacher/students/<int:sid>/delete', methods=['POST'])
@teacher_required
def delete_student(sid):
    s = db.get_or_404(Student, sid)
    db.session.delete(s)
    db.session.commit()
    flash(f'Ученик «{s.full_name}» удалён', 'success')
    return redirect(url_for('teacher_groups'))


@app.route('/teacher/students/<int:sid>/regen', methods=['POST'])
@teacher_required
def regen_password(sid):
    s = db.get_or_404(Student, sid)
    s.password = generate_password()
    db.session.commit()
    flash(f'Новый пароль для «{s.full_name}»: {s.password}', 'success')
    return redirect(url_for('teacher_groups'))


# ── Teacher: Trainers ─────────────────────────────────────────────────────────

@app.route('/teacher/trainers')
@teacher_required
def teacher_trainers():
    trainers = Trainer.query.order_by(Trainer.created_at.desc()).all()
    return render_template('teacher/trainers.html', trainers=trainers)


@app.route('/teacher/trainers/add', methods=['POST'])
@teacher_required
def add_trainer():
    name = request.form.get('name', '').strip()
    description = request.form.get('description', '').strip()
    time_limit = request.form.get('time_limit', 300, type=int)
    if not name:
        flash('Введите название тренажёра', 'error')
        return redirect(url_for('teacher_trainers'))
    t = Trainer(name=name, description=description, time_limit=time_limit)
    db.session.add(t)
    db.session.commit()
    flash(f'Тренажёр «{name}» создан', 'success')
    return redirect(url_for('teacher_trainer_detail', tid=t.id))


@app.route('/teacher/trainers/<int:tid>', methods=['GET', 'POST'])
@teacher_required
def teacher_trainer_detail(tid):
    trainer = db.get_or_404(Trainer, tid)
    if request.method == 'POST':
        action = request.form.get('action')

        if action == 'update':
            trainer.name = request.form.get('name', trainer.name).strip() or trainer.name
            trainer.description = request.form.get('description', '').strip()
            trainer.time_limit = request.form.get('time_limit', trainer.time_limit, type=int)
            db.session.commit()
            flash('Тренажёр обновлён', 'success')

        elif action == 'add_sentence':
            text = request.form.get('text', '').strip()
            if not text:
                flash('Введите текст предложения', 'error')
            else:
                analysis = analyze_sentence(text)
                if not analysis:
                    flash('Предложение не содержит слов для анализа', 'error')
                else:
                    order = db.session.query(db.func.count(Sentence.id)).filter_by(trainer_id=tid).scalar()
                    s = Sentence(trainer_id=tid, text=text, order=order,
                                 correct_pos_json=json.dumps(analysis, ensure_ascii=False))
                    db.session.add(s)
                    db.session.commit()
                    flash('Предложение добавлено', 'success')

    return render_template('teacher/trainer_detail.html',
                           trainer=trainer, pos_colors=POS_COLORS,
                           book_loaded=booklib.book_exists())


@app.route('/teacher/trainers/<int:tid>/reanalyze', methods=['POST'])
@teacher_required
def reanalyze_trainer(tid):
    trainer = db.get_or_404(Trainer, tid)
    for s in trainer.sentences:
        s.correct_pos_json = json.dumps(analyze_sentence(s.text), ensure_ascii=False)
    db.session.commit()
    flash(f'Разбор пересчитан для {len(trainer.sentences)} предложений', 'success')
    return redirect(url_for('teacher_trainer_detail', tid=tid))


@app.route('/teacher/trainers/<int:tid>/delete', methods=['POST'])
@teacher_required
def delete_trainer(tid):
    t = db.get_or_404(Trainer, tid)
    db.session.delete(t)
    db.session.commit()
    flash(f'Тренажёр «{t.name}» удалён', 'success')
    return redirect(url_for('teacher_trainers'))


@app.route('/teacher/sentences/<int:sid>/delete', methods=['POST'])
@teacher_required
def delete_sentence(sid):
    s = db.get_or_404(Sentence, sid)
    tid = s.trainer_id
    db.session.delete(s)
    db.session.commit()
    flash('Предложение удалено', 'success')
    return redirect(url_for('teacher_trainer_detail', tid=tid))


@app.route('/teacher/sentences/<int:sid>/edit', methods=['POST'])
@teacher_required
def edit_sentence(sid):
    s = db.get_or_404(Sentence, sid)
    text = request.form.get('text', '').strip()
    if text:
        s.text = text
        s.correct_pos_json = json.dumps(analyze_sentence(text), ensure_ascii=False)
        db.session.commit()
        flash('Предложение обновлено', 'success')
    return redirect(url_for('teacher_trainer_detail', tid=s.trainer_id))


# ── Teacher: Book ────────────────────────────────────────────────────────────

@app.route('/teacher/book')
@teacher_required
def teacher_book():
    return render_template('teacher/book.html', info=booklib.book_info())


@app.route('/teacher/book/upload', methods=['POST'])
@teacher_required
def upload_book():
    f = request.files.get('book_file')
    if not f or not f.filename:
        flash('Выберите файл', 'error')
        return redirect(url_for('teacher_book'))
    fname = f.filename.lower()
    if not (fname.endswith('.txt') or fname.endswith('.pdf')):
        flash('Поддерживаются только .txt и .pdf файлы', 'error')
        return redirect(url_for('teacher_book'))
    try:
        count = booklib.save_book_from_upload(f, fname)
        flash(f'Книга загружена: найдено {count} предложений', 'success')
    except Exception as e:
        flash(f'Ошибка обработки файла: {e}', 'error')
    return redirect(url_for('teacher_book'))


@app.route('/teacher/book/delete', methods=['POST'])
@teacher_required
def delete_book():
    booklib.delete_book()
    flash('Книга удалена', 'success')
    return redirect(url_for('teacher_book'))


@app.route('/teacher/api/random-sentence')
@teacher_required
def api_random_sentence():
    min_w = request.args.get('min_words', 5, type=int)
    max_w = request.args.get('max_words', 15, type=int)
    if min_w > max_w:
        return jsonify({'error': 'Минимум не может быть больше максимума', 'sentence': None})
    sentence = booklib.get_random_sentence(min_w, max_w)
    if sentence is None:
        return jsonify({'error': 'Нет подходящих предложений в книге', 'sentence': None})
    return jsonify({'sentence': sentence})


# ── Teacher: Stats ────────────────────────────────────────────────────────────

@app.route('/teacher/stats')
@teacher_required
def teacher_stats():
    groups = Group.query.order_by(Group.name).all()
    trainers = Trainer.query.order_by(Trainer.name).all()

    query = (TrainerResult.query
             .join(Student).join(Group)
             .options(
                 joinedload(TrainerResult.student).joinedload(Student.group),
                 joinedload(TrainerResult.trainer)))

    group_id = request.args.get('group_id', type=int)
    student_id = request.args.get('student_id', type=int)
    trainer_id = request.args.get('trainer_id', type=int)
    date_from = request.args.get('date_from', '')
    date_to = request.args.get('date_to', '')

    if group_id:
        query = query.filter(Student.group_id == group_id)
    if student_id:
        query = query.filter(TrainerResult.student_id == student_id)
    if trainer_id:
        query = query.filter(TrainerResult.trainer_id == trainer_id)
    if date_from:
        query = query.filter(TrainerResult.completed_at >= date_from)
    if date_to:
        query = query.filter(TrainerResult.completed_at <= date_to + ' 23:59:59')

    results = query.order_by(TrainerResult.completed_at.desc()).all()

    students_for_group = []
    if group_id:
        students_for_group = Student.query.filter_by(group_id=group_id).order_by(Student.full_name).all()

    return render_template('teacher/stats.html',
                           results=results, groups=groups, trainers=trainers,
                           students_for_group=students_for_group,
                           sel_group=group_id, sel_student=student_id,
                           sel_trainer=trainer_id,
                           date_from=date_from, date_to=date_to)


@app.route('/teacher/results/<int:rid>/delete', methods=['POST'])
@teacher_required
def delete_result(rid):
    r = db.get_or_404(TrainerResult, rid)
    db.session.delete(r)
    db.session.commit()
    flash('Результат удалён', 'success')
    return redirect(request.referrer or url_for('teacher_stats'))


@app.route('/about')
def about():
    return render_template('about.html')


@app.route('/teacher/reset-sessions', methods=['POST'])
@teacher_required
def reset_sessions():
    Student.query.update({'is_online': False})
    db.session.commit()
    flash('Все сессии учеников сброшены', 'success')
    return redirect(url_for('teacher_dashboard'))


@app.route('/teacher/stats/students-by-group')
@teacher_required
def teacher_students_by_group():
    group_id = request.args.get('group_id', type=int)
    if not group_id:
        return jsonify([])
    students = Student.query.filter_by(group_id=group_id).order_by(Student.full_name).all()
    return jsonify([{'id': s.id, 'name': s.full_name} for s in students])


# ── Student: Auth ─────────────────────────────────────────────────────────────

@app.route('/student/login', methods=['GET', 'POST'])
def student_login():
    groups = Group.query.order_by(Group.name).all()
    if request.method == 'POST':
        group_id = request.form.get('group_id', type=int)
        student_id = request.form.get('student_id', type=int)
        password = request.form.get('password', '').strip()

        student = Student.query.filter_by(id=student_id, group_id=group_id).first()
        if not student:
            flash('Ученик не найден', 'error')
            return render_template('student/login.html', groups=groups)
        if student.password != password:
            flash('Неверный пароль', 'error')
            return render_template('student/login.html', groups=groups)
        if student.is_online:
            flash('Предыдущая сессия была сброшена. Добро пожаловать!', 'warning')

        student.is_online = True
        db.session.commit()
        session['role'] = 'student'
        session['student_id'] = student.id
        session['student_name'] = student.full_name
        return redirect(url_for('student_dashboard'))

    return render_template('student/login.html', groups=groups)


@app.route('/student/logout')
def student_logout():
    sid = session.get('student_id')
    if sid:
        s = db.session.get(Student, sid)
        if s:
            s.is_online = False
            db.session.commit()
    session.clear()
    return redirect(url_for('index'))


# ── Student: Dashboard ────────────────────────────────────────────────────────

@app.route('/student/')
@student_required
def student_dashboard():
    student = db.session.get(Student, session['student_id'])
    trainers = Trainer.query.order_by(Trainer.name).all()
    recent = (TrainerResult.query
              .filter_by(student_id=student.id)
              .options(joinedload(TrainerResult.trainer))
              .order_by(TrainerResult.completed_at.desc())
              .limit(5).all())
    return render_template('student/dashboard.html',
                           student=student, trainers=trainers, recent=recent)


# ── Student: Exercise ─────────────────────────────────────────────────────────

@app.route('/student/exercise/<int:tid>')
@student_required
def student_exercise(tid):
    trainer = db.get_or_404(Trainer, tid)
    if not trainer.sentences:
        flash('В этом тренажёре пока нет предложений', 'error')
        return redirect(url_for('student_dashboard'))

    sentences_data = []
    for s in trainer.sentences:
        analysis = s.correct_pos
        sentences_data.append({
            'id': s.id,
            'text': s.text,
            'words': [
                {'word': item['word'], 'index': i, 'correct_pos': item['pos']}
                for i, item in enumerate(analysis)
            ]
        })

    return render_template('student/exercise.html',
                           trainer=trainer,
                           sentences_json=json.dumps(sentences_data, ensure_ascii=False),
                           pos_colors_json=json.dumps(POS_COLORS, ensure_ascii=False),
                           all_pos=ALL_POS,
                           pos_colors=POS_COLORS)


@app.route('/student/submit-exercise', methods=['POST'])
@student_required
def submit_exercise():
    data = request.get_json()
    trainer_id = data.get('trainer_id')
    results = data.get('results', [])

    # Server-side verification
    total_stars = 0
    max_stars = 0
    verified_results = []

    for r in results:
        sentence = db.session.get(Sentence, r.get('sentence_id'))
        if not sentence:
            continue
        correct = sentence.correct_pos
        max_s = len(correct)
        stars = 0
        word_details = []
        answers = r.get('answers', {})

        for i, item in enumerate(correct):
            student_pos = answers.get(str(i), '')
            is_correct = student_pos == item['pos']
            if is_correct:
                stars += 1
            word_details.append({
                'word': item['word'],
                'student_pos': student_pos,
                'correct_pos': item['pos'],
                'correct': is_correct,
            })

        total_stars += stars
        max_stars += max_s
        verified_results.append({
            'sentence_id': sentence.id,
            'sentence_text': sentence.text,
            'stars': stars,
            'max_stars': max_s,
            'word_details': word_details,
        })

    percentage = round(total_stars / max_stars * 100, 1) if max_stars > 0 else 0.0

    tr = TrainerResult(
        student_id=session['student_id'],
        trainer_id=trainer_id,
        total_stars=total_stars,
        max_stars=max_stars,
        percentage=percentage,
        details_json=json.dumps(verified_results, ensure_ascii=False),
    )
    db.session.add(tr)
    db.session.commit()
    return jsonify({'result_id': tr.id})


# ── Student: Results & Stats ──────────────────────────────────────────────────

@app.route('/student/results/<int:rid>')
@student_required
def student_results(rid):
    result = db.get_or_404(TrainerResult, rid)
    if result.student_id != session.get('student_id'):
        return redirect(url_for('student_dashboard'))
    details = json.loads(result.details_json or '[]')
    return render_template('student/results.html',
                           result=result, details=details,
                           pos_colors=POS_COLORS)


@app.route('/student/stats')
@student_required
def student_stats():
    results = (TrainerResult.query
               .filter_by(student_id=session['student_id'])
               .options(joinedload(TrainerResult.trainer))
               .order_by(TrainerResult.completed_at.desc())
               .all())
    return render_template('student/stats.html', results=results)


# ── Public API ────────────────────────────────────────────────────────────────

@app.route('/api/students-by-group')
def api_students_by_group():
    group_id = request.args.get('group_id', type=int)
    if not group_id:
        return jsonify([])
    students = (Student.query.filter_by(group_id=group_id)
                .order_by(Student.full_name).all())
    return jsonify([{'id': s.id, 'name': s.full_name} for s in students])


# ── Init ──────────────────────────────────────────────────────────────────────

with app.app_context():
    db.create_all()

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
