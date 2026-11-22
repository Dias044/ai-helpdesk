"""Подбор весов и порогов формулы (3') по сетке на размеченных наборах PRIO_TEST + PRIO_TEST2."""
import itertools, re, json
import ai
from data import PRIO_TEST
from eval_data import PRIO_TEST2
rows = PRIO_TEST + PRIO_TEST2
def feats(t):
    t = t.lower().replace("ё", "е")
    return (min(1, ai._hits(t, ai.URGENT, ai.URGENT_RE) / 2), min(1, ai._hits(t, ai.NEGATIVE, ai.NEGATIVE_RE) / 2), min(1, ai._hits(t, ai.PROBLEM)))
F = [(feats(t), ai.RISK[c], e) for t, c, e in rows]
best = []
g = [round(x * 0.05, 2) for x in range(0, 13)]
for wu, wn, wp in itertools.product(g, g, g):
    wc = round(1 - wu - wn - wp, 2)
    if wc < 0.1: continue
    for th, tm in [(0.6, 0.35), (0.55, 0.3), (0.6, 0.3), (0.55, 0.35), (0.5, 0.3), (0.65, 0.35)]:
        ok = 0
        for (U, N, P), R, e in F:
            S = wu * U + wn * N + wp * P + wc * R
            ok += e == ("Высокий" if S >= th else "Средний" if S >= tm else "Низкий")
        best.append((ok, -abs(wc - 0.3), wu, wn, wp, wc, th, tm))
best.sort(reverse=True)
for b in best[:8]: print(b[0], "/", len(rows), dict(wu=b[2], wn=b[3], wp=b[4], wc=b[5], T_high=b[6], T_mid=b[7]))
