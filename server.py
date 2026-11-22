"""REST-сервер AI HelpDesk на стандартной библиотеке Python. Запуск: python server.py [порт]"""
import json, re, hmac, hashlib, base64, time, os, sys, threading, mimetypes
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
import db, ai

SECRET = os.environ.get("HELPDESK_SECRET", "dev-secret-change-me").encode()
STATIC = os.path.join(os.path.dirname(__file__), "static")
LOCK = threading.Lock()
STATUSES = ["Принято", "В работе", "Решено"]
ROLES, USER_STATUSES = ("student", "operator", "admin"), ("active", "blocked")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MAX_SUBJECT, MAX_TEXT, MAX_BODY = 200, 5000, 5 * 1024 * 1024
LOGIN_FAILS, LOGIN_LIMIT, LOGIN_WINDOW = {}, 5, 300      # защита от подбора пароля: 5 ошибок за 5 минут
SUS_POSITIVE = (0, 2, 4, 6, 8)                           # нечётные вопросы анкеты SUS (нумерация с нуля)

def make_token(uid, role):
    p = base64.urlsafe_b64encode(json.dumps({"uid": uid, "role": role, "exp": time.time() + 8 * 3600}).encode()).decode()
    return p + "." + hmac.new(SECRET, p.encode(), hashlib.sha256).hexdigest()

def read_token(tok):
    try:
        p, s = tok.split(".")
        if not hmac.compare_digest(s, hmac.new(SECRET, p.encode(), hashlib.sha256).hexdigest()): return None
        d = json.loads(base64.urlsafe_b64decode(p)); return d if d["exp"] > time.time() else None
    except Exception: return None

class Err(Exception):
    def __init__(self, code, msg): self.code, self.msg = code, msg

def need(user, *roles):
    if not user: raise Err(401, "Требуется авторизация")
    if user["role"] not in roles: raise Err(403, "Недостаточно прав")

def int_param(q, key, default, lo, hi):
    try: v = int(q.get(key, default))
    except (TypeError, ValueError): raise Err(400, f"Параметр {key} должен быть целым числом")
    if not lo <= v <= hi: raise Err(400, f"Параметр {key} вне допустимого диапазона {lo}–{hi}")
    return v

def ticket_row(c, t, with_ai=False):
    keys = t.keys()
    if "cat_name" in keys:          # строка уже содержит имена (один запрос с JOIN вместо трёх на каждую строку)
        cat, stu, op = (t["cat_name"],), (t["stu_name"],), (t["op_name"],) if t["op_name"] else None
    else:
        cat = c.execute("SELECT name FROM categories WHERE category_id=?", (t["category_id"],)).fetchone()
        stu = c.execute("SELECT name FROM users WHERE user_id=?", (t["user_id"],)).fetchone()
        op = c.execute("SELECT name FROM users WHERE user_id=?", (t["operator_id"],)).fetchone() if t["operator_id"] else None
    d = dict(id=t["ticket_id"], code=f"HD-{2000 + t['ticket_id']}", subject=t["subject"], body=t["body"], category=cat[0] if cat else None,
             priority=t["priority"], status=t["status"], confidence=t["confidence"], in_queue=t["in_queue"], auto_resolved=t["auto_resolved"],
             student=stu[0], operator=op[0] if op else None, created_at=t["created_at"], taken_at=t["taken_at"], resolved_at=t["resolved_at"])
    if with_ai:
        r = c.execute("SELECT * FROM ai_results WHERE ticket_id=? ORDER BY result_id DESC LIMIT 1", (t["ticket_id"],)).fetchone()
        a = c.execute("SELECT title,source,updated_at FROM kb_articles WHERE article_id=?", (r["article_id"],)).fetchone() if r and r["article_id"] else None
        d["ai"] = dict(category=r["pred_category"], priority=r["pred_priority"], confidence=r["confidence"], auto=bool(r["auto"]), answer=r["answer"],
                       article=dict(a) if a else None, reason=r["reason"] if "reason" in r.keys() else None) if r else None
        fb = c.execute("SELECT helpful FROM feedback WHERE ticket_id=? ORDER BY feedback_id DESC LIMIT 1", (t["ticket_id"],)).fetchone()
        d["feedback"] = None if not fb else bool(fb[0])
        d["attachments"] = [x[0] for x in c.execute("SELECT filename FROM attachments WHERE ticket_id=?", (t["ticket_id"],))]
    return d

