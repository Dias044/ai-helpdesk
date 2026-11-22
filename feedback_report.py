"""Сводка пользовательского тестирования по данным из БД: анкеты SUS, оценки автоответов, комментарии.
Запуск после сессии тестирования: python feedback_report.py [путь к helpdesk.db]
Результат печатается и сохраняется в feedback_report.csv (для таблиц отчёта)."""
import sqlite3, sys, os, json, csv, statistics

path = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("HELPDESK_DB", os.path.join(os.path.dirname(os.path.abspath(__file__)), "helpdesk.db"))
c = sqlite3.connect(path); c.row_factory = sqlite3.Row

rows = c.execute("SELECT s.*, u.name, u.email FROM survey s JOIN users u ON u.user_id=s.user_id ORDER BY s.survey_id").fetchall()
print(f"Анкет SUS: {len(rows)}")
out = [["Участник", "E-mail", "Ответы Q1–Q10", "Балл SUS", "Комментарий", "Дата"]]
for r in rows:
    out.append([r["name"], r["email"], " ".join(map(str, json.loads(r["answers"]))), r["sus"], r["comment"], r["created_at"]])
    print(f"  {r['name']:<25} SUS={r['sus']:5.1f}  {r['comment'] or ''}")
if rows:
    s = [r["sus"] for r in rows]
    print(f"Средний SUS: {statistics.mean(s):.1f}, медиана {statistics.median(s):.1f}, мин {min(s)}, макс {max(s)}"
          + (f", ст. откл. {statistics.stdev(s):.1f}" if len(s) > 1 else ""))
    print("Интерпретация (Bangor et al.): <50 — неприемлемо, 50–70 — пограничное, 68 — средний уровень, >70 — хорошо, >85 — отлично")

fb = c.execute("SELECT f.*, u.name, a.pred_category, t.subject, t.body FROM feedback f JOIN users u ON u.user_id=f.user_id "
               "JOIN ai_results a ON a.ticket_id=f.ticket_id JOIN tickets t ON t.ticket_id=f.ticket_id").fetchall()
print(f"\nОценок автоответов: {len(fb)}, полезных: {sum(r['helpful'] for r in fb)}"
      + (f" ({100 * sum(r['helpful'] for r in fb) / len(fb):.0f}%)" if fb else ""))
out += [[], ["Оценки автоответов"], ["Участник", "Категория ИИ", "Обращение", "Полезен", "Комментарий", "Дата"]]
for r in fb:
    out.append([r["name"], r["pred_category"], r["subject"] + ". " + r["body"], "да" if r["helpful"] else "нет", r["comment"], r["created_at"]])
    if not r["helpful"]: print(f"  НЕ ПОМОГ: [{r['pred_category']}] {r['body'][:70]} — {r['comment'] or ''}")

corr = c.execute("SELECT COUNT(*) FROM tickets t JOIN ai_results a ON a.ticket_id=t.ticket_id JOIN categories k ON k.category_id=t.category_id "
                 "WHERE a.pred_category IS NOT NULL AND a.pred_category!=k.name").fetchone()[0]
print(f"\nОбращений, где оператор исправил категорию ИИ: {corr}")
with open("feedback_report.csv", "w", newline="", encoding="utf-8-sig") as f: csv.writer(f, delimiter=";").writerows(out)
print("Сохранено: feedback_report.csv")
