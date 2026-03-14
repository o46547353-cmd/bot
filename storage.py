### storage.py
import sqlite3, json, os
from datetime import datetime

DB_FILE = os.environ.get('DB_FILE', 'slash_vpn_bot.db')
conn = sqlite3.connect(DB_FILE, check_same_thread=False)
c = conn.cursor()

c.executescript('''
CREATE TABLE IF NOT EXISTS accounts (
    login TEXT PRIMARY KEY,
    session_id TEXT DEFAULT '',
    csrf_token TEXT DEFAULT '',
    user_id TEXT DEFAULT '',
    username TEXT DEFAULT '',
    account_prompt TEXT DEFAULT '',
    topic_prompt TEXT DEFAULT '',
    warmup_keywords TEXT DEFAULT 'vpn,безопасность,интернет,privacy',
    warmup_preset TEXT DEFAULT 'A',
    timezone TEXT DEFAULT 'Europe/Moscow',
    warmup_active INTEGER DEFAULT 0,
    autopost_active INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS posts_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_login TEXT,
    post_json TEXT,
    scheduled_at TEXT,
    added_at TEXT
);
CREATE TABLE IF NOT EXISTS images (
    account_login TEXT PRIMARY KEY,
    path TEXT
);
CREATE TABLE IF NOT EXISTS archive (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_login TEXT,
    post_json TEXT,
    post_ids TEXT,
    posted_at TEXT
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS warmup_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_login TEXT,
    stats TEXT,
    logged_at TEXT
);
CREATE TABLE IF NOT EXISTS monitor_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_login TEXT,
    post_id TEXT,
    comment_id TEXT,
    commenter_username TEXT,
    action TEXT,
    logged_at TEXT
);
CREATE TABLE IF NOT EXISTS post_stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_login TEXT,
    post_id TEXT,
    topic TEXT,
    likes INTEGER DEFAULT 0,
    replies INTEGER DEFAULT 0,
    reposts INTEGER DEFAULT 0,
    checked_at TEXT,
    hours_after INTEGER DEFAULT 0
);
''')
conn.commit()


# --- Аккаунты ---
def get_all_accounts():
    c.execute('SELECT login FROM accounts')
    return [{'login': r[0]} for r in c.fetchall()]

def get_account(login):
    c.execute('SELECT * FROM accounts WHERE login=?', (login,))
    row = c.fetchone()
    if not row: return None
    keys = ['login','session_id','csrf_token','user_id','username',
            'account_prompt','topic_prompt','warmup_keywords',
            'warmup_preset','timezone','warmup_active','autopost_active']
    return dict(zip(keys, row))

def save_account(account):
    acc = get_account(account['login'])
    if acc:
        c.execute('''UPDATE accounts SET session_id=?,csrf_token=?,user_id=?,username=?
                     WHERE login=?''',
                  (account.get('session_id',''), account.get('csrf_token',''),
                   account.get('user_id',''), account.get('username', account['login']),
                   account['login']))
    else:
        c.execute('''INSERT INTO accounts(login,session_id,csrf_token,user_id,username)
                     VALUES(?,?,?,?,?)''',
                  (account['login'], account.get('session_id',''),
                   account.get('csrf_token',''), account.get('user_id',''),
                   account.get('username', account['login'])))
    conn.commit()

def update_account_prompts(login, account_prompt, topic_prompt):
    c.execute('UPDATE accounts SET account_prompt=?,topic_prompt=? WHERE login=?',
              (account_prompt, topic_prompt, login))
    conn.commit()

def update_warmup_settings(login, keywords, preset, timezone):
    c.execute('UPDATE accounts SET warmup_keywords=?,warmup_preset=?,timezone=? WHERE login=?',
              (keywords, preset, timezone, login))
    conn.commit()

def set_warmup_active(login, active: bool):
    c.execute('UPDATE accounts SET warmup_active=? WHERE login=?', (int(active), login))
    conn.commit()

def set_autopost_active(login, active: bool):
    c.execute('UPDATE accounts SET autopost_active=? WHERE login=?', (int(active), login))
    conn.commit()


# --- Очередь ---
def add_series(series, account_login, scheduled_at=None):
    c.execute('INSERT INTO posts_queue(account_login,post_json,scheduled_at,added_at) VALUES(?,?,?,?)',
              (account_login, json.dumps(series, ensure_ascii=False),
               scheduled_at or '', datetime.now().isoformat()))
    conn.commit()