class App:
    def __init__(self):
        self.c = db.conn(); db.init(self.c); self.engine = ai.Engine(); db.seed_demo(self.c, self.engine)

    # ---- обработчики ----
    def register(self, u, b, q):
        name, email, pw = (b.get("name") or "").strip(), (b.get("email") or "").strip().lower(), b.get("password") or ""
        if not name or len(name) > 100 or not EMAIL_RE.match(email) or not 6 <= len(pw) <= 128: raise Err(400, "Проверьте имя, e-mail и пароль (не менее 6 символов)")
        if self.c.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone(): raise Err(409, "Пользователь с таким e-mail уже существует")
        cur = self.c.execute("INSERT INTO users(name,email,pw_hash,role,created_at) VALUES(?,?,?,?,?)", (name, email, db.hash_pw(pw), "student", db.now()))
        db.log(self.c, cur.lastrowid, "register"); self.c.commit()
        return 201, dict(token=make_token(cur.lastrowid, "student"), name=name, role="student")

    def login(self, u, b, q):
        email = str(b.get("email") or "").strip().lower(); t = time.time()
        fails = [x for x in LOGIN_FAILS.get(email, []) if t - x < LOGIN_WINDOW]; LOGIN_FAILS[email] = fails
        if len(fails) >= LOGIN_LIMIT: raise Err(429, "Слишком много попыток входа. Повторите через 5 минут")
        r = self.c.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        if not r or not db.check_pw(str(b.get("password") or ""), r["pw_hash"]):
            fails.append(t); raise Err(401, "Неверный e-mail или пароль")
        LOGIN_FAILS.pop(email, None)
        if r["status"] != "active": raise Err(403, "Учётная запись заблокирована")
        db.log(self.c, r["user_id"], "login"); self.c.commit()
        return 200, dict(token=make_token(r["user_id"], r["role"]), name=r["name"], role=r["role"])

    def list_tickets(self, u, b, q):
        need(u, "student", "operator"); sql, args = "SELECT * FROM tickets t WHERE 1=1", []
        if u["role"] == "student": sql += " AND t.user_id=?"; args.append(u["uid"])
        else: sql += " AND (t.in_queue=1 OR t.status!='Принято')"
        if q.get("status"): sql += " AND t.status=?"; args.append(q["status"])
        if q.get("priority"): sql += " AND t.priority=?"; args.append(q["priority"])
        if q.get("category"): sql += " AND t.category_id=(SELECT category_id FROM categories WHERE name=?)"; args.append(q["category"])
        if q.get("q"):
            sql += " AND (t.subject LIKE ? OR t.body LIKE ? OR ('HD-'||(2000+t.ticket_id)) LIKE ?)"; args += [f"%{q['q']}%"] * 3
        limit = int_param(q, "limit", 200, 1, 1000); offset = int_param(q, "offset", 0, 0, 10**9)
        rows = self.c.execute(sql.replace("SELECT * FROM tickets t", "SELECT t.*, c.name AS cat_name, s.name AS stu_name, o.name AS op_name FROM tickets t "
                              "LEFT JOIN categories c ON c.category_id=t.category_id LEFT JOIN users s ON s.user_id=t.user_id "
                              "LEFT JOIN users o ON o.user_id=t.operator_id") + " ORDER BY t.ticket_id DESC LIMIT ? OFFSET ?", args + [limit, offset]).fetchall()
        return 200, [ticket_row(self.c, t) for t in rows]

    def create(self, u, b, q):
        need(u, "student"); s, t = str(b.get("subject") or "").strip(), str(b.get("text") or "").strip()
        if not s or not t: raise Err(400, "Заполните тему и текст обращения")
        if len(s) > MAX_SUBJECT or len(t) > MAX_TEXT: raise Err(400, f"Тема — до {MAX_SUBJECT} символов, текст — до {MAX_TEXT} символов")
        att = b.get("attachment"); file = None
        if att:
            if not isinstance(att, dict) or not att.get("name") or not att.get("data"): raise Err(400, "Некорректное вложение")
            try: raw = base64.b64decode(str(att["data"]), validate=True)
            except Exception: raise Err(400, "Вложение повреждено (ожидается Base64)")
            if len(raw) > db.MAX_FILE: raise Err(400, "Размер вложения превышает 3 МБ")
            file = dict(name=str(att["name"]), bytes=raw)
        pre = b.get("_ai")
        if pre and pre["category"] and pre["category"] not in db.active_categories(self.c):   # категорию могли отключить
            pre = dict(pre, auto=False, article=None, answer=None, reason="категория отключена")
        tid = db.create_ticket(self.c, self.engine, u["uid"], s, t, attachment=file, res=pre)
        return 201, ticket_row(self.c, self.get_ticket(tid), True)

    def get_ticket(self, tid):
        t = self.c.execute("SELECT * FROM tickets WHERE ticket_id=?", (tid,)).fetchone()
        if not t: raise Err(404, "Обращение не найдено")
        return t

    def get(self, u, b, q, tid):
        need(u, "student", "operator"); t = self.get_ticket(int(tid))
        if u["role"] == "student" and t["user_id"] != u["uid"]: raise Err(403, "Недостаточно прав")
        return 200, ticket_row(self.c, t, True)

    def confirm(self, u, b, q, tid):
        need(u, "student"); t = self.get_ticket(int(tid))
        if t["user_id"] != u["uid"]: raise Err(403, "Недостаточно прав")
        if t["status"] == "Решено" or t["in_queue"]: raise Err(409, "Обращение уже обработано или передано оператору")
        r = self.c.execute("SELECT auto FROM ai_results WHERE ticket_id=?", (t["ticket_id"],)).fetchone()
        if not r or not r["auto"]: raise Err(409, "Для обращения нет автоматического ответа")
        self.c.execute("UPDATE tickets SET status='Решено',auto_resolved=1,resolved_at=? WHERE ticket_id=?", (db.now(), t["ticket_id"]))
        db.log(self.c, u["uid"], "confirm", f"id={tid}"); self.c.commit(); return 200, dict(ok=True)

    def escalate(self, u, b, q, tid):
        need(u, "student"); t = self.get_ticket(int(tid))
        if t["user_id"] != u["uid"]: raise Err(403, "Недостаточно прав")
        if t["status"] == "Решено": raise Err(409, "Обращение уже решено")
        if t["in_queue"]: raise Err(409, "Обращение уже передано оператору")
        self.c.execute("UPDATE tickets SET in_queue=1 WHERE ticket_id=?", (t["ticket_id"],))
        db.log(self.c, u["uid"], "escalate", f"id={tid}"); self.c.commit(); return 200, dict(ok=True)

    def set_status(self, u, b, q, tid):
        need(u, "operator"); t = self.get_ticket(int(tid)); st = b.get("status")
        if st not in STATUSES: raise Err(400, "Недопустимый статус")
        if STATUSES.index(st) < STATUSES.index(t["status"]): raise Err(409, "Нельзя вернуть обращение в предыдущий статус")
        taken = t["taken_at"] or (db.now() if st != "Принято" else None)
        self.c.execute("UPDATE tickets SET status=?,operator_id=?,taken_at=?,resolved_at=? WHERE ticket_id=?",
                       (st, u["uid"], taken, db.now() if st == "Решено" else None, t["ticket_id"]))
        if b.get("category"):                       # корректировка категории оператором — основа показателя «Точность ИИ»
            cid = self.c.execute("SELECT category_id FROM categories WHERE name=? AND is_active=1", (b["category"],)).fetchone()
            if not cid: raise Err(400, "Неизвестная категория")
            self.c.execute("UPDATE tickets SET category_id=? WHERE ticket_id=?", (cid[0], t["ticket_id"]))
        db.log(self.c, u["uid"], "status", f"id={tid} -> {st}"); self.c.commit(); return 200, dict(ok=True)

    def messages(self, u, b, q, tid):
        need(u, "student", "operator"); t = self.get_ticket(int(tid))
        if u["role"] == "student" and t["user_id"] != u["uid"]: raise Err(403, "Недостаточно прав")
        return 200, [dict(sender=r["name"], role=r["role"], mine=r["sender_id"] == u["uid"], text=r["text"], at=r["created_at"]) for r in self.c.execute(
            "SELECT m.*,u.name,u.role FROM messages m JOIN users u ON u.user_id=m.sender_id WHERE ticket_id=? ORDER BY message_id", (t["ticket_id"],))]

    def send_message(self, u, b, q, tid):
        need(u, "student", "operator"); t = self.get_ticket(int(tid))
        if u["role"] == "student" and t["user_id"] != u["uid"]: raise Err(403, "Недостаточно прав")
        txt = str(b.get("text") or "").strip()
        if not txt: raise Err(400, "Пустое сообщение")
        if len(txt) > MAX_TEXT: raise Err(400, f"Сообщение длиннее {MAX_TEXT} символов")
        if t["status"] == "Решено": raise Err(409, "Обращение решено — создайте новое обращение")
        self.c.execute("INSERT INTO messages(ticket_id,sender_id,text,created_at) VALUES(?,?,?,?)", (t["ticket_id"], u["uid"], txt, db.now()))
        if u["role"] == "operator" and t["status"] == "Принято":
            self.c.execute("UPDATE tickets SET status='В работе',operator_id=?,taken_at=? WHERE ticket_id=?", (u["uid"], db.now(), t["ticket_id"]))
        self.c.commit(); return 201, dict(ok=True)

    def stats(self, u, b, q):
        need(u, "admin"); days = int_param(q, "days", 30, 1, 3650); since = f"datetime('now','localtime','-{days} days')"; c = self.c
        n = c.execute(f"SELECT COUNT(*) FROM tickets WHERE created_at>={since}").fetchone()[0]
        auto = c.execute(f"SELECT COUNT(*) FROM tickets WHERE created_at>={since} AND auto_resolved=1").fetchone()[0]
        avg = c.execute(f"SELECT AVG((julianday(taken_at)-julianday(created_at))*1440) FROM tickets WHERE created_at>={since} AND taken_at IS NOT NULL").fetchone()[0]
        ok = c.execute(f"SELECT AVG(CASE WHEN a.pred_category=c.name THEN 1.0 ELSE 0 END) FROM tickets t JOIN ai_results a ON a.ticket_id=t.ticket_id "
                       f"JOIN categories c ON c.category_id=t.category_id WHERE t.created_at>={since}").fetchone()[0]
        by = lambda col: {r[0] or "—": r[1] for r in c.execute(f"SELECT {col},COUNT(*) FROM tickets t LEFT JOIN categories c ON c.category_id=t.category_id WHERE created_at>={since} GROUP BY {col}")}
        daily = {r[0]: r[1] for r in c.execute(f"SELECT substr(created_at,1,10),COUNT(*) FROM tickets WHERE created_at>={since} GROUP BY 1 ORDER BY 1")}
        return 200, dict(total=n, auto_rate=round(100 * auto / n, 1) if n else 0, avg_response_min=round(avg) if avg is not None else None,
                         ai_accuracy=round(100 * ok, 1) if ok is not None else None, by_priority=by("priority"), by_category=by("c.name"), daily=daily,
                         recent=[ticket_row(c, t) for t in c.execute("SELECT * FROM tickets ORDER BY ticket_id DESC LIMIT 5")], **self._fb_summary())

    def categories(self, u, b, q):
        need(u, "admin", "operator", "student")
        if b:
            need(u, "admin"); name = (b.get("name") or "").strip()
            if not name: raise Err(400, "Введите название категории")
            try: self.c.execute("INSERT INTO categories(name) VALUES(?)", (name,)); self.c.commit()
            except Exception: raise Err(409, "Такая категория уже есть")
        return 200, [dict(id=r[0], name=r[1], active=bool(r[2]), count=r[3]) for r in self.c.execute(
            "SELECT c.category_id,c.name,c.is_active,(SELECT COUNT(*) FROM tickets t WHERE t.category_id=c.category_id) FROM categories c")]

    def patch_category(self, u, b, q, cid):
        need(u, "admin")
        if not self.c.execute("SELECT 1 FROM categories WHERE category_id=?", (int(cid),)).fetchone(): raise Err(404, "Категория не найдена")
        name = b.get("name")
        if name is not None:
            name = str(name).strip()
            if not name or len(name) > 60: raise Err(400, "Название категории не может быть пустым")
            if self.c.execute("SELECT 1 FROM categories WHERE name=? AND category_id!=?", (name, int(cid))).fetchone(): raise Err(409, "Такая категория уже есть")
        if "active" in b and b["active"] not in (0, 1, True, False): raise Err(400, "Поле active должно быть 0 или 1")
        self.c.execute("UPDATE categories SET is_active=COALESCE(?,is_active),name=COALESCE(?,name) WHERE category_id=?",
                       (int(b["active"]) if "active" in b else None, name, int(cid)))
        db.log(self.c, u["uid"], "category_patch", f"id={cid} {b}"); self.c.commit(); return 200, dict(ok=True)

    def users(self, u, b, q):
        need(u, "admin")
        if b:
            name, email, pw = str(b.get("name") or "").strip(), str(b.get("email") or "").strip().lower(), str(b.get("password") or "")
            if b.get("role") not in ROLES: raise Err(400, "Недопустимая роль")
            if not name or not EMAIL_RE.match(email) or len(pw) < 6: raise Err(400, "Укажите имя, корректный e-mail и пароль не короче 6 символов")
            if self.c.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone(): raise Err(409, "E-mail занят")
            self.c.execute("INSERT INTO users(name,email,pw_hash,role,created_at) VALUES(?,?,?,?,?)", (name, email, db.hash_pw(pw), b["role"], db.now()))
            db.log(self.c, u["uid"], "user_create", email); self.c.commit()
        return 200, [dict(id=r["user_id"], name=r["name"], email=r["email"], role=r["role"], status=r["status"]) for r in self.c.execute("SELECT * FROM users")]

    def patch_user(self, u, b, q, uid):
        need(u, "admin")
        if "role" in b and b["role"] not in ROLES: raise Err(400, "Недопустимая роль")
        if "status" in b and b["status"] not in USER_STATUSES: raise Err(400, "Недопустимый статус")
        if not self.c.execute("SELECT 1 FROM users WHERE user_id=?", (int(uid),)).fetchone(): raise Err(404, "Пользователь не найден")
        if int(uid) == u["uid"] and (b.get("status") == "blocked" or b.get("role") not in (None, "admin")): raise Err(409, "Нельзя изменить собственную роль или заблокировать себя")
        self.c.execute("UPDATE users SET role=COALESCE(?,role),status=COALESCE(?,status) WHERE user_id=?", (b.get("role"), b.get("status"), int(uid)))
        db.log(self.c, u["uid"], "user_patch", f"id={uid} {b}"); self.c.commit(); return 200, dict(ok=True)

    # ---- версия 1.1: обратная связь и подсказки оператору ----
    def feedback(self, u, b, q, tid):
        need(u, "student"); t = self.get_ticket(int(tid))
        if t["user_id"] != u["uid"]: raise Err(403, "Недостаточно прав")
        if not isinstance(b.get("helpful"), bool): raise Err(400, "Поле helpful должно быть true или false")
        self.c.execute("INSERT INTO feedback(ticket_id,user_id,helpful,comment,created_at) VALUES(?,?,?,?,?) "
                       "ON CONFLICT(ticket_id,user_id) DO UPDATE SET helpful=excluded.helpful, comment=excluded.comment, created_at=excluded.created_at",
                       (t["ticket_id"], u["uid"], int(b["helpful"]), str(b.get("comment") or "")[:1000], db.now()))
        db.log(self.c, u["uid"], "feedback", f"id={tid} helpful={b['helpful']}"); self.c.commit(); return 201, dict(ok=True)

    def survey(self, u, b, q):
        need(u, "student", "operator", "admin"); a = b.get("answers")
        if not isinstance(a, list) or len(a) != 10 or not all(isinstance(x, int) and 1 <= x <= 5 for x in a):
            raise Err(400, "Ответьте на все 10 вопросов (оценка от 1 до 5)")
        sus = 2.5 * sum((x - 1) if i in SUS_POSITIVE else (5 - x) for i, x in enumerate(a))   # шкала SUS 0–100
        self.c.execute("INSERT INTO survey(user_id,answers,sus,comment,created_at) VALUES(?,?,?,?,?)",
                       (u["uid"], json.dumps(a), sus, str(b.get("comment") or "")[:2000], db.now()))
        self.c.commit(); return 201, dict(sus=sus)

    def _fb_summary(self):
        c = self.c; h = c.execute("SELECT COUNT(*), SUM(helpful) FROM feedback").fetchone()
        s = c.execute("SELECT COUNT(*), AVG(sus) FROM survey").fetchone()
        return dict(feedback_n=h[0], helpful_rate=round(100 * (h[1] or 0) / h[0], 1) if h[0] else None,
                    survey_n=s[0], sus_mean=round(s[1], 1) if s[1] is not None else None)

    def feedback_summary(self, u, b, q):
        need(u, "admin"); d = self._fb_summary()
        d["comments"] = [dict(kind="Анкета", user=r[0], text=r[1], value=r[2], at=r[3]) for r in self.c.execute(
            "SELECT u.name, s.comment, s.sus, s.created_at FROM survey s JOIN users u ON u.user_id=s.user_id WHERE s.comment!='' ORDER BY s.survey_id DESC LIMIT 50")]
        d["comments"] += [dict(kind="Автоответ", user=r[0], text=r[1], value="полезен" if r[2] else "не помог", at=r[3]) for r in self.c.execute(
            "SELECT u.name, f.comment, f.helpful, f.created_at FROM feedback f JOIN users u ON u.user_id=f.user_id WHERE f.comment!='' ORDER BY f.feedback_id DESC LIMIT 50")]
        d["by_category"] = {r[0] or "—": dict(n=r[1], helpful=round(100 * r[2] / r[1], 1)) for r in self.c.execute(
            "SELECT a.pred_category, COUNT(*), SUM(f.helpful) FROM feedback f JOIN ai_results a ON a.ticket_id=f.ticket_id GROUP BY 1")}
        return 200, d

    def suggest(self, u, b, q, tid):
        need(u, "operator"); t = self.get_ticket(int(tid))
        return 200, self.engine.suggest(t["subject"] + ". " + t["body"])

