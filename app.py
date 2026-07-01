from flask import Flask, render_template, request, redirect, url_for, session, send_file, jsonify, flash
import sqlite3
import hashlib
import io
import os
import shutil
from datetime import datetime
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib.units import cm
import pymorphy3 as _pm3
_morph = _pm3.MorphAnalyzer()

def decline_fio(full_name):
    """Склоняет ФИО в дательный падеж (кому/чему)"""""
    parts = full_name.strip().split()
    if len(parts) < 2:
        return full_name

    # Определяем род по отчеству (3-е слово) или имени (2-е слово)
    gender = None
    if len(parts) >= 3:
        otch = parts[2].lower()
        if otch.endswith('овна') or otch.endswith('евна') or otch.endswith('инична'):
            gender = 'femn'
        elif otch.endswith('ович') or otch.endswith('евич') or otch.endswith('ич'):
            gender = 'masc'

    result = []
    for word in parts:
        candidates = _morph.parse(word)
        chosen = None
        # Ищем разбор с нужным родом
        if gender:
            for p in candidates:
                if gender in p.tag.grammemes:
                    chosen = p
                    break
        if not chosen:
            chosen = candidates[0]
        inflected = chosen.inflect({'datv'})
        if inflected:
            result.append(inflected.word.capitalize())
        else:
            result.append(word)
    return ' '.join(result)

# Ищем шрифт с кириллицей автоматически
def find_font():
    candidates = [
        '/System/Library/Fonts/Supplemental/Arial Unicode.ttf',   # macOS
        '/System/Library/Fonts/Supplemental/Arial.ttf',
        '/Library/Fonts/Arial Unicode MS.ttf',
        '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',        # Linux
        '/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf',
        '/usr/share/fonts/truetype/freefont/FreeSans.ttf',
    ]
    for p in candidates:
        if os.path.exists(p):
            return p
    return None

FONT_PATH = find_font()
if FONT_PATH:
    pdfmetrics.registerFont(TTFont('CyrFont', FONT_PATH))
    pdfmetrics.registerFont(TTFont('CyrFont-Bold', FONT_PATH))
    F, FB = 'CyrFont', 'CyrFont-Bold'
else:
    F, FB = 'Helvetica', 'Helvetica-Bold'

app = Flask(__name__)
app.secret_key = 'school-spravki-secret-2026'

DB_PATH = 'school.db'
ADMIN_PASSWORD_HASH = hashlib.sha256('admin123'.encode()).hexdigest()

SPRAVKA_TYPES = [
    'по месту требования',
    'в органы социальной защиты',
    'в Пенсионный фонд',
    'в военный комиссариат',
    'в органы опеки и попечительства',
    'для получения льгот',
]

MONTHS_RU = {
    1:'января',2:'февраля',3:'марта',4:'апреля',5:'мая',6:'июня',
    7:'июля',8:'августа',9:'сентября',10:'октября',11:'ноября',12:'декабря'
}

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    conn.execute('''CREATE TABLE IF NOT EXISTS students (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        full_name TEXT NOT NULL,
        class_name TEXT NOT NULL,
        order_number TEXT NOT NULL,
        order_date TEXT NOT NULL,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS requests_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        spravka_number INTEGER,
        student_id INTEGER,
        student_name TEXT,
        class_name TEXT,
        spravka_type TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )''')
    conn.commit()
    conn.close()

def next_spravka_number(conn):
    row = conn.execute('SELECT MAX(spravka_number) FROM requests_log').fetchone()[0]
    return (row or 0) + 1

# ── Публичная часть ───────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html', spravka_types=SPRAVKA_TYPES)

