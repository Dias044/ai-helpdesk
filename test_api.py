import os, sys, json, urllib.parse, threading, tempfile, unittest, urllib.request, base64
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["HELPDESK_DB"] = os.path.join(tempfile.mkdtemp(), "t.db")
import server
from http.server import ThreadingHTTPServer
PORT = 8765

def call(method, path, body=None, token=None):
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}", method=method, data=json.dumps(body).encode() if body is not None else None)
    if token: req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req) as r: return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e: return e.code, json.loads(e.read())

class T(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        server.Handler.app = server.App(); cls.srv = ThreadingHTTPServer(("127.0.0.1", PORT), server.Handler)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.st = call("POST", "/api/auth/login", dict(email="student@turan.edu.kz", password="student123"))[1]["token"]
        cls.op = call("POST", "/api/auth/login", dict(email="operator@turan.edu.kz", password="operator123"))[1]["token"]
        cls.ad = call("POST", "/api/auth/login", dict(email="admin@turan.edu.kz", password="admin123"))[1]["token"]
    @classmethod
    def tearDownClass(cls): cls.srv.shutdown()

    def test01_login_wrong_password(self): self.assertEqual(call("POST", "/api/auth/login", dict(email="student@turan.edu.kz", password="x"))[0], 401)
    def test02_register_and_duplicate(self):
        b = dict(name="Тест Тестов", email="new@turan.edu.kz", password="secret1")
        self.assertEqual(call("POST", "/api/auth/register", b)[0], 201); self.assertEqual(call("POST", "/api/auth/register", b)[0], 409)
    def test03_register_validation(self): self.assertEqual(call("POST", "/api/auth/register", dict(name="A", email="bad", password="1"))[0], 400)
    def test04_no_token(self): self.assertEqual(call("GET", "/api/tickets")[0], 401)
    def test05_create_auto_answer(self):
        c, d = call("POST", "/api/tickets", dict(subject="Пересдача", text="Как записаться на пересдачу экзамена по физике?"), self.st)
        self.assertEqual(c, 201); self.assertEqual(d["ai"]["category"], "Экзамены"); self.assertTrue(d["ai"]["auto"]); self.assertTrue(d["ai"]["article"])
        self.__class__.tid = d["id"]
    def test06_confirm_resolves(self):
        self.assertEqual(call("POST", f"/api/tickets/{self.tid}/confirm", {}, self.st)[0], 200)
        self.assertEqual(call("GET", f"/api/tickets/{self.tid}", None, self.st)[1]["status"], "Решено")
    def test07_out_of_domain_goes_to_operator(self):
        c, d = call("POST", "/api/tickets", dict(subject="Погода", text="Расскажи анекдот про погоду в Астане"), self.st)
        self.assertFalse(d["ai"]["auto"]); self.assertEqual(d["in_queue"], 1)
    def test08_validation_empty(self): self.assertEqual(call("POST", "/api/tickets", dict(subject="", text=""), self.st)[0], 400)
    def test09_operator_cannot_create(self): self.assertEqual(call("POST", "/api/tickets", dict(subject="a", text="b"), self.op)[0], 403)
    def test10_student_cannot_see_stats(self): self.assertEqual(call("GET", "/api/admin/stats", None, self.st)[0], 403)
    def test11_escalate_and_operator_flow(self):
        _, d = call("POST", "/api/tickets", dict(subject="Оплата", text="Деньги списались дважды при оплате обучения"), self.st)
        call("POST", f"/api/tickets/{d['id']}/escalate", {}, self.st)
        ids = [t["id"] for t in call("GET", "/api/tickets", None, self.op)[1]]; self.assertIn(d["id"], ids)
        self.assertEqual(call("POST", f"/api/tickets/{d['id']}/messages", dict(text="Проверяю платёж"), self.op)[0], 201)
        self.assertEqual(call("GET", f"/api/tickets/{d['id']}", None, self.op)[1]["status"], "В работе")
        self.assertEqual(call("PATCH", f"/api/tickets/{d['id']}/status", dict(status="Принято"), self.op)[0], 409)
        self.assertEqual(call("PATCH", f"/api/tickets/{d['id']}/status", dict(status="Решено"), self.op)[0], 200)
    def test12_student_cannot_read_foreign_ticket(self):
        t = call("POST", "/api/auth/login", dict(email="dina@turan.edu.kz", password="student123"))[1]["token"]
        self.assertEqual(call("GET", f"/api/tickets/{self.tid}", None, t)[0], 403)
    def test13_filters(self):
        r = call("GET", "/api/tickets?category=" + urllib.parse.quote("Оплата"), None, self.op)[1]; self.assertTrue(all(x["category"] == "Оплата" for x in r))
    def test14_admin_stats(self):
        c, d = call("GET", "/api/admin/stats?days=30", None, self.ad); self.assertEqual(c, 200); self.assertGreater(d["total"], 0); self.assertIn("auto_rate", d)
    def test15_block_user(self):
        users = call("GET", "/api/admin/users", None, self.ad)[1]; uid = [u["id"] for u in users if u["email"] == "arman@turan.edu.kz"][0]
        self.assertEqual(call("PATCH", f"/api/admin/users/{uid}", dict(status="blocked"), self.ad)[0], 200)
        self.assertEqual(call("POST", "/api/auth/login", dict(email="arman@turan.edu.kz", password="student123"))[0], 403)
    def test16_add_category(self):
        self.assertEqual(call("POST", "/api/admin/categories", dict(name="Стипендия"), self.ad)[0], 200)
        self.assertEqual(call("POST", "/api/admin/categories", dict(name="Стипендия"), self.ad)[0], 409)
    def test17_attachment(self):
        c, d = call("POST", "/api/tickets", dict(subject="Файл", text="Не загружается файл с работой", attachment=dict(name="a.txt", data=base64.b64encode(b"hi").decode())), self.st)
        self.assertEqual(c, 201); self.assertEqual(d["attachments"], ["a.txt"])
    def test18_bad_token(self): self.assertEqual(call("GET", "/api/tickets", None, "abc.def")[0], 401)
    def test19_ai_failure_fallback(self):
        orig = server.Handler.app.engine.analyze
        server.Handler.app.engine.analyze = lambda s, t: (_ for _ in ()).throw(RuntimeError("down"))
        try:
            c, d = call("POST", "/api/tickets", dict(subject="x", text="проверка отказа ИИ"), self.st)
            self.assertEqual(c, 201); self.assertEqual(d["in_queue"], 1); self.assertFalse(d["ai"]["auto"])
        finally: server.Handler.app.engine.analyze = orig

if __name__ == "__main__": unittest.main(verbosity=2)
