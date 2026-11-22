"""Тестовые сценарии СРО4: негативные проверки, граничные значения, бизнес-правила, новые функции.
Набор запускался на версии 1.0 (фиксация ошибок) и на версии 1.1 (повторное тестирование).
Запуск: python tests/test_sro4.py"""
import os, sys, json, threading, tempfile, unittest, urllib.request, urllib.error, base64, concurrent.futures as cf
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["HELPDESK_DB"] = os.path.join(tempfile.mkdtemp(), "t4.db")
import server
from http.server import ThreadingHTTPServer
PORT = 8766

def raw(method, path, data=None, token=None):
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}", method=method, data=data)
    if token: req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req) as r: body = r.read(); code = r.status
    except urllib.error.HTTPError as e: body = e.read(); code = e.code
    try: return code, json.loads(body)
    except Exception: return code, body

def call(method, path, body=None, token=None):
    return raw(method, path, json.dumps(body).encode() if body is not None else None, token)

def login(email, pw): return call("POST", "/api/auth/login", dict(email=email, password=pw))[1]["token"]

class SRO4(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        server.Handler.app = server.App(); cls.srv = ThreadingHTTPServer(("127.0.0.1", PORT), server.Handler)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.st = login("student@turan.edu.kz", "student123"); cls.st2 = login("dina@turan.edu.kz", "student123")
        cls.op = login("operator@turan.edu.kz", "operator123"); cls.ad = login("admin@turan.edu.kz", "admin123")
    @classmethod
    def tearDownClass(cls): cls.srv.shutdown(); cls.srv.server_close()

    def new_ticket(self, text="Как записаться на пересдачу экзамена по физике?", tok=None):
        return call("POST", "/api/tickets", dict(subject="Тест", text=text), tok or self.st)

    # --- устойчивость API к некорректным данным ---
    def test_b01_json_array_body(self):
        """Тело запроса — JSON-массив вместо объекта: ожидается 400, а не 500"""
        self.assertEqual(raw("POST", "/api/tickets", b"[1,2]", self.st)[0], 400)
    def test_b02_stats_bad_period(self):
        """Параметр периода статистики не число: ожидается 400"""
        self.assertEqual(call("GET", "/api/admin/stats?days=abc", None, self.ad)[0], 400)
    def test_b03_admin_user_missing_fields(self):
        """Создание пользователя администратором без e-mail: ожидается 400"""
        self.assertEqual(call("POST", "/api/admin/users", dict(name="Без почты", role="operator"), self.ad)[0], 400)
    def test_b04_admin_user_bad_email(self):
        """Создание пользователя с некорректным e-mail: ожидается 400"""
        self.assertEqual(call("POST", "/api/admin/users", dict(name="X", email="not-an-email", role="operator", password="secret12"), self.ad)[0], 400)
    def test_b05_patch_user_bad_role(self):
        """Назначение несуществующей роли: ожидается 400"""
        self.assertEqual(call("PATCH", "/api/admin/users/5", dict(role="superuser"), self.ad)[0], 400)
    def test_b06_patch_user_bad_status(self):
        """Недопустимый статус пользователя: ожидается 400"""
        self.assertEqual(call("PATCH", "/api/admin/users/5", dict(status="foo"), self.ad)[0], 400)
    def test_b07_rename_category_duplicate(self):
        """Переименование категории в уже существующую: ожидается 409"""
        self.assertEqual(call("PATCH", "/api/admin/categories/1", dict(name="Экзамены"), self.ad)[0], 409)
    def test_b08_rename_category_empty(self):
        """Переименование категории в пустую строку: ожидается 400"""
        self.assertEqual(call("PATCH", "/api/admin/categories/1", dict(name="  "), self.ad)[0], 400)
    def test_b09_attachment_bad_base64(self):
        """Вложение с повреждёнными данными: ожидается 400"""
        b = dict(subject="Файл", text="Не грузится файл", attachment=dict(name="a.txt", data="@@@не base64@@@"))
        self.assertEqual(call("POST", "/api/tickets", b, self.st)[0], 400)
    def test_b10_attachment_too_large(self):
        """Вложение больше 3 МБ в обход клиента: ожидается 400"""
        big = base64.b64encode(b"0" * (3 * 1024 * 1024 + 10)).decode()
        b = dict(subject="Файл", text="Большой файл", attachment=dict(name="big.bin", data=big))
        self.assertEqual(call("POST", "/api/tickets", b, self.st)[0], 400)
    def test_b11_text_too_long(self):
        """Текст обращения длиннее 5000 символов: ожидается 400"""
        self.assertEqual(self.new_ticket("а" * 6000)[0], 400)
    def test_b12_bruteforce_limit(self):
        """После 5 неудачных попыток входа подряд: ожидается 429"""
        codes = [call("POST", "/api/auth/login", dict(email="arman@turan.edu.kz", password="wrong"))[0] for _ in range(6)]
        self.assertEqual(codes[-1], 429)
    def test_b13_message_to_resolved(self):
        """Студент пишет в чат решённого обращения через API: ожидается 409"""
        c, d = self.new_ticket(); call("POST", f"/api/tickets/{d['id']}/confirm", {}, self.st)
        self.assertEqual(call("POST", f"/api/tickets/{d['id']}/messages", dict(text="ещё вопрос"), self.st)[0], 409)
    def test_b14_inactive_category_no_auto(self):
        """Обращение отнесено к отключённой категории: автоответ не выдаётся, обращение уходит оператору"""
        cats = {x["name"]: x["id"] for x in call("GET", "/api/admin/categories", None, self.ad)[1]}
        call("PATCH", f"/api/admin/categories/{cats['Оплата']}", dict(active=0), self.ad)
        try:
            c, d = self.new_ticket("Как оплатить обучение за семестр картой?")
            self.assertEqual(c, 201); self.assertFalse(d["ai"]["auto"]); self.assertEqual(d["in_queue"], 1)
        finally:
            call("PATCH", f"/api/admin/categories/{cats['Оплата']}", dict(active=1), self.ad)
    def test_b15_escalate_twice(self):
        """Повторная передача оператору: ожидается 409"""
        c, d = self.new_ticket()
        call("POST", f"/api/tickets/{d['id']}/escalate", {}, self.st)
        self.assertEqual(call("POST", f"/api/tickets/{d['id']}/escalate", {}, self.st)[0], 409)

    # --- корректная работа (должны проходить в обеих версиях) ---
    def test_c01_invalid_json(self):
        """Некорректный JSON: 400"""
        self.assertEqual(raw("POST", "/api/tickets", b"{bad", self.st)[0], 400)
    def test_c02_path_traversal(self):
        """Попытка чтения файла вне каталога static: 404"""
        self.assertEqual(raw("GET", "/../server.py")[0], 404)
    def test_c03_unicode_emoji(self):
        """Обращение с эмодзи и казахскими буквами принимается"""
        self.assertEqual(self.new_ticket("Сабақ кестесі қайда? 😊 где расписание")[0], 201)
    def test_c04_html_is_stored_as_text(self):
        """HTML в теме сохраняется как текст (экранирование выполняет клиент)"""
        c, d = call("POST", "/api/tickets", dict(subject="<script>alert(1)</script>", text="где расписание"), self.st)
        self.assertEqual(c, 201); self.assertEqual(d["subject"], "<script>alert(1)</script>")
    def test_c05_parallel_creation(self):
        """20 одновременных обращений: все приняты, номера уникальны"""
        with cf.ThreadPoolExecutor(20) as ex:
            res = list(ex.map(lambda i: self.new_ticket(f"Где посмотреть расписание группы {i}"), range(20)))
        self.assertTrue(all(c == 201 for c, _ in res)); self.assertEqual(len({d["id"] for _, d in res}), 20)
    def test_c06_other_student_messages(self):
        """Студент не может писать в чужое обращение: 403"""
        c, d = self.new_ticket()
        self.assertEqual(call("POST", f"/api/tickets/{d['id']}/messages", dict(text="чужое"), self.st2)[0], 403)
    def test_c07_unknown_route(self):
        """Неизвестный метод API: 404"""
        self.assertEqual(call("GET", "/api/nothing", None, self.st)[0], 404)

    # --- новые функции версии 1.1 (по обратной связи) ---
    def test_n01_answer_feedback(self):
        """Студент оценивает полезность автоответа"""
        c, d = self.new_ticket()
        self.assertEqual(call("POST", f"/api/tickets/{d['id']}/feedback", dict(helpful=False, comment="не то"), self.st)[0], 201)
    def test_n02_system_survey(self):
        """Анкета пользователя (SUS) сохраняется, неполная отклоняется"""
        good = dict(answers=[4, 2, 4, 2, 4, 2, 4, 2, 4, 2], comment="удобно")
        self.assertEqual(call("POST", "/api/survey", good, self.st)[0], 201)
        self.assertEqual(call("POST", "/api/survey", dict(answers=[5, 5]), self.st)[0], 400)
    def test_n03_feedback_summary_admin_only(self):
        """Сводка обратной связи доступна только администратору"""
        self.assertEqual(call("GET", "/api/admin/feedback", None, self.st)[0], 403)
        c, d = call("GET", "/api/admin/feedback", None, self.ad)
        self.assertEqual(c, 200); self.assertIn("sus_mean", d)
    def test_n04_operator_answer_template(self):
        """Оператор получает подсказку-ответ из базы знаний для обращения из очереди"""
        c, d = self.new_ticket("Расскажи анекдот про погоду")
        c2, s = call("GET", f"/api/tickets/{d['id']}/suggest", None, self.op)
        self.assertEqual(c2, 200); self.assertEqual(len(s), 3)

if __name__ == "__main__":
    unittest.main(verbosity=2)
