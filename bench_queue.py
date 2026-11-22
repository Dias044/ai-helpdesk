"""Время ответа GET /api/tickets (очередь оператора) при большом числе обращений. python bench_queue.py N"""
import os, sys, json, time, tempfile, threading, urllib.request
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["HELPDESK_DB"] = os.path.join(tempfile.mkdtemp(), "q.db")
import server, db
from http.server import ThreadingHTTPServer
N = int(sys.argv[1]) if len(sys.argv) > 1 else 3000
app = server.App(); c = app.c
c.executemany("INSERT INTO tickets(user_id,subject,body,category_id,priority,status,confidence,in_queue,created_at) VALUES(4,?,?,1,'Низкий','Принято',0.9,1,?)",
              [(f"Тема {i}", "Где расписание", db.now()) for i in range(N)]); c.commit()
server.Handler.app = app; srv = ThreadingHTTPServer(("127.0.0.1", 8795), server.Handler); threading.Thread(target=srv.serve_forever, daemon=True).start()
tok = json.loads(urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:8795/api/auth/login", method="POST",
      data=json.dumps(dict(email="operator@turan.edu.kz", password="operator123")).encode())).read())["token"]
ts = []
for _ in range(5):
    r = urllib.request.Request("http://127.0.0.1:8795/api/tickets", headers={"Authorization": "Bearer " + tok})
    t = time.perf_counter(); body = urllib.request.urlopen(r).read(); ts.append((time.perf_counter() - t) * 1000)
print(json.dumps(dict(tickets=N, returned=len(json.loads(body)), ms=round(sorted(ts)[2], 1), kb=round(len(body) / 1024))))
srv.shutdown()
