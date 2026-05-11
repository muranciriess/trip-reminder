from flask import Flask, render_template, request, jsonify, redirect, url_for, flash, g, send_from_directory, Response, session
from functools import wraps
import requests as http_requests
from apscheduler.schedulers.background import BackgroundScheduler
import sqlite3
import os
import json
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo
import atexit
import re

import config
import wechat_bot

app = Flask(__name__)
app.secret_key = config.SECRET_KEY
app.config['EVENING_REMINDER_HOUR'] = config.EVENING_REMINDER_HOUR
app.config['EVENING_REMINDER_MINUTE'] = config.EVENING_REMINDER_MINUTE


# ── 管理后台登录验证 ──────────────────────────────────────

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('admin_logged_in'):
            return redirect(url_for('admin_login'))
        return f(*args, **kwargs)
    return decorated

TZ = ZoneInfo(config.TIMEZONE)
WEEKDAYS = ['周一', '周二', '周三', '周四', '周五', '周六', '周日']

# ── 数据库 ──────────────────────────────────────────────

def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(config.DATABASE, timeout=10)
        g.db.row_factory = sqlite3.Row
        g.db.execute('PRAGMA journal_mode=WAL')
        g.db.execute('PRAGMA foreign_keys=ON')
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop('db', None)
    if db:
        db.close()


def get_raw_db():
    db = sqlite3.connect(config.DATABASE, timeout=10)
    db.row_factory = sqlite3.Row
    return db


def init_db():
    db = sqlite3.connect(config.DATABASE, timeout=10)
    db.executescript('''
        CREATE TABLE IF NOT EXISTS trip (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            trip_date TEXT NOT NULL,
            time_start TEXT,
            time_end TEXT,
            location_from TEXT,
            location_to TEXT,
            personnel TEXT,
            weather TEXT,
            notes TEXT,
            remind_evening INTEGER DEFAULT 1,
            remind_before_min INTEGER DEFAULT 60,
            status TEXT DEFAULT 'active',
            created_at TEXT DEFAULT (datetime('now','localtime')),
            updated_at TEXT DEFAULT (datetime('now','localtime'))
        );

        CREATE TABLE IF NOT EXISTS checklist_item (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trip_id INTEGER NOT NULL REFERENCES trip(id) ON DELETE CASCADE,
            content TEXT NOT NULL,
            sort_order INTEGER DEFAULT 0,
            checked INTEGER DEFAULT 0,
            checked_at TEXT,
            checked_by TEXT
        );

        CREATE TABLE IF NOT EXISTS reminder_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trip_id INTEGER REFERENCES trip(id),
            remind_type TEXT,
            sent_at TEXT DEFAULT (datetime('now','localtime')),
            status TEXT,
            response TEXT
        );

        CREATE TABLE IF NOT EXISTS weekly_event (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trip_id INTEGER REFERENCES trip(id) ON DELETE CASCADE,
            event_date TEXT NOT NULL,
            event_time TEXT,
            event_title TEXT NOT NULL,
            event_note TEXT,
            sort_order INTEGER DEFAULT 0
        );
    ''')

    for col_def in ['mode TEXT DEFAULT "structured"', 'custom_content TEXT']:
        try:
            db.execute(f'ALTER TABLE trip ADD COLUMN {col_def}')
        except Exception:
            pass

    db.executescript('''
        CREATE TABLE IF NOT EXISTS daily_task (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            remind_time TEXT DEFAULT '09:00',
            active INTEGER DEFAULT 1,
            created_at TEXT DEFAULT (datetime('now','localtime'))
        );
        CREATE TABLE IF NOT EXISTS daily_task_item (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL REFERENCES daily_task(id) ON DELETE CASCADE,
            content TEXT NOT NULL,
            sort_order INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS daily_checkin (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id INTEGER NOT NULL REFERENCES daily_task_item(id) ON DELETE CASCADE,
            checkin_date TEXT NOT NULL,
            checked_by TEXT NOT NULL,
            checked_at TEXT DEFAULT (datetime('now','localtime')),
            UNIQUE(item_id, checkin_date, checked_by)
        );
    ''')

    db.close()


def query_db(query, args=(), one=False):
    cur = get_db().execute(query, args)
    rv = cur.fetchall()
    cur.close()
    return (rv[0] if rv else None) if one else rv


# ── 消息格式化 ──────────────────────────────────────────

def get_weekday(d):
    if isinstance(d, str):
        d = date.fromisoformat(d)
    return WEEKDAYS[d.weekday()]


def format_freeform_message(trip, checklist_items):
    lines = []
    if trip['custom_content']:
        lines.append(trip['custom_content'])

    if checklist_items:
        lines.append('')
        lines.append('⚠️ 准备检查')
        for i, item in enumerate(checklist_items):
            check = "☑" if item['checked'] else "☐"
            lines.append(f"    {check} {i+1}. {item['content']}")
        unchecked = [f"{i+1}" for i, item in enumerate(checklist_items) if not item['checked']]
        if unchecked:
            lines.append(f"\n💬 回复「打卡 {unchecked[0]}」完成对应项目")

    return '\n'.join(lines)