def pop(account_login=None):
    if account_login:
        c.execute('SELECT id,post_json,account_login FROM posts_queue WHERE account_login=? ORDER BY id ASC LIMIT 1', (account_login,))
    else:
        c.execute('SELECT id,post_json,account_login FROM posts_queue ORDER BY id ASC LIMIT 1')
    row = c.fetchone()
    if not row: return None
    c.execute('DELETE FROM posts_queue WHERE id=?', (row[0],))
    conn.commit()
    return {'id': row[0], 'posts': json.loads(row[1]), 'account_login': row[2]}

def count(account_login=None):
    if account_login:
        c.execute('SELECT COUNT(*) FROM posts_queue WHERE account_login=?', (account_login,))
    else:
        c.execute('SELECT COUNT(*) FROM posts_queue')
    return c.fetchone()[0]

def get_queue(account_login=None):
    if account_login:
        c.execute('SELECT id,account_login,post_json,added_at FROM posts_queue WHERE account_login=? ORDER BY id ASC', (account_login,))
    else:
        c.execute('SELECT id,account_login,post_json,added_at FROM posts_queue ORDER BY id ASC')
    result = []
    for row in c.fetchall():
        p = json.loads(row[2])
        result.append({'id': row[0], 'account_login': row[1],
                       'topic': p.get('topic','—'), 'added_at': row[3]})
    return result

def delete_queue_item(item_id):
    c.execute('DELETE FROM posts_queue WHERE id=?', (item_id,))
    conn.commit()


# --- Изображения ---
def set_image(account_login, path):
    c.execute('INSERT OR REPLACE INTO images VALUES(?,?)', (account_login, path))
    conn.commit()

def get_image(account_login):
    c.execute('SELECT path FROM images WHERE account_login=?', (account_login,))
    row = c.fetchone()
    return row[0] if row else None


# --- Настройки ---
def get_setting(key, default=None):
    c.execute('SELECT value FROM settings WHERE key=?', (key,))
    row = c.fetchone()
    return row[0] if row else default

def set_setting(key, value):
    c.execute('INSERT OR REPLACE INTO settings VALUES(?,?)', (key, str(value)))
    conn.commit()


# --- Архив ---
def archive_item(series, account_login, post_ids=None):
    c.execute('INSERT INTO archive(account_login,post_json,post_ids,posted_at) VALUES(?,?,?,?)',
              (account_login, json.dumps(series, ensure_ascii=False),
               json.dumps(post_ids or []), datetime.now().isoformat()))
    conn.commit()

def get_archive(limit=20):
    c.execute('SELECT id,account_login,post_json,post_ids,posted_at FROM archive ORDER BY id DESC LIMIT ?', (limit,))
    result = []
    for row in c.fetchall():
        p = json.loads(row[2])
        result.append({'id': row[0], 'account_login': row[1],
                       'topic': p.get('topic','—'),
                       'post_ids': json.loads(row[3]),
                       'posted_at': row[4]})
    return result


# --- Прогрев ---
def log_warmup(account_login, stats):
    c.execute('INSERT INTO warmup_log(account_login,stats,logged_at) VALUES(?,?,?)',
              (account_login, json.dumps(stats), datetime.now().isoformat()))
    conn.commit()


# --- Мониторинг ---
def is_comment_processed(comment_id):
    c.execute('SELECT id FROM monitor_log WHERE comment_id=?', (comment_id,))
    return c.fetchone() is not None

def log_monitor_action(account_login, post_id, comment_id, commenter, action):
    c.execute('INSERT INTO monitor_log(account_login,post_id,comment_id,commenter_username,action,logged_at) VALUES(?,?,?,?,?,?)',
              (account_login, post_id, comment_id, commenter, action, datetime.now().isoformat()))
    conn.commit()


# --- Статистика постов ---
def save_post_stat(account_login, post_id, topic, likes, replies, reposts, hours_after):
    c.execute('''INSERT INTO post_stats(account_login,post_id,topic,likes,replies,reposts,checked_at,hours_after)
                 VALUES(?,?,?,?,?,?,?,?)''',
              (account_login, post_id, topic, likes, replies, reposts,
               datetime.now().isoformat(), hours_after))
    conn.commit()

def get_post_stats(account_login, limit=10):
    c.execute('''SELECT post_id,topic,likes,replies,reposts,checked_at,hours_after
                 FROM post_stats WHERE account_login=? ORDER BY id DESC LIMIT ?''',
              (account_login, limit))
    return [{'post_id': r[0], 'topic': r[1], 'likes': r[2], 'replies': r[3],
             'reposts': r[4], 'checked_at': r[5], 'hours_after': r[6]}
            for r in c.fetchall()]
