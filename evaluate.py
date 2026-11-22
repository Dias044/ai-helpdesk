"""Оценка ИИ-модуля (СРО4): классификация, приоритет, автоответ, отказ вне тематики,
устойчивость к опечаткам, время анализа. Запуск: python evaluate.py [out.json]"""
import json, random, sys, time
import numpy as np
from sklearn.metrics import accuracy_score, precision_recall_fscore_support
import ai
from data import MANUAL_TEST, RETR_TEST, PRIO_TEST
from eval_data import EXT_TEST, OOD_TEST, PRIO_TEST2, HOLDOUT, HOLDOUT_OOD, HOLDOUT_PRIO

eng = ai.Engine()
OUT_OF_SCOPE = getattr(ai, "OUT_OF_SCOPE", None)

def cls_metrics(rows):
    y = [c for _, c in rows]; p = [eng.classify(t)[0] for t, _ in rows]
    pr, rc, f1, _ = precision_recall_fscore_support(y, p, average="macro", zero_division=0)
    errs = [(t, c, q) for (t, c), q in zip(rows, p) if c != q]
    return dict(n=len(rows), acc=round(accuracy_score(y, p), 3), prec=round(pr, 3), rec=round(rc, 3), f1=round(f1, 3)), errs

def auto_metrics(rows):
    """Охват автоответа и доля автоответов из документа правильной категории."""
    n_auto = n_ok = 0
    for t, c in rows:
        r = eng.analyze("", t)
        if r["auto"]:
            n_auto += 1; n_ok += (r["category"] == c)
    return dict(n=len(rows), auto=n_auto, auto_rate=round(n_auto / len(rows), 3), correct=n_ok,
                precision=round(n_ok / n_auto, 3) if n_auto else None)

def ood_metrics(texts):
    wrong = [t for t in texts if eng.analyze("", t)["auto"]]
    return dict(n=len(texts), rejected=len(texts) - len(wrong), reject_rate=round(1 - len(wrong) / len(texts), 3), answered=wrong)

def prio_metrics(rows):
    ok = 0; errs = []
    for t, c, e in rows:
        p, s = ai.priority(t, c)
        if p == e: ok += 1
        else: errs.append((t, e, p, s))
    return dict(n=len(rows), acc=round(ok / len(rows), 3)), errs

LAYOUT = dict(zip("йцукенгшщзхфывапролджэячсмитьбю", "цукенгшщзхъывапролджэячсмитьбю."))
def typo(text, rate, rnd):
    """Опечатки: пропуск, перестановка, удвоение или замена соседней по раскладке буквы."""
    out = list(text); i = 0
    while i < len(out):
        ch = out[i]
        if ch.isalpha() and rnd.random() < rate:
            op = rnd.randrange(4)
            if op == 0: del out[i]; continue
            if op == 1 and i + 1 < len(out): out[i], out[i + 1] = out[i + 1], out[i]; i += 1
            elif op == 2: out.insert(i, ch); i += 1
            else: out[i] = LAYOUT.get(ch.lower(), ch)
        i += 1
    return "".join(out)

def robustness(rows, rates=(0.0, 0.05, 0.10, 0.15), seeds=5):
    res = {}
    for r in rates:
        accs = []
        for s in range(seeds):
            rnd = random.Random(100 + s)
            accs.append(np.mean([eng.classify(typo(t, r, rnd))[0] == c for t, c in rows]))
        res[str(r)] = dict(mean=round(float(np.mean(accs)), 3), std=round(float(np.std(accs)), 3))
    return res

def latency(rows, n=3):
    ts = []
    for _ in range(n):
        for t, _ in rows:
            a = time.perf_counter(); eng.analyze("Вопрос", t); ts.append((time.perf_counter() - a) * 1000)
    return dict(mean_ms=round(float(np.mean(ts)), 2), p95_ms=round(float(np.percentile(ts, 95)), 2))

if __name__ == "__main__":
    out = {}
    out["manual"], out["manual_errors"] = cls_metrics(MANUAL_TEST)
    out["ext"], out["ext_errors"] = cls_metrics(EXT_TEST)
    out["auto_manual"] = auto_metrics(MANUAL_TEST); out["auto_ext"] = auto_metrics(EXT_TEST)
    old_ood = [q for q, a in RETR_TEST if a is None]
    out["ood_old"] = ood_metrics(old_ood); out["ood_new"] = ood_metrics(OOD_TEST)
    out["prio_old"], out["prio_old_errors"] = prio_metrics(PRIO_TEST)
    out["prio_new"], out["prio_new_errors"] = prio_metrics(PRIO_TEST2)
    out["hold"], out["hold_errors"] = cls_metrics(HOLDOUT)
    out["auto_hold"] = auto_metrics(HOLDOUT); out["ood_hold"] = ood_metrics(HOLDOUT_OOD)
    out["prio_hold"], out["prio_hold_errors"] = prio_metrics(HOLDOUT_PRIO)
    out["robust"] = robustness(MANUAL_TEST + EXT_TEST)
    out["latency"] = latency(EXT_TEST)
    path = sys.argv[1] if len(sys.argv) > 1 else "eval.json"
    json.dump(out, open(path, "w"), ensure_ascii=False, indent=1)
    for k, v in out.items():
        if "errors" not in k: print(k, v)
    print("\nОшибки ext:"); [print(" ", e) for e in out["ext_errors"]]
    print("Ошибки holdout:"); [print(" ", e) for e in out["hold_errors"]]
    print("Ошибки приоритета (нов.):"); [print(" ", e) for e in out["prio_new_errors"]]
