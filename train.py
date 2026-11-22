"""Обучение, сравнение моделей и оценка качества. Результаты пишутся в model/metrics.json и рисунки."""
import json, pickle, numpy as np, matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt
from sklearn.model_selection import train_test_split, StratifiedKFold, cross_val_score
from sklearn.linear_model import LogisticRegression
from sklearn.naive_bayes import MultinomialNB
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import accuracy_score, precision_recall_fscore_support, confusion_matrix
import ai
from data import *

X, y = ai.training_data()   # 6 учебных категорий + класс «вне тематики» (версия 1.1)
Xp = [ai.preprocess(t) for t in X]
Xtr, Xte, ytr, yte = train_test_split(Xp, y, test_size=0.2, stratify=y, random_state=1)
mt = [ai.preprocess(t) for t, _ in MANUAL_TEST]; my = [c for _, c in MANUAL_TEST]
out = {"n_train": len(Xtr), "n_test_synth": len(Xte), "n_test_manual": len(mt), "models": {}}

models = {"Логистическая регрессия": LogisticRegression(max_iter=2000, C=10.0),
          "Наивный Байес": MultinomialNB(alpha=0.1),
          "Случайный лес": RandomForestClassifier(n_estimators=200, random_state=1)}
for name, m in models.items():
    vec = ai.make_vectorizer(); A = vec.fit_transform(Xtr); m.fit(A, ytr)
    r = {}
    from eval_data import EXT_TEST, HOLDOUT
    sets = {"synth": (Xte, yte), "manual": (mt, my), "ext": ([ai.preprocess(t) for t, _ in EXT_TEST], [c for _, c in EXT_TEST]),
            "hold": ([ai.preprocess(t) for t, _ in HOLDOUT], [c for _, c in HOLDOUT])}
    inner = np.array([c != OOD_LABEL for c in m.classes_])
    for tag, (xs, ys) in sets.items():
        P_ = m.predict_proba(vec.transform(xs))
        if tag != "synth": P_ = np.where(inner, P_, -1)          # для учебных обращений выбирается учебная категория
        p = m.classes_[P_.argmax(1)]; pr, rc, f1, _ = precision_recall_fscore_support(ys, p, average="macro", zero_division=0)
        r[tag] = dict(acc=round(accuracy_score(ys, p), 3), prec=round(pr, 3), rec=round(rc, 3), f1=round(f1, 3))
    from sklearn.pipeline import make_pipeline
    cv = cross_val_score(make_pipeline(ai.make_vectorizer(), m.__class__(**m.get_params())), Xp, y,
                         cv=StratifiedKFold(5, shuffle=True, random_state=1), scoring="f1_macro")
    r["cv_f1"] = round(float(cv.mean()), 3); r["cv_std"] = round(float(cv.std()), 3)
    out["models"][name] = r; print(name, r)

# итоговая модель: обучаем на всей синтетической выборке, сохраняем
import os
if os.path.exists(ai.MODEL_PATH): os.remove(ai.MODEL_PATH)
eng0 = ai.Engine()                     # обучает модель и сохраняет её вместе с версиями модуля и scikit-learn
res = [eng0.classify(t) for t in [x for x, _ in MANUAL_TEST]]
pred = np.array([r[0] for r in res]); conf = np.array([r[1] for r in res])
from eval_data import HOLDOUT
hy = [c for _, c in HOLDOUT]; hp = [eng0.classify(t)[0] for t, _ in HOLDOUT]
cm = confusion_matrix(hy, hp, labels=CATEGORIES)
fig, ax = plt.subplots(figsize=(6.4, 5.4)); ax.imshow(cm, cmap="Purples")
ax.set_xticks(range(6)); ax.set_yticks(range(6)); short = ["Расп.", "Экз.", "Оплата", "Матер.", "Техн.", "Админ."]
ax.set_xticklabels(short, rotation=30); ax.set_yticklabels(short); ax.set_xlabel("Предсказано"); ax.set_ylabel("Истинная категория"); ax.set_title("Контрольный набор (36 обращений)", fontsize=10)
for i in range(6):
    for j in range(6): ax.text(j, i, cm[i, j], ha="center", va="center", color="white" if cm[i, j] > cm.max()/2 else "black")
plt.tight_layout(); plt.savefig("model/confusion.png", dpi=150); plt.close()
out["manual_conf_correct"] = round(float(conf[pred == np.array(my)].mean()), 3)
out["manual_conf_wrong"] = round(float(conf[pred != np.array(my)].mean()), 3) if (pred != np.array(my)).any() else None
out["manual_errors"] = [(t, a, b) for (t, a), b in zip(MANUAL_TEST, pred) if a != b]

# приоритет
ok = 0; rows = []
from eval_data import PRIO_TEST2
for t, c, e in PRIO_TEST + PRIO_TEST2:
    l, s = ai.priority(t, c); ok += (l == e); rows.append((t, c, e, l, s))
out["prio_acc"] = round(ok / len(rows), 3); out["prio_rows"] = rows

# семантический поиск
kb = ai.KnowledgeBase(); h1 = h3 = n = 0; neg_sims = []; pos_sims = []; neg_total = 0
for q, a in RETR_TEST:
    r = kb.search(q, 3)
    if a is None: neg_sims.append(r[0][1]); continue
    n += 1; h1 += r[0][0]["id"] == a; h3 += any(x[0]["id"] == a for x in r); pos_sims.append(r[0][1])
out["retr"] = dict(n=n, hit1=round(h1/n, 3), hit3=round(h3/n, 3),
                   pos_min=round(min(pos_sims), 3), pos_mean=round(float(np.mean(pos_sims)), 3),
                   neg_max=round(max(neg_sims), 3), neg_mean=round(float(np.mean(neg_sims)), 3), n_neg=len(neg_sims))
# порог τs/τc: доля отказов на «вне базы» при текущих порогах и доля автоответов на ручном наборе
eng = ai.Engine()
rej = sum(1 for q, a in RETR_TEST if a is None and not eng.analyze("", q)["auto"])
out["reject_ood"] = f"{rej}/{out['retr']['n_neg']}"
auto = sum(1 for t, c in MANUAL_TEST if eng.analyze("", t)["auto"]); out["manual_auto"] = f"{auto}/{len(MANUAL_TEST)}"
out["thresholds"] = dict(tau_c=ai.TAU_C, tau_s=ai.TAU_S, tau_ood=ai.TAU_OOD, model=ai.MODEL_VERSION, T_high=ai.T_HIGH, T_mid=ai.T_MID)
json.dump(out, open("model/metrics.json", "w"), ensure_ascii=False, indent=1, default=str)
print(json.dumps({k: v for k, v in out.items() if k not in ("prio_rows",)}, ensure_ascii=False, indent=1, default=str))
for r in rows: print(r)