ROUTES = [("POST", r"/api/auth/register", "register"), ("POST", r"/api/auth/login", "login"), ("GET", r"/api/tickets", "list_tickets"),
  ("POST", r"/api/tickets", "create"), ("GET", r"/api/tickets/(\d+)", "get"), ("POST", r"/api/tickets/(\d+)/confirm", "confirm"),
  ("POST", r"/api/tickets/(\d+)/escalate", "escalate"), ("PATCH", r"/api/tickets/(\d+)/status", "set_status"),
  ("GET", r"/api/tickets/(\d+)/messages", "messages"), ("POST", r"/api/tickets/(\d+)/messages", "send_message"),
  ("GET", r"/api/admin/stats", "stats"), ("GET", r"/api/admin/categories", "categories"), ("POST", r"/api/admin/categories", "categories"),
  ("PATCH", r"/api/admin/categories/(\d+)", "patch_category"), ("GET", r"/api/admin/users", "users"), ("POST", r"/api/admin/users", "users"),
  ("PATCH", r"/api/admin/users/(\d+)", "patch_user"), ("POST", r"/api/tickets/(\d+)/feedback", "feedback"),
  ("POST", r"/api/survey", "survey"), ("GET", r"/api/admin/feedback", "feedback_summary"), ("GET", r"/api/tickets/(\d+)/suggest", "suggest")]