def format_daily_task_message(task, items, checkins_today=None):
    lines = [f"📝 {task['title']}", "━━━━━━━━━━"]

    for i, item in enumerate(items):
        done_by = []
        if checkins_today:
            done_by = [c['checked_by'] for c in checkins_today if c['item_id'] == item['id']]
        if done_by:
            lines.append(f"    ☑ {i+1}. {item['content']}（{'、'.join(done_by)}）")
        else:
            lines.append(f"    ☐ {i+1}. {item['content']}")

    lines.append("")
    lines.append("💬 完成后回复「每日打卡 1」打卡对应项目")
    return '\n'.join(lines)


def format_trip_message(trip, checklist_items, weekly_events=None, msg_type='evening'):
    if trip['mode'] == 'freeform':
        return format_freeform_message(trip, checklist_items)

    trip_date = date.fromisoformat(trip['trip_date'])
    today = datetime.now(TZ).date()
    wd = get_weekday(trip_date)

    lines = []

    if msg_type == 'evening':
        lines.append(f"🌙 {today.month}月{today.day}日 {get_weekday(today)} 晚间提醒")
        lines.append("━━━━━━━━━━")
        lines.append("")
        lines.append(f"📅 明日安排（{trip_date.month}月{trip_date.day}日 {wd}）")
    elif msg_type == 'morning':
        lines.append(f"☀️ {today.month}月{today.day}日 {get_weekday(today)} 早间提醒")
        lines.append("━━━━━━━━━━")
        lines.append("")
        lines.append(f"📅 今日安排（{trip_date.month}月{trip_date.day}日 {wd}）")
    elif msg_type == 'before':
        lines.append(f"⏰ 出发提醒 — {trip_date.month}月{trip_date.day}日 {wd}")
        lines.append("━━━━━━━━━━")
    else:
        lines.append(f"📅 {trip_date.month}月{trip_date.day}日 {wd} 行程安排")
        lines.append("━━━━━━━━━━")

    lines.append("")

    time_str = ""
    if trip['time_start']:
        time_str = trip['time_start']
        if trip['time_end']:
            time_str += f"-{trip['time_end']}"
    if time_str:
        lines.append(f"🍅 {time_str}  {trip['title']}")
    else:
        lines.append(f"🍅 {trip['title']}")

    if trip['location_from'] or trip['location_to']:
        if trip['location_from'] and trip['location_to']:
            lines.append(f"    ✈️  {trip['location_from']} → {trip['location_to']}")
        elif trip['location_to']:
            lines.append(f"    📍 {trip['location_to']}")
        else:
            lines.append(f"    📍 {trip['location_from']}")

    if trip['personnel']:
        lines.append(f"    👥 {trip['personnel']}")

    lines.append("")

    if trip['weather']:
        lines.append(f"🌤 {trip['weather']}")
        lines.append("")

    if trip['notes']:
        for note_line in trip['notes'].split('\n'):
            if note_line.strip():
                lines.append(f"    {note_line.strip()}")
        lines.append("")

    if checklist_items:
        lines.append("⚠️ 准备检查")
        for i, item in enumerate(checklist_items):
            check = "☑" if item['checked'] else "☐"
            lines.append(f"    {check} {i+1}. {item['content']}")
        lines.append("")
        unchecked = [f"{i+1}" for i, item in enumerate(checklist_items) if not item['checked']]
        if unchecked:
            lines.append(f"💬 回复「打卡 {unchecked[0]}」完成对应项目")

    if weekly_events:
        lines.append("")
        lines.append("📅 本周后续")
        for ev in weekly_events:
            ev_date = date.fromisoformat(ev['event_date'])
            time_part = f" {ev['event_time']}" if ev['event_time'] else ""
            note_part = f" {ev['event_note']}" if ev['event_note'] else ""
            lines.append(f"    {ev_date.month}/{ev_date.day}{time_part}  {ev['event_title']}{note_part}")

    return '\n'.join(lines)


# ── 微信群消息推送 ──────────────────────────────────────

def send_to_wechat(message_text, trip_id=None):
    result = wechat_bot.send_to_group(message_text)

    if trip_id:
        db = get_raw_db()
        db.execute(
            'INSERT INTO reminder_log (trip_id, remind_type, status, response) VALUES (?, ?, ?, ?)',
            (trip_id, 'manual', result['status'], json.dumps(result, ensure_ascii=False))
        )
        db.commit()
        db.close()

    return result



# ── 定时任务 ────────────────────────────────────────────

scheduler = BackgroundScheduler(timezone=config.TIMEZONE)


def evening_reminder_job():
    db = get_raw_db()
    tomorrow = (datetime.now(TZ) + timedelta(days=1)).date().isoformat()
    trips = db.execute(
        'SELECT * FROM trip WHERE trip_date = ? AND remind_evening = 1 AND status = ?',
        (tomorrow, 'active')
    ).fetchall()

    for trip in trips:
        already = db.execute(
            "SELECT 1 FROM reminder_log WHERE trip_id = ? AND remind_type = 'evening' AND date(sent_at) = date('now','localtime')",
            (trip['id'],)
        ).fetchone()
        if already:
            continue

        items = db.execute(
            'SELECT * FROM checklist_item WHERE trip_id = ? ORDER BY sort_order', (trip['id'],)
        ).fetchall()
        events = db.execute(
            'SELECT * FROM weekly_event WHERE trip_id = ? ORDER BY event_date, sort_order', (trip['id'],)
        ).fetchall()

        msg = format_trip_message(trip, items, events, 'evening')
        result = wechat_bot.send_to_group(msg)

        db.execute(
            'INSERT INTO reminder_log (trip_id, remind_type, status, response) VALUES (?, ?, ?, ?)',
            (trip['id'], 'evening', result.get('status', 'unknown'),
             json.dumps(result, ensure_ascii=False))
        )
        db.commit()
    db.close()