@app.route('/get_spravka', methods=['POST'])
def get_spravka():
    full_name = request.form.get('full_name', '').strip()
    class_name = request.form.get('class_name', '').strip()
    spravka_type = request.form.get('spravka_type', '').strip()

    if not full_name or not class_name or not spravka_type:
        return render_template('index.html', spravka_types=SPRAVKA_TYPES,
                               error='Пожалуйста, заполните все поля.')

    conn = get_db()
    student = conn.execute(
        'SELECT * FROM students WHERE LOWER(full_name)=LOWER(?) AND LOWER(class_name)=LOWER(?)',
        (full_name, class_name)
    ).fetchone()

    if not student:
        conn.close()
        return render_template('index.html', spravka_types=SPRAVKA_TYPES,
                               error=f'Ученик «{full_name}» в классе {class_name} не найден в базе данных школы.')

    spravka_num = next_spravka_number(conn)
    now = datetime.now()
    conn.execute(
        'INSERT INTO requests_log (spravka_number, student_id, student_name, class_name, spravka_type, created_at) VALUES (?,?,?,?,?,?)',
        (spravka_num, student['id'], student['full_name'], student['class_name'], spravka_type, now.strftime('%Y-%m-%d %H:%M:%S'))
    )
    conn.commit()
    conn.close()

    return redirect(url_for('download_spravka', sid=student['id'], stype=spravka_type, snum=spravka_num))

@app.route('/spravka/download')
def download_spravka():
    sid = request.args.get('sid')
    spravka_type = request.args.get('stype', 'по месту требования')
    spravka_num = int(request.args.get('snum', 1))
    conn = get_db()
    student = conn.execute('SELECT * FROM students WHERE id=?', (sid,)).fetchone()
    conn.close()
    if not student:
        return redirect(url_for('index'))
    pdf_buffer = generate_pdf(student, spravka_type, spravka_num)
    return send_file(pdf_buffer, as_attachment=True,
                     download_name=f'spravka_{spravka_num}.pdf',
                     mimetype='application/pdf')


def generate_pdf(student, spravka_type, spravka_num):
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    w, h = A4
    now = datetime.now()
    day = now.strftime('%d')
    month = MONTHS_RU[now.month]
    year = now.strftime('%Y')

    left = 2*cm
    right = w - 2*cm
    text_w = right - left

    # Times New Roman через CyrFont (зарегистрирован из системного шрифта)
    # Для шапки используем тот же шрифт
    TN  = F    # обычный
    TNB = FB   # жирный

    # ── Шапка: текст по центру в левой части ──────────────────────────────
    cx = w / 4
    y = h - 1.5*cm

    c.setFont(TNB, 8)
    for line in [
        'МИНИСТЕРСТВО ОБРАЗОВАНИЯ ИРКУТСКОЙ',
        'ОБЛАСТИ',
        'МУНИЦИПАЛЬНОЕ КАЗЕННОЕ',
        'ОБЩЕОБРАЗОВАТЕЛЬНОЕ УЧРЕЖДЕНИЕ',
        'ИРКУТСКОГО МУНИЦИПАЛЬНОГО ОКРУГА',
    ]:
        c.drawCentredString(cx, y, line)
        y -= 0.42*cm

    c.setFont(TNB, 8.5)
    c.drawCentredString(cx, y, '«Грановская средняя')
    y -= 0.42*cm
    c.drawCentredString(cx, y, 'общеобразовательная школа»')
    y -= 0.42*cm

    c.setFont(TN, 8)
    for line in [
        'д. Грановщина, ул. Объездная, д.132А',
        'Иркутский район,',
        'Иркутская область, 664531',
        'тел./факс (3952)',
        'E-mail: nshds.granovskaja@yandex.ru',
        'http://',
        'ИНН 3827012456',
    ]:
        c.drawCentredString(cx, y, line)
        y -= 0.4*cm

    c.drawCentredString(cx, y, f'«{day}» {month} {year} г.  №  {spravka_num}')

    # ── Заголовок ─────────────────────────────────────────────────────────
    y_title = h - 9.5*cm
    c.setFont(TNB, 14)
    c.drawCentredString(w/2, y_title, 'С П Р А В К А')
    title_w = c.stringWidth('С П Р А В К А', TNB, 14)
    c.setLineWidth(0.5)
    c.line(w/2 - title_w/2, y_title - 0.15*cm, w/2 + title_w/2, y_title - 0.15*cm)

    # ── Тело справки — выравнивание по ширине через drawString с расчётом ─
    y = y_title - 1.5*cm
    c.setFont(TN, 12)
    sz = 12

    # Дана ФИО + продолжение текста в одном абзаце
    fio_datv = decline_fio(student['full_name'])
    indent = 1.25*cm  # отступ первой строки
    line_gap = 0.75*cm  # одинаковый интервал между строками

    # Строим полный текст абзаца: «Дана ФИО о том, что ... класса МОУ ИРМО «Грановская СОШ».»
    full_text = f'Дана  {fio_datv}  о том, что она (он) действительно является учеником(цей) {student["class_name"]} класса МОУ ИРМО «Грановская СОШ».'

    # Разбиваем на строки с учётом ширины страницы
    words = full_text.split()
    lines = []
    current = ''
    first_line = True
    for word in words:
        test = (current + ' ' + word).strip()
        max_w = text_w - (indent if first_line else 0)
        if c.stringWidth(test, TN, sz) <= max_w:
            current = test
        else:
            lines.append((current, first_line))
            first_line = False
            current = word
    if current:
        lines.append((current, first_line))

    for text, is_first in lines:
        x = left + (indent if is_first else 0)
        c.drawString(x, y, text)
        y -= line_gap

    y -= 0.2*cm  # небольшой отступ перед приказом

    c.drawString(left, y, f'Приказ о зачислении {student["order_number"]} от {student["order_date"]}.')
    y -= line_gap

    c.drawString(left, y, f'Справка дана {spravka_type}.')

    # ── Подпись ───────────────────────────────────────────────────────────
    y -= 2.5*cm
    c.setFont(TN, 12)
    c.drawString(left, y, 'Директор школы:')
    c.drawString(w - 5.3*cm, y, 'Н.П. Сидорина')

    # Печать — по центру страницы, перекрывает строку подписи
    stamp_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'stamp.png')
    if os.path.exists(stamp_path):
        stamp_size = 5.5*cm
        stamp_x = w/2 - stamp_size/2   # строго по центру
        stamp_y = y - stamp_size * 0.65  # перекрывает строку подписи
        c.drawImage(stamp_path, stamp_x, stamp_y, width=stamp_size, height=stamp_size, mask='auto')

    c.save()
    buffer.seek(0)
    return buffer




