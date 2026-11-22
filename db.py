"""Работа с базой данных SQLite (схема из приложения А отчёта)."""
import sqlite3, hashlib, os, re, secrets, random, datetime as dt
from data import CATEGORIES, KB
DB_PATH = os.environ.get("HELPDESK_DB", os.path.join(os.path.dirname(__file__), "helpdesk.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(user_id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, email TEXT UNIQUE NOT NULL,
  pw_hash TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN('student','operator','admin')), status TEXT NOT NULL DEFAULT 'active', created_at TEXT);
CREATE TABLE IF NOT EXISTS categories(category_id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE NOT NULL, is_active INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS tickets(ticket_id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL REFERENCES users, subject TEXT NOT NULL,
  body TEXT NOT NULL, category_id INTEGER REFERENCES categories, priority TEXT, status TEXT NOT NULL DEFAULT 'Принято',
  confidence REAL, in_queue INTEGER DEFAULT 0, auto_resolved INTEGER DEFAULT 0, operator_id INTEGER REFERENCES users,
  created_at TEXT, taken_at TEXT, resolved_at TEXT);
CREATE TABLE IF NOT EXISTS ai_results(result_id INTEGER PRIMARY KEY AUTOINCREMENT, ticket_id INTEGER NOT NULL REFERENCES tickets,
  pred_category TEXT, pred_priority TEXT, confidence REAL, similarity REAL, article_id INTEGER, answer TEXT, auto INTEGER, model_version TEXT, reason TEXT);
CREATE TABLE IF NOT EXISTS kb_articles(article_id INTEGER PRIMARY KEY, category TEXT, title TEXT, body TEXT, source TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS messages(message_id INTEGER PRIMARY KEY AUTOINCREMENT, ticket_id INTEGER NOT NULL REFERENCES tickets,
  sender_id INTEGER NOT NULL REFERENCES users, text TEXT NOT NULL, created_at TEXT);
CREATE TABLE IF NOT EXISTS attachments(file_id INTEGER PRIMARY KEY AUTOINCREMENT, ticket_id INTEGER NOT NULL REFERENCES tickets, filename TEXT, path TEXT);
CREATE TABLE IF NOT EXISTS audit_log(log_id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, action TEXT, details TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS feedback(feedback_id INTEGER PRIMARY KEY AUTOINCREMENT, ticket_id INTEGER NOT NULL REFERENCES tickets,
  user_id INTEGER NOT NULL REFERENCES users, helpful INTEGER NOT NULL, comment TEXT, created_at TEXT, UNIQUE(ticket_id, user_id));
CREATE TABLE IF NOT EXISTS survey(survey_id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL REFERENCES users,
  answers TEXT NOT NULL, sus REAL NOT NULL, comment TEXT, created_at TEXT);
CREATE INDEX IF NOT EXISTS idx_tickets_queue ON tickets(in_queue, status);
CREATE INDEX IF NOT EXISTS idx_tickets_status ON tickets(status);
CREATE INDEX IF NOT EXISTS idx_tickets_cat ON tickets(category_id);
CREATE INDEX IF NOT EXISTS idx_tickets_date ON tickets(created_at);
"""

def now(): return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def hash_pw(pw, salt=None):
    salt = salt or secrets.token_hex(8)
    return salt + "$" + hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 100_000).hex()

def check_pw(pw, stored):
    salt = stored.split("$")[0]; return secrets.compare_digest(hash_pw(pw, salt), stored)

def conn():
    c = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=10); c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON"); return c

def init(c):
    c.executescript(SCHEMA)
    if "reason" not in [r[1] for r in c.execute("PRAGMA table_info(ai_results)")]:   # миграция БД версии 1.0
        c.execute("ALTER TABLE ai_results ADD COLUMN reason TEXT")
    if c.execute("SELECT COUNT(*) FROM categories").fetchone()[0] == 0:
        c.executemany("INSERT INTO categories(name) VALUES(?)", [(n,) for n in CATEGORIES])
        c.executemany("INSERT INTO kb_articles VALUES(?,?,?,?,?,?)", KB)
        for n, e, r in [("Администратор Системы", "admin@turan.edu.kz", "admin"), ("Михаил Орлов", "operator@turan.edu.kz", "operator"),
                        ("Анна Ким", "operator2@turan.edu.kz", "operator"), ("Студент Демо", "student@turan.edu.kz", "student"),
                        ("Дина Сейтова", "dina@turan.edu.kz", "student"), ("Арман Нурлан", "arman@turan.edu.kz", "student")]:
            c.execute("INSERT INTO users(name,email,pw_hash,role,created_at) VALUES(?,?,?,?,?)",
                      (n, e, hash_pw({"admin": "admin123", "operator": "operator123", "student": "student123"}[r]), r, now()))
        c.commit()

def seed_demo(c, engine, n=40):
    """Демонстрационные обращения за последние 30 дней — прогоняются через настоящий ИИ-модуль."""
    if c.execute("SELECT COUNT(*) FROM tickets").fetchone()[0] > 0: return
    from data import gen_dataset
    rnd = random.Random(11); X, _ = gen_dataset(12, seed=99); rnd.shuffle(X)
    studs = [r[0] for r in c.execute("SELECT user_id FROM users WHERE role='student'")]
    ops = [r[0] for r in c.execute("SELECT user_id FROM users WHERE role='operator'")]
    for text in X[:n]:
        created = dt.datetime.now() - dt.timedelta(days=rnd.randint(0, 29), hours=rnd.randint(0, 8))
        create_ticket(c, engine, rnd.choice(studs), (text[:48].rsplit(" ", 1)[0] + "…") if len(text) > 48 else text, text, when=created.strftime("%Y-%m-%d %H:%M:%S"))
        tid = c.execute("SELECT MAX(ticket_id) FROM tickets").fetchone()[0]
        t = c.execute("SELECT * FROM tickets WHERE ticket_id=?", (tid,)).fetchone(); r = rnd.random(); fmt = lambda d: d.strftime("%Y-%m-%d %H:%M:%S")
        if t["in_queue"] == 0:
            if r < 0.6:      # студент подтвердил автоответ
                c.execute("UPDATE tickets SET status='Решено',auto_resolved=1,resolved_at=? WHERE ticket_id=?", (fmt(created + dt.timedelta(minutes=5)), tid)); continue
            if r < 0.9:      # студент отправил оператору
                c.execute("UPDATE tickets SET in_queue=1 WHERE ticket_id=?", (tid,))
            else: continue   # ответ ИИ ещё не подтверждён
        r2 = rnd.random()
        if r2 < 0.55:
            tk = created + dt.timedelta(minutes=rnd.randint(8, 70)); rs = tk + dt.timedelta(hours=rnd.randint(1, 20))
            c.execute("UPDATE tickets SET status='Решено',operator_id=?,taken_at=?,resolved_at=? WHERE ticket_id=?", (rnd.choice(ops), fmt(tk), fmt(rs), tid))
        elif r2 < 0.8:
            c.execute("UPDATE tickets SET status='В работе',operator_id=?,taken_at=? WHERE ticket_id=?", (rnd.choice(ops), fmt(created + dt.timedelta(minutes=rnd.randint(8, 70))), tid))
    c.commit()

def log(c, uid, action, details=""):
    c.execute("INSERT INTO audit_log(user_id,action,details,created_at) VALUES(?,?,?,?)", (uid, action, details, now()))

MAX_FILE = 3 * 1024 * 1024

def active_categories(c):
    return {r[0] for r in c.execute("SELECT name FROM categories WHERE is_active=1")}

def create_ticket(c, engine, user_id, subject, body, when=None, attachment=None, res=None):
    """res — результат ИИ-анализа, если он уже выполнен вне блокировки БД (версия 1.1)."""
    when = when or now()
    try:
        if res is None: res = engine.analyze(subject, body, active_categories(c))
    except Exception as e:                       # недоступность ИИ: обращение уходит оператору без автоответа
        res = dict(category=None, confidence=None, priority=None, similarity=None, auto=False, article=None, answer=None, reason="сбой ИИ")
        log(c, user_id, "ai_error", str(e))
    cat = c.execute("SELECT category_id FROM categories WHERE name=?", (res["category"],)).fetchone()
    cur = c.execute("INSERT INTO tickets(user_id,subject,body,category_id,priority,status,confidence,in_queue,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (user_id, subject, body, cat[0] if cat else None, res["priority"], "Принято", res["confidence"], 0 if res["auto"] else 1, when))
    tid = cur.lastrowid
    c.execute("INSERT INTO ai_results(ticket_id,pred_category,pred_priority,confidence,similarity,article_id,answer,auto,model_version,reason) VALUES(?,?,?,?,?,?,?,?,?,?)",
              (tid, res["category"], res["priority"], res["confidence"], res["similarity"], res["article"]["id"] if res["article"] else None,
               res["answer"], int(res["auto"]), getattr(engine, "version", "tfidf-lr-1.1"), res.get("reason")))
    if attachment:
        os.makedirs(os.path.join(os.path.dirname(DB_PATH), "uploads"), exist_ok=True)
        fn = re.sub(r"[^\w.\- ]", "_", os.path.basename(attachment["name"].replace("\\", "/")))[:100] or "file"
        path = os.path.join(os.path.dirname(DB_PATH), "uploads", f"{tid}_{fn}")
        with open(path, "wb") as fh: fh.write(attachment["bytes"])
        c.execute("INSERT INTO attachments(ticket_id,filename,path) VALUES(?,?,?)", (tid, fn, path))
    log(c, user_id, "ticket_created", f"id={tid}"); c.commit()
    return tid