class Handler(BaseHTTPRequestHandler):
    app = None
    def log_message(self, *a): pass
    def _send(self, code, data, ctype="application/json"):
        body = data if isinstance(data, bytes) else json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code); self.send_header("Content-Type", ctype + "; charset=utf-8"); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    def _handle(self, method):
        from urllib.parse import urlparse, parse_qsl
        path = urlparse(self.path); q = dict(parse_qsl(path.query))
        if not path.path.startswith("/api/"):
            f = os.path.join(STATIC, "index.html" if path.path in ("/", "") else path.path.lstrip("/"))
            if os.path.isfile(f) and os.path.abspath(f).startswith(os.path.abspath(STATIC)):
                return self._send(200, open(f, "rb").read(), mimetypes.guess_type(f)[0] or "text/plain")
            return self._send(404, {"error": "Не найдено"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY: return self._send(413, {"error": "Слишком большой запрос"})
            b = json.loads(self.rfile.read(n) or b"{}") if n else {}
        except Exception: return self._send(400, {"error": "Некорректный JSON"})
        if not isinstance(b, dict): return self._send(400, {"error": "Тело запроса должно быть JSON-объектом"})
        b.pop("_ai", None)
        if method == "POST" and path.path == "/api/tickets" and b.get("subject") and b.get("text"):
            try:     # ИИ-анализ выполняется вне блокировки БД, чтобы не задерживать другие запросы
                if len(str(b["subject"])) <= MAX_SUBJECT and len(str(b["text"])) <= MAX_TEXT:
                    with LOCK: active = db.active_categories(self.app.c)
                    b["_ai"] = self.app.engine.analyze(str(b["subject"]), str(b["text"]), active)
            except Exception as e:
                b["_ai"] = None    # create_ticket повторит анализ и при сбое передаст обращение оператору
        tok = (self.headers.get("Authorization") or "").replace("Bearer ", ""); u = read_token(tok)
        with LOCK:
            if u:
                r = self.app.c.execute("SELECT status FROM users WHERE user_id=?", (u["uid"],)).fetchone()
                if not r or r[0] != "active": u = None            # заблокированный пользователь теряет доступ сразу
            try:
                for m, pat, fn in ROUTES:
                    mt = re.fullmatch(pat, path.path)
                    if m == method and mt:
                        code, data = getattr(self.app, fn)(u, b, q, *mt.groups()); return self._send(code, data)
                return self._send(404, {"error": "Метод не найден"})
            except Err as e: return self._send(e.code, {"error": e.msg})
            except Exception as e:
                db.log(self.app.c, u["uid"] if u else None, "server_error", f"{method} {path.path}: {e!r}")
                try: self.app.c.commit()
                except Exception: pass
                return self._send(500, {"error": "Внутренняя ошибка сервера"})
    def do_GET(self): self._handle("GET")
    def do_POST(self): self._handle("POST")
    def do_PATCH(self): self._handle("PATCH")

class Server(ThreadingHTTPServer):
    request_queue_size = 128     # было 5 по умолчанию: при 50 одновременных клиентах соединения отклонялись
    daemon_threads = True

def serve(port=8000):
    Handler.app = App(); srv = Server(("0.0.0.0", port), Handler)
    print(f"AI HelpDesk: http://localhost:{port}"); srv.serve_forever()

if __name__ == "__main__": serve(int(sys.argv[1]) if len(sys.argv) > 1 else 8000)