# ── Админ-панель ──────────────────────────────────────────────────────────────

@app.route('/admin')
def admin_login_page():
    if session.get('admin'):
        return redirect(url_for('admin_panel'))
    return render_template('admin_login.html')

@app.route('/admin/login', methods=['POST'])
def admin_login():
    password = request.form.get('password', '')
    if hashlib.sha256(password.encode()).hexdigest() == ADMIN_PASSWORD_HASH:
        session['admin'] = True
        return redirect(url_for('admin_panel'))
    return render_template('admin_login.html', error='Неверный пароль')

@app.route('/admin/logout')
def admin_logout():
    session.pop('admin', None)
    return redirect(url_for('index'))

@app.route('/admin/panel')
def admin_panel():
    if not session.get('admin'):
        return redirect(url_for('admin_login_page'))
    conn = get_db()
    students = conn.execute('SELECT * FROM students ORDER BY class_name, full_name').fetchall()
    logs = conn.execute('SELECT * FROM requests_log ORDER BY created_at DESC LIMIT 100').fetchall()
    conn.close()
    return render_template('admin_panel.html', students=students, logs=logs)

@app.route('/admin/student/add', methods=['POST'])
def admin_add_student():
    if not session.get('admin'):
        return jsonify({'error': 'Unauthorized'}), 401
    full_name = request.form.get('full_name', '').strip()
    class_name = request.form.get('class_name', '').strip()
    order_number = request.form.get('order_number', '').strip()
    order_date = request.form.get('order_date', '').strip()
    if not all([full_name, class_name, order_number, order_date]):
        flash('Заполните все поля', 'error')
        return redirect(url_for('admin_panel'))
    conn = get_db()
    conn.execute('INSERT INTO students (full_name, class_name, order_number, order_date) VALUES (?,?,?,?)',
                 (full_name, class_name, order_number, order_date))
    conn.commit()
    conn.close()
    flash(f'Ученик {full_name} добавлен', 'success')
    return redirect(url_for('admin_panel'))

@app.route('/admin/student/delete/<int:sid>', methods=['POST'])
def admin_delete_student(sid):
    if not session.get('admin'):
        return jsonify({'error': 'Unauthorized'}), 401
    conn = get_db()
    conn.execute('DELETE FROM students WHERE id=?', (sid,))
    conn.commit()
    conn.close()
    flash('Ученик удалён', 'success')
    return redirect(url_for('admin_panel'))

if __name__ == '__main__':
    init_db()
    app.run(debug=True, port=5001)
