"""Нагрузочное тестирование: N параллельных клиентов создают обращения и читают очередь.
Запуск: python loadtest.py [out.json]"""
import os, sys, json, time, tempfile, threading, urllib.request, urllib.error, statistics, concurrent.futures as cf
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["HELPDESK_DB"] = os.path.join(tempfile.mkdtemp(), "load.db")
import server
from http.server import ThreadingHTTPServer
PORT = 8790
TEXTS = ["Как записаться на пересдачу экзамена?", "Не могу войти в личный кабинет", "Где посмотреть расписание группы",
         "Как оплатить обучение картой", "Нужна справка с места учёбы", "Где найти лекции по алгоритмам"]

def req(method, path, body=None, tok=None):
    r = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}", method=method, data=json.dumps(body).encode() if body else None)
    if tok: r.add_header("Authorization", "Bearer " + tok)
    t = time.perf_counter()
    try:
        with urllib.request.urlopen(r, timeout=30) as resp: resp.read(); code = resp.status
    except urllib.error.HTTPError as e: code = e.code
    except Exception: code = 0
    return code, (time.perf_counter() - t) * 1000

def run(conc, per_client, st, op):
    def client(i):
        out = []
        for k in range(per_client):
            out.append(req("POST", "/api/tickets", dict(subject="Нагрузка", text=TEXTS[(i + k) % len(TEXTS)]), st))
            out.append(req("GET", "/api/tickets?priority=%D0%92%D1%8B%D1%81%D0%BE%D0%BA%D0%B8%D0%B9", None, op))
        return out
    t0 = time.perf_counter()
    with cf.ThreadPoolExecutor(conc) as ex: res = [x for r in ex.map(client, range(conc)) for x in r]
    dur = time.perf_counter() - t0; lat = [l for _, l in res]; err = sum(1 for c, _ in res if c >= 500 or c == 0)
    return dict(clients=conc, requests=len(res), rps=round(len(res) / dur, 1), p50_ms=round(statistics.median(lat), 1),
                p95_ms=round(sorted(lat)[int(0.95 * len(lat)) - 1], 1), max_ms=round(max(lat), 1), errors=err)

if __name__ == "__main__":
    server.Handler.app = server.App(); srv = getattr(server, "Server", ThreadingHTTPServer)(("127.0.0.1", PORT), server.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    st = json.loads(urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{PORT}/api/auth/login", method="POST",
         data=json.dumps(dict(email="student@turan.edu.kz", password="student123")).encode())).read())["token"]
    op = json.loads(urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{PORT}/api/auth/login", method="POST",
         data=json.dumps(dict(email="operator@turan.edu.kz", password="operator123")).encode())).read())["token"]
    out = [run(c, 10, st, op) for c in (1, 10, 25, 50)]
    for r in out: print(r)
    json.dump(out, open(sys.argv[1] if len(sys.argv) > 1 else "load.json", "w"), ensure_ascii=False, indent=1)
    srv.shutdown()