def morning_reminder_job():
    db = get_raw_db()
    today_str = datetime.now(TZ).date().isoformat()

    # 今日行程提醒
    trips = db.execute(
        'SELECT * FROM trip WHERE trip_date = ? AND remind_evening = 1 AND status = ?',
        (today_str, 'active')
    ).fetchall()

    for trip in trips:
        already = db.execute(
            "SELECT 1 FROM reminder_log WHERE trip_id = ? AND remind_type = 'morning' AND date(sent_at) = date('now','localtime')",
            (trip['id'],)
        ).fetchone()
        if already:
            continue

        items = db.execute(
            'SELECT * FROM checklist_item WHERE trip_id = ? ORDER BY sort_order', (trip['id'],)
        ).fetchall()
        events = db.execute(
            'SELECT * FROM weekly_event WHERE trip_id = ? ORDER BY event_date, sort_order', (trip['id'],)
        ).fetchall()

        msg = format_trip_message(trip, items, events, 'morning')
        result = wechat_bot.send_to_group(msg)
        db.execute(
            'INSERT INTO reminder_log (trip_id, remind_type, status, response) VALUES (?, ?, ?, ?)',
            (trip['id'], 'morning', result.get('status', 'unknown'),
             json.dumps(result, ensure_ascii=False))
        )
        db.commit()

    # 每日任务也一起推送
    tasks = db.execute('SELECT * FROM daily_task WHERE active = 1').fetchall()
    for task in tasks:
        already = db.execute(
            "SELECT 1 FROM reminder_log WHERE trip_id = ? AND remind_type = 'daily_morning' AND date(sent_at) = date('now','localtime')",
            (task['id'],)
        ).fetchone()
        if already:
            continue
        items = db.execute(
            'SELECT * FROM daily_task_item WHERE task_id = ? ORDER BY sort_order', (task['id'],)
        ).fetchall()
        if not items:
            continue
        msg = format_daily_task_message(task, items)
        result = wechat_bot.send_to_group(msg)
        db.execute(
            'INSERT INTO reminder_log (trip_id, remind_type, status, response) VALUES (?, ?, ?, ?)',
            (task['id'], 'daily_morning', result.get('status', 'unknown'),
             json.dumps(result, ensure_ascii=False))
        )
        db.commit()
    db.close()


def before_trip_reminder_job():
    db = get_raw_db()
    now = datetime.now(TZ)
    today_str = now.date().isoformat()

    trips = db.execute(
        'SELECT * FROM trip WHERE trip_date = ? AND status = ? AND time_start IS NOT NULL',
        (today_str, 'active')
    ).fetchall()

    for trip in trips:
        already = db.execute(
            "SELECT 1 FROM reminder_log WHERE trip_id = ? AND remind_type = 'before'",
            (trip['id'],)
        ).fetchone()
        if already:
            continue

        trip_time = datetime.strptime(trip['time_start'], '%H:%M').replace(
            year=now.year, month=now.month, day=now.day, tzinfo=TZ
        )
        remind_at = trip_time - timedelta(minutes=trip['remind_before_min'])

        if remind_at <= now <= trip_time:
            items = db.execute(
                'SELECT * FROM checklist_item WHERE trip_id = ? ORDER BY sort_order', (trip['id'],)
            ).fetchall()
            msg = format_trip_message(trip, items, None, 'before')
            result = wechat_bot.send_to_group(msg)
            db.execute(
                'INSERT INTO reminder_log (trip_id, remind_type, status, response) VALUES (?, ?, ?, ?)',
                (trip['id'], 'before', result.get('status', 'unknown'),
                 json.dumps(result, ensure_ascii=False))
            )
            db.commit()
    db.close()


scheduler.add_job(morning_reminder_job, 'cron',
                  hour=config.MORNING_REMINDER_HOUR,
                  minute=config.MORNING_REMINDER_MINUTE,
                  id='morning_reminder')
scheduler.add_job(evening_reminder_job, 'cron',
                  hour=config.EVENING_REMINDER_HOUR,
                  minute=config.EVENING_REMINDER_MINUTE,
                  id='evening_reminder')
scheduler.add_job(before_trip_reminder_job, 'interval',
                  minutes=config.BEFORE_TRIP_CHECK_INTERVAL_MINUTES,
                  id='before_trip_reminder')


# ── 管理后台路由 ────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    if request.method == 'POST':
        password = request.form.get('password', '')
        if password == config.ADMIN_PASSWORD:
            session['admin_logged_in'] = True
            session.permanent = True
            app.permanent_session_lifetime = timedelta(days=30)
            return redirect(url_for('admin'))
        return render_template('login.html', error='密码错误，请重试')
    return render_template('login.html', error=None)


@app.route('/admin/logout')
def admin_logout():
    session.pop('admin_logged_in', None)
    return redirect(url_for('index'))


@app.route('/admin')
@admin_required
def admin():
    trips = query_db('SELECT * FROM trip WHERE status != ? ORDER BY trip_date DESC', ('cancelled',))
    return render_template('admin.html', trips=trips, editing=None)


@app.route('/admin/trip/new')
@admin_required
def admin_new():
    trips = query_db('SELECT * FROM trip WHERE status != ? ORDER BY trip_date DESC', ('cancelled',))
    return render_template('admin.html', trips=trips, editing='new')


@app.route('/admin/trip/<int:trip_id>/edit')
@admin_required
def admin_edit(trip_id):
    trips = query_db('SELECT * FROM trip WHERE status != ? ORDER BY trip_date DESC', ('cancelled',))
    trip = query_db('SELECT * FROM trip WHERE id = ?', (trip_id,), one=True)
    if not trip:
        flash('行程不存在')
        return redirect(url_for('admin'))
    checklist = query_db('SELECT * FROM checklist_item WHERE trip_id = ? ORDER BY sort_order', (trip_id,))
    events = query_db('SELECT * FROM weekly_event WHERE trip_id = ? ORDER BY event_date, sort_order', (trip_id,))
    return render_template('admin.html', trips=trips, editing='edit', trip=trip, checklist=checklist, events=events)


@app.route('/admin/trip/save', methods=['POST'])
@admin_required
def admin_save():
    db = get_db()
    trip_id = request.form.get('trip_id')
    data = {
        'title': request.form['title'],
        'trip_date': request.form['trip_date'],
        'time_start': request.form.get('time_start') or None,
        'time_end': request.form.get('time_end') or None,
        'location_from': request.form.get('location_from') or None,
        'location_to': request.form.get('location_to') or None,
        'personnel': request.form.get('personnel') or None,
        'weather': request.form.get('weather') or None,
        'notes': request.form.get('notes') or None,
        'remind_evening': 1 if request.form.get('remind_evening') else 0,
        'remind_before_min': int(request.form.get('remind_before_min', 60)),
        'mode': request.form.get('mode', 'structured'),
        'custom_content': request.form.get('custom_content') or None,
    }

    if trip_id:
        db.execute('''UPDATE trip SET title=?, trip_date=?, time_start=?, time_end=?,
                      location_from=?, location_to=?, personnel=?, weather=?, notes=?,
                      remind_evening=?, remind_before_min=?, mode=?, custom_content=?,
                      updated_at=datetime('now','localtime')
                      WHERE id=?''',
                   (*data.values(), trip_id))
    else:
        cur = db.execute('''INSERT INTO trip (title, trip_date, time_start, time_end,
                           location_from, location_to, personnel, weather, notes,
                           remind_evening, remind_before_min, mode, custom_content)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                        tuple(data.values()))
        trip_id = cur.lastrowid

    db.execute('DELETE FROM checklist_item WHERE trip_id = ?', (trip_id,))
    cl_contents = request.form.getlist('cl_content[]')
    for i, content in enumerate(cl_contents):
        if content.strip():
            db.execute('INSERT INTO checklist_item (trip_id, content, sort_order) VALUES (?, ?, ?)',
                       (trip_id, content.strip(), i))

    db.execute('DELETE FROM weekly_event WHERE trip_id = ?', (trip_id,))
    ev_dates = request.form.getlist('ev_date[]')
    ev_times = request.form.getlist('ev_time[]')
    ev_titles = request.form.getlist('ev_title[]')
    ev_notes = request.form.getlist('ev_note[]')
    for i in range(len(ev_titles)):
        if ev_titles[i].strip():
            db.execute('''INSERT INTO weekly_event (trip_id, event_date, event_time, event_title, event_note, sort_order)
                         VALUES (?, ?, ?, ?, ?, ?)''',
                       (trip_id, ev_dates[i] if i < len(ev_dates) else '', ev_times[i] if i < len(ev_times) else '',
                        ev_titles[i].strip(), ev_notes[i].strip() if i < len(ev_notes) and ev_notes[i] else '', i))

    db.commit()
    flash('行程已保存')
    return redirect(url_for('admin_edit', trip_id=trip_id))


@app.route('/admin/trip/<int:trip_id>/delete', methods=['POST'])
@admin_required
def admin_delete(trip_id):
    db = get_db()
    db.execute("UPDATE trip SET status = 'cancelled' WHERE id = ?", (trip_id,))
    db.commit()
    flash('行程已取消')
    return redirect(url_for('admin'))


# ── 每日任务管理路由 ───────────────────────────────────

@app.route('/admin/daily')
@admin_required
def admin_daily():
    tasks = query_db('SELECT * FROM daily_task ORDER BY created_at DESC')
    task_data = []
    for task in tasks:
        items = query_db('SELECT * FROM daily_task_item WHERE task_id = ? ORDER BY sort_order', (task['id'],))
        task_data.append({'task': task, 'items': items})
    return render_template('admin_daily.html', task_data=task_data, editing=None)


@app.route('/admin/daily/new')
@admin_required
def admin_daily_new():
    tasks = query_db('SELECT * FROM daily_task ORDER BY created_at DESC')
    task_data = []
    for task in tasks:
        items = query_db('SELECT * FROM daily_task_item WHERE task_id = ? ORDER BY sort_order', (task['id'],))
        task_data.append({'task': task, 'items': items})
    return render_template('admin_daily.html', task_data=task_data, editing='new')


@app.route('/admin/daily/<int:task_id>/edit')
@admin_required
def admin_daily_edit(task_id):
    tasks = query_db('SELECT * FROM daily_task ORDER BY created_at DESC')
    task_data = []
    for task in tasks:
        items = query_db('SELECT * FROM daily_task_item WHERE task_id = ? ORDER BY sort_order', (task['id'],))
        task_data.append({'task': task, 'items': items})
    task = query_db('SELECT * FROM daily_task WHERE id = ?', (task_id,), one=True)
    if not task:
        flash('任务不存在')
        return redirect(url_for('admin_daily'))
    items = query_db('SELECT * FROM daily_task_item WHERE task_id = ? ORDER BY sort_order', (task_id,))
    return render_template('admin_daily.html', task_data=task_data, editing='edit', task=task, items=items)


@app.route('/admin/daily/save', methods=['POST'])
@admin_required
def admin_daily_save():
    db = get_db()
    task_id = request.form.get('task_id')
    title = request.form['title']
    remind_time = request.form.get('remind_time', '09:00')
    active = 1 if request.form.get('active') else 0

    if task_id:
        db.execute('UPDATE daily_task SET title=?, remind_time=?, active=? WHERE id=?',
                   (title, remind_time, active, task_id))
    else:
        cur = db.execute('INSERT INTO daily_task (title, remind_time, active) VALUES (?, ?, ?)',
                         (title, remind_time, active))
        task_id = cur.lastrowid

    db.execute('DELETE FROM daily_task_item WHERE task_id = ?', (task_id,))
    contents = request.form.getlist('item_content[]')
    for i, content in enumerate(contents):
        if content.strip():
            db.execute('INSERT INTO daily_task_item (task_id, content, sort_order) VALUES (?, ?, ?)',
                       (task_id, content.strip(), i))

    db.commit()
    flash('每日任务已保存')
    return redirect(url_for('admin_daily_edit', task_id=task_id))


@app.route('/admin/daily/<int:task_id>/delete', methods=['POST'])
@admin_required
def admin_daily_delete(task_id):
    db = get_db()
    db.execute('DELETE FROM daily_checkin WHERE item_id IN (SELECT id FROM daily_task_item WHERE task_id = ?)', (task_id,))
    db.execute('DELETE FROM daily_task_item WHERE task_id = ?', (task_id,))
    db.execute('DELETE FROM daily_task WHERE id = ?', (task_id,))
    db.commit()
    flash('每日任务已删除')
    return redirect(url_for('admin_daily'))


# ── 学员端路由 ──────────────────────────────────────────

@app.route('/student')
def student_view():
    today = datetime.now(TZ).date().isoformat()
    trips = query_db(
        "SELECT * FROM trip WHERE trip_date >= ? AND status = 'active' ORDER BY trip_date, time_start",
        (today,)
    )
    trip_data = []
    for trip in trips:
        checklist = query_db('SELECT * FROM checklist_item WHERE trip_id = ? ORDER BY sort_order', (trip['id'],))
        events = query_db('SELECT * FROM weekly_event WHERE trip_id = ? ORDER BY event_date, sort_order', (trip['id'],))
        trip_data.append({
            'trip': trip,
            'checklist': checklist,
            'events': events,
        })

    daily_tasks = query_db('SELECT * FROM daily_task WHERE active = 1')
    daily_data = []
    for task in daily_tasks:
        items = query_db('SELECT * FROM daily_task_item WHERE task_id = ? ORDER BY sort_order', (task['id'],))
        checkins = query_db(
            'SELECT dc.* FROM daily_checkin dc JOIN daily_task_item dti ON dc.item_id = dti.id WHERE dti.task_id = ? AND dc.checkin_date = ?',
            (task['id'], today)
        )
        checkin_set = set()
        for c in checkins:
            checkin_set.add(c['item_id'])
        daily_data.append({'task': task, 'items': items, 'checkin_set': checkin_set})

    return render_template('student.html', trip_data=trip_data, daily_data=daily_data, today=today)


# ── API 路由 ────────────────────────────────────────────

@app.route('/api/checklist/<int:item_id>/toggle', methods=['POST'])
def toggle_checklist(item_id):
    db = get_db()
    item = query_db('SELECT * FROM checklist_item WHERE id = ?', (item_id,), one=True)
    if not item:
        return jsonify({'error': 'not found'}), 404

    new_val = 0 if item['checked'] else 1
    checked_at = datetime.now(TZ).strftime('%Y-%m-%d %H:%M:%S') if new_val else None
    checked_by = request.json.get('name', '学员') if request.is_json else '学员'

    db.execute('UPDATE checklist_item SET checked = ?, checked_at = ?, checked_by = ? WHERE id = ?',
               (new_val, checked_at, checked_by, item_id))
    db.commit()

    checklist = query_db('SELECT * FROM checklist_item WHERE trip_id = ?', (item['trip_id'],))
    total = len(checklist)
    done = sum(1 for c in checklist if c['checked'])

    return jsonify({'checked': bool(new_val), 'checked_at': checked_at, 'total': total, 'done': done})


@app.route('/api/trip/<int:trip_id>/preview')
def trip_preview(trip_id):
    trip = query_db('SELECT * FROM trip WHERE id = ?', (trip_id,), one=True)
    if not trip:
        return jsonify({'error': 'not found'}), 404
    items = query_db('SELECT * FROM checklist_item WHERE trip_id = ? ORDER BY sort_order', (trip_id,))
    events = query_db('SELECT * FROM weekly_event WHERE trip_id = ? ORDER BY event_date, sort_order', (trip_id,))
    msg = format_trip_message(trip, items, events, 'evening')
    return jsonify({'message': msg})


@app.route('/api/trip/<int:trip_id>/send', methods=['POST'])
def trip_send(trip_id):
    trip = query_db('SELECT * FROM trip WHERE id = ?', (trip_id,), one=True)
    if not trip:
        return jsonify({'error': 'not found'}), 404
    items = query_db('SELECT * FROM checklist_item WHERE trip_id = ? ORDER BY sort_order', (trip_id,))
    events = query_db('SELECT * FROM weekly_event WHERE trip_id = ? ORDER BY event_date, sort_order', (trip_id,))
    msg = format_trip_message(trip, items, events, 'evening')
    result = send_to_wechat(msg, trip_id)
    return jsonify(result)


# ── 微信机器人管理 API ──────────────────────────────────

@app.route('/api/bot/status')
def bot_status():
    return jsonify(wechat_bot.get_status())


@app.route('/api/bot/start', methods=['POST'])
def bot_start():
    result = wechat_bot.start_bot()
    return jsonify(result)


@app.route('/api/bot/stop', methods=['POST'])
def bot_stop():
    result = wechat_bot.stop_bot()
    return jsonify(result)


@app.route('/api/bot/groups')
def bot_groups():
    groups = wechat_bot.get_groups()
    return jsonify(groups)


@app.route('/api/bot/set-group', methods=['POST'])
def bot_set_group():
    data = request.get_json()
    group_name = data.get('group_name', '')
    if not group_name:
        return jsonify({'error': '请提供群名'}), 400
    wechat_bot.set_target_group(group_name)
    return jsonify({'status': 'ok', 'group': group_name})


@app.route('/api/bot/test-send', methods=['POST'])
def bot_test_send():
    result = wechat_bot.send_to_group("🤖 机器人测试消息\n连接成功！可以正常推送。")
    return jsonify(result)


# ── 每日任务 API ──────────────────────────────────────

@app.route('/api/daily/<int:task_id>/send', methods=['POST'])
def daily_send(task_id):
    task = query_db('SELECT * FROM daily_task WHERE id = ?', (task_id,), one=True)
    if not task:
        return jsonify({'error': 'not found'}), 404
    items = query_db('SELECT * FROM daily_task_item WHERE task_id = ? ORDER BY sort_order', (task_id,))
    today = datetime.now(TZ).date().isoformat()
    checkins = query_db(
        'SELECT dc.* FROM daily_checkin dc JOIN daily_task_item dti ON dc.item_id = dti.id WHERE dti.task_id = ? AND dc.checkin_date = ?',
        (task_id, today)
    )
    msg = format_daily_task_message(task, items, checkins)
    result = send_to_wechat(msg)
    return jsonify(result)


@app.route('/api/daily/checkin/<int:item_id>/toggle', methods=['POST'])
def toggle_daily_checkin(item_id):
    db = get_db()
    item = query_db('SELECT * FROM daily_task_item WHERE id = ?', (item_id,), one=True)
    if not item:
        return jsonify({'error': 'not found'}), 404

    today = datetime.now(TZ).date().isoformat()
    name = request.json.get('name', '学员') if request.is_json else '学员'

    existing = query_db(
        'SELECT * FROM daily_checkin WHERE item_id = ? AND checkin_date = ? AND checked_by = ?',
        (item_id, today, name), one=True
    )

    if existing:
        db.execute('DELETE FROM daily_checkin WHERE id = ?', (existing['id'],))
        checked = False
    else:
        db.execute('INSERT INTO daily_checkin (item_id, checkin_date, checked_by) VALUES (?, ?, ?)',
                   (item_id, today, name))
        checked = True
    db.commit()

    total = query_db('SELECT COUNT(*) as cnt FROM daily_task_item WHERE task_id = ?',
                     (item['task_id'],), one=True)['cnt']
    done = query_db(
        'SELECT COUNT(DISTINCT dc.item_id) as cnt FROM daily_checkin dc JOIN daily_task_item dti ON dc.item_id = dti.id WHERE dti.task_id = ? AND dc.checkin_date = ? AND dc.checked_by = ?',
        (item['task_id'], today, name), one=True
    )['cnt']

    return jsonify({'checked': checked, 'total': total, 'done': done})


# ── 微信机器人反向代理（HF Spaces 用） ────────────────

@app.route('/bot/<path:path>', methods=['GET', 'POST', 'PUT', 'DELETE'])
def bot_proxy(path=''):
    bot_url = f"{wechat_bot.BOT_BASE_URL}/{path}"
    try:
        resp = http_requests.request(
            method=request.method,
            url=bot_url,
            params=request.args,
            data=request.get_data(),
            headers={k: v for k, v in request.headers if k.lower() not in ('host', 'content-length', 'transfer-encoding')},
            allow_redirects=False,
            timeout=30
        )
        excluded = {'content-encoding', 'content-length', 'transfer-encoding', 'connection'}
        headers = [(k, v) for k, v in resp.headers.items() if k.lower() not in excluded]
        return Response(resp.content, resp.status_code, headers)
    except Exception as e:
        return jsonify({'error': str(e)}), 502


@app.route('/sse')
def sse_proxy():
    """代理微信机器人的 SSE 事件流（扫码登录实时更新）"""
    bot_url = f"{wechat_bot.BOT_BASE_URL}/sse"
    try:
        resp = http_requests.get(bot_url, stream=True, timeout=120)

        def generate():
            for line in resp.iter_lines(decode_unicode=True):
                if line is not None:
                    yield line + '\n'

        return Response(generate(), content_type='text/event-stream',
                        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


# ── 微信消息回调（Docker 容器 → Flask） ─────────────────

@app.route('/api/wechat/callback', methods=['POST'])
def wechat_callback():
    msg_type = request.form.get('type', '')
    content = request.form.get('content', '').strip()
    source_str = request.form.get('source', '{}')
    is_from_self = request.form.get('isMsgFromSelf', '0')

    if is_from_self == '1':
        return jsonify({'success': False})

    if msg_type != 'text':
        return jsonify({'success': False})

    try:
        source = json.loads(source_str)
    except Exception:
        return jsonify({'success': False})

    room = source.get('room', '')
    if not room:
        return jsonify({'success': False})

    sender_info = source.get('from', {})
    sender_payload = sender_info.get('payload', {})
    sender = sender_payload.get('name', '学员')

    m_daily = re.match(r'^每日打卡\s*(\d+)$', content)
    m = re.match(r'^打卡\s*(\d+)$', content)

    if m_daily:
        reply = _handle_daily_checkin(int(m_daily.group(1)), sender)
        if reply:
            return jsonify({'success': True, 'data': {'type': 'text', 'content': reply}})
    elif content == '每日进度':
        reply = _build_daily_progress()
        if reply:
            return jsonify({'success': True, 'data': {'type': 'text', 'content': reply}})
    elif m:
        reply = _handle_checkin(int(m.group(1)), sender)
        if reply:
            return jsonify({'success': True, 'data': {'type': 'text', 'content': reply}})
    elif content == '查看行程':
        reply = _build_upcoming_trips()
        if reply:
            return jsonify({'success': True, 'data': {'type': 'text', 'content': reply}})
    elif content == '打卡进度':
        reply = _build_checklist_progress()
        if reply:
            return jsonify({'success': True, 'data': {'type': 'text', 'content': reply}})

    return jsonify({'success': False})


def _handle_checkin(item_num, sender):
    db = get_raw_db()
    try:
        today = datetime.now(TZ).date().isoformat()
        trip = db.execute(
            "SELECT * FROM trip WHERE trip_date >= ? AND status = 'active' ORDER BY trip_date, time_start LIMIT 1",
            (today,)
        ).fetchone()
        if not trip:
            return f"@{sender} 当前没有进行中的行程"

        items = db.execute(
            'SELECT * FROM checklist_item WHERE trip_id = ? ORDER BY sort_order', (trip['id'],)
        ).fetchall()
        if item_num < 1 or item_num > len(items):
            return f"@{sender} 序号无效，有效范围 1-{len(items)}"

        item = items[item_num - 1]
        if item['checked']:
            return f"@{sender} ☑ 第{item_num}项「{item['content']}」已完成过了"

        now_str = datetime.now(TZ).strftime('%Y-%m-%d %H:%M:%S')
        db.execute(
            'UPDATE checklist_item SET checked = 1, checked_at = ?, checked_by = ? WHERE id = ?',
            (now_str, sender, item['id'])
        )
        db.commit()

        items_after = db.execute(
            'SELECT * FROM checklist_item WHERE trip_id = ? ORDER BY sort_order', (trip['id'],)
        ).fetchall()
        total = len(items_after)
        done = sum(1 for i in items_after if i['checked'])

        reply = f"✅ @{sender} 打卡成功！\n☑ 第{item_num}项「{item['content']}」已完成\n📊 进度 {done}/{total}"
        if done == total:
            reply += "\n🎉 所有准备项已完成！"
        return reply
    finally:
        db.close()


def _build_upcoming_trips():
    db = get_raw_db()
    today = datetime.now(TZ).date().isoformat()
    trips = db.execute(
        "SELECT * FROM trip WHERE trip_date >= ? AND status = 'active' ORDER BY trip_date, time_start LIMIT 3",
        (today,)
    ).fetchall()
    if not trips:
        db.close()
        return "📋 暂无即将到来的行程"

    parts = []
    for trip in trips:
        items = db.execute(
            'SELECT * FROM checklist_item WHERE trip_id = ? ORDER BY sort_order', (trip['id'],)
        ).fetchall()
        events = db.execute(
            'SELECT * FROM weekly_event WHERE trip_id = ? ORDER BY event_date, sort_order', (trip['id'],)
        ).fetchall()
        parts.append(format_trip_message(trip, items, events, 'detail'))
    db.close()
    return '\n\n'.join(parts)


def _build_checklist_progress():
    db = get_raw_db()
    today = datetime.now(TZ).date().isoformat()
    trip = db.execute(
        "SELECT * FROM trip WHERE trip_date >= ? AND status = 'active' ORDER BY trip_date LIMIT 1",
        (today,)
    ).fetchone()
    if not trip:
        db.close()
        return "📋 暂无进行中的行程"

    items = db.execute(
        'SELECT * FROM checklist_item WHERE trip_id = ? ORDER BY sort_order', (trip['id'],)
    ).fetchall()
    db.close()

    lines = [f"📊 打卡进度 — {trip['title']}", "━━━━━━━━━━"]
    for i, item in enumerate(items):
        check = "☑" if item['checked'] else "☐"
        who = f"（{item['checked_by']}）" if item['checked'] and item['checked_by'] else ""
        lines.append(f"  {check} {i+1}. {item['content']}{who}")
    total = len(items)
    done = sum(1 for i in items if i['checked'])
    lines.append(f"\n✅ {done}/{total} 已完成")
    return '\n'.join(lines)


def _handle_daily_checkin(item_num, sender):
    db = get_raw_db()
    try:
        today = datetime.now(TZ).date().isoformat()
        tasks = db.execute('SELECT * FROM daily_task WHERE active = 1 ORDER BY id LIMIT 1').fetchone()
        if not tasks:
            return f"@{sender} 当前没有每日任务"

        items = db.execute(
            'SELECT * FROM daily_task_item WHERE task_id = ? ORDER BY sort_order', (tasks['id'],)
        ).fetchall()
        if item_num < 1 or item_num > len(items):
            return f"@{sender} 序号无效，有效范围 1-{len(items)}"

        item = items[item_num - 1]
        existing = db.execute(
            'SELECT 1 FROM daily_checkin WHERE item_id = ? AND checkin_date = ? AND checked_by = ?',
            (item['id'], today, sender)
        ).fetchone()
        if existing:
            return f"@{sender} 今天已经打卡过「{item['content']}」了"

        db.execute('INSERT INTO daily_checkin (item_id, checkin_date, checked_by) VALUES (?, ?, ?)',
                   (item['id'], today, sender))
        db.commit()

        done = db.execute(
            'SELECT COUNT(DISTINCT dc.item_id) FROM daily_checkin dc JOIN daily_task_item dti ON dc.item_id = dti.id WHERE dti.task_id = ? AND dc.checkin_date = ? AND dc.checked_by = ?',
            (tasks['id'], today, sender)
        ).fetchone()[0]
        total = len(items)

        reply = f"✅ @{sender} 每日打卡成功！\n☑ 第{item_num}项「{item['content']}」\n📊 今日进度 {done}/{total}"
        if done == total:
            reply += "\n🎉 今日任务全部完成！"
        return reply
    finally:
        db.close()


def _build_daily_progress():
    db = get_raw_db()
    try:
        today = datetime.now(TZ).date().isoformat()
        tasks = db.execute('SELECT * FROM daily_task WHERE active = 1').fetchall()
        if not tasks:
            return "📋 暂无每日任务"

        parts = []
        for task in tasks:
            items = db.execute(
                'SELECT * FROM daily_task_item WHERE task_id = ? ORDER BY sort_order', (task['id'],)
            ).fetchall()
            lines = [f"📊 {task['title']} — 今日进度", "━━━━━━━━━━"]
            for i, item in enumerate(items):
                checkins = db.execute(
                    'SELECT checked_by FROM daily_checkin WHERE item_id = ? AND checkin_date = ?',
                    (item['id'], today)
                ).fetchall()
                names = [c['checked_by'] for c in checkins]
                if names:
                    lines.append(f"  ☑ {i+1}. {item['content']}（{'、'.join(names)}）")
                else:
                    lines.append(f"  ☐ {i+1}. {item['content']}")
            parts.append('\n'.join(lines))

        return '\n\n'.join(parts)
    finally:
        db.close()


def daily_task_reminder_job():
    db = get_raw_db()
    now = datetime.now(TZ)
    current_time = now.strftime('%H:%M')
    today = now.date().isoformat()

    tasks = db.execute(
        'SELECT * FROM daily_task WHERE active = 1 AND remind_time = ?', (current_time,)
    ).fetchall()

    for task in tasks:
        already = db.execute(
            "SELECT 1 FROM reminder_log WHERE trip_id = ? AND remind_type IN ('daily', 'daily_morning') AND date(sent_at) = ?",
            (task['id'], today)
        ).fetchone()
        if already:
            continue

        items = db.execute(
            'SELECT * FROM daily_task_item WHERE task_id = ? ORDER BY sort_order', (task['id'],)
        ).fetchall()
        if not items:
            continue

        msg = format_daily_task_message(task, items)
        result = wechat_bot.send_to_group(msg)
        db.execute(
            'INSERT INTO reminder_log (trip_id, remind_type, status, response) VALUES (?, ?, ?, ?)',
            (task['id'], 'daily', result.get('status', 'unknown'), json.dumps(result, ensure_ascii=False))
        )
        db.commit()
    db.close()


# ── 保活（HF Spaces 防休眠） ────────────────────────────

def keep_alive_job():
    if not config.KEEP_ALIVE_URL:
        return
    try:
        http_requests.get(config.KEEP_ALIVE_URL, timeout=10)
    except Exception:
        pass


# ── 启动 ────────────────────────────────────────────────

init_db()
scheduler.add_job(daily_task_reminder_job, 'cron', minute='*', id='daily_task_reminder')

if config.KEEP_ALIVE_URL:
    scheduler.add_job(keep_alive_job, 'interval', minutes=4, id='keep_alive')

if not scheduler.running:
    scheduler.start()
    atexit.register(lambda: scheduler.shutdown(wait=False))

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5001, use_reloader=False)
