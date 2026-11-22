"""ИИ-модуль (версия 1.1): предобработка с нормализацией лексики, классификация с классом «вне тематики»,
приоритет, семантический поиск, решение об автоответе, подсказки оператору."""
import re, pickle, os
import numpy as np
import sklearn
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from data import CATEGORIES, KB, gen_dataset, gen_ood, OOD_LABEL

MODEL_VERSION = "tfidf-lr-1.1"
MODEL_PATH = os.path.join(os.path.dirname(__file__), "model", "classifier.pkl")
TAU_C, TAU_S = 0.45, 0.12          # пороги условия (4)
TAU_OOD = 0.35                     # порог вероятности класса «вне тематики» (условие 4а)
W_U, W_N, W_P, W_C = 0.35, 0.20, 0.20, 0.25   # веса формулы (3') — подобраны по сетке в СРО4 (tune_priority.py)
T_HIGH, T_MID = 0.60, 0.30         # пороги приоритета (T_mid снижен с 0,35)

# Словарь нормализации: англоязычные, казахские и транслитерированные слова -> русские ключевые слова.
# Добавлен по результатам тестирования: такие обращения классифицировались с ошибками.
GLOSSARY = {
 r"\btime ?table|\bschedule|raspisan|\bкесте": " расписание ", r"\bclass(es)?\b|\bсабақ|\bpara\b": " пара занятие ",
 r"\bexam|\bemtihan|емтихан|\bsession\b": " экзамен ", r"\bretake|\bresit|peresdach": " пересдача экзамена ",
 r"\bgrade|\bbaga\b|\bбаға": " оценка ",
 r"\bpay(ment)?\b|\btuition|\bfee\b|\boplat|төле|төлем|ақы": " оплата обучения ", r"\brefund": " возврат денег оплата ",
 r"\bkaspi|каспи": " оплата через банк ", r"\bконтракт": " оплата обучения ",
 r"\blecture|\bslides?\b|\bnotes\b|\bmaterials?\b|\bsyllabus|силлабус": " лекции учебные материалы ",
 r"\btextbook|\bbook\b|оқулық": " учебник ", r"\bcourse\b": " курс дисциплина ", r"\blibrary|кітапхана": " библиотека ",
 r"\blog ?in\b|\bsign ?in|\bportal\b|кіре алма": " вход личный кабинет не могу войти ", r"\bpassword|құпия сөз": " пароль ",
 r"\berror\b|\bbug\b|қате": " ошибка ", r"\bwi-?fi\b|вайфай|вай-фай": " wi-fi ", r"\bnot working|\bdoesn.?t work|жұмыс істемейді": " не работает ",
 r"\bcertificate|\bspravk|анықтама": " справка ", r"\btranscript|транскрипт": " выписка оценок ",
 r"\benrollment|\btransfer\b|\bdean": " деканат перевод ", r"\bdorm|общаг": " общежитие ",
}
_GLOSS = [(re.compile(k), v) for k, v in GLOSSARY.items()]

URGENT = ["срочн", "сегодня", "завтра", "не могу", "до конца", "дедлайн", "срок", "немедленно", "последний день",
          "горит", "не допустил", "нет в списке", "нет в ведомости", "закрывается", "экзамен через"]
URGENT_RE = [r"через \d+ ?(мин|час)", r"через (час|полчаса)", r"\d+ ?минут", r"\bк (понедельник|вторник|сред|четверг|пятниц|суббот|завтра)",
             r"\bдо (понедельник|вторник|сред|четверг|пятниц|суббот|завтра|конца)"]
NEGATIVE = ["ужас", "безобразие", "возмутительно", "никто не отвечает", "никто не помогает", "надоело", "опять",
            "жалоб", "недоволен", "сколько можно", "издевательство", "невозможно"]
NEGATIVE_RE = [r"(второй|третий|четв[её]ртый|пятый|\d+-?й?) (день|раз)", r"(вторую|третью|\d+-?ю?) недел", r"уже (неделю|месяц|\d+ дн)"]
PROBLEM = ["не работает", "не открыва", "не отображ", "не приход", "не приш", "не видно", "не указан", "не загруж", "не грузит",
           "не проход", "не отвечает", "ошибк", "сбой", "завис", "заблокир", "списал", "проблем", "висит", "вылета"]
RISK = {"Техническая проблема": 1.0, "Экзамены": 0.8, "Оплата": 0.7,
        "Административные вопросы": 0.5, "Расписание": 0.4, "Учебные материалы": 0.4}

def preprocess(text: str) -> str:
    """Очистка: нижний регистр, «ё»->«е», нормализация лексики по словарю, удаление служебных символов."""
    t = (text or "").lower().replace("ё", "е")
    for rx, rep in _GLOSS:
        if rx.search(t): t = t + " " + rep                 # ключевые слова добавляются, исходный текст сохраняется
    t = re.sub(r"[^\w\s-]", " ", t)
    return re.sub(r"\s+", " ", t).strip()

def make_vectorizer():
    # символьные n-граммы компенсируют отсутствие лемматизации и устойчивы к опечаткам
    return TfidfVectorizer(analyzer="char_wb", ngram_range=(2, 5), sublinear_tf=True, min_df=1)

def training_data():
    X, y = gen_dataset(); Xo = gen_ood()
    return X + Xo, y + [OOD_LABEL] * len(Xo)

def train_classifier(X=None, y=None):
    if X is None: X, y = training_data()
    vec = make_vectorizer(); M = vec.fit_transform([preprocess(t) for t in X])
    clf = LogisticRegression(max_iter=3000, C=10.0).fit(M, y)
    return vec, clf

def _hits(text, words, patterns=()):
    return sum(1 for w in words if w in text) + sum(1 for p in patterns if re.search(p, text))

def priority(text: str, category: str):
    """Формула (3'): S = wu*U + wn*N + wp*P + wc*R(c), где P — признак сбоя/проблемы (добавлен в СРО4)."""
    t = (text or "").lower().replace("ё", "е")
    U = min(1.0, _hits(t, URGENT, URGENT_RE) / 2); N = min(1.0, _hits(t, NEGATIVE, NEGATIVE_RE) / 2)
    P = min(1.0, _hits(t, PROBLEM) / 1)
    S = W_U * U + W_N * N + W_P * P + W_C * RISK.get(category, 0.5)
    label = "Высокий" if S >= T_HIGH else ("Средний" if S >= T_MID else "Низкий")
    return label, round(S, 3)

class KnowledgeBase:
    """Семантический поиск по базе знаний: TF-IDF (символьные n-граммы) + косинусная близость, формула (2)."""
    def __init__(self, articles=None):
        self.articles = articles or [dict(id=a[0], category=a[1], title=a[2], text=a[3], source=a[4], updated=a[5]) for a in KB]
        self.vec = TfidfVectorizer(analyzer="char_wb", ngram_range=(3, 5), sublinear_tf=True)
        self.M = self.vec.fit_transform([preprocess(a["title"] + " " + a["title"] + " " + a["text"]) for a in self.articles])
    def search(self, query, k=3, category=None):
        q = self.vec.transform([preprocess(query)])
        sims = (self.M @ q.T).toarray().ravel()
        if category:                                  # поиск только внутри предсказанной категории
            sims = np.where([a["category"] == category for a in self.articles], sims, -1.0)
        idx = np.argsort(-sims)[:k]
        return [(self.articles[i], float(sims[i])) for i in idx]

class Engine:
    def __init__(self):
        loaded = None
        if os.path.exists(MODEL_PATH):
            try:
                with open(MODEL_PATH, "rb") as f: loaded = pickle.load(f)
            except Exception: loaded = None
        # модель переобучается, если файл отсутствует, повреждён, создан другой версией модуля или scikit-learn
        if not (isinstance(loaded, dict) and loaded.get("version") == MODEL_VERSION and loaded.get("sklearn") == sklearn.__version__):
            vec, clf = train_classifier()
            loaded = dict(version=MODEL_VERSION, sklearn=sklearn.__version__, vec=vec, clf=clf)
            os.makedirs(os.path.dirname(MODEL_PATH), exist_ok=True)
            with open(MODEL_PATH, "wb") as f: pickle.dump(loaded, f)
        self.vec, self.clf = loaded["vec"], loaded["clf"]
        self.kb = KnowledgeBase(); self.version = MODEL_VERSION

    def probs(self, text):
        p = self.clf.predict_proba(self.vec.transform([preprocess(text)]))[0]
        return dict(zip(self.clf.classes_, p))

    def classify(self, text, p=None):
        """Формула (1) по учебным категориям; вероятность «вне тематики» не участвует в выборе категории."""
        p = p if p is not None else self.probs(text); inner = {c: v for c, v in p.items() if c != OOD_LABEL}
        cat = max(inner, key=inner.get); s = sum(inner.values()) or 1.0
        return cat, float(inner[cat] / s)

    def analyze(self, subject, text, active=None):
        full = f"{subject}. {text}"
        p = self.probs(full); p_ood = float(p.get(OOD_LABEL, 0.0))
        cat, conf = self.classify(full, p)        # векторизация выполняется один раз
        prio, score = priority(full, cat)
        art, sim = self.kb.search(full, 1, category=cat)[0]
        reason = None
        if p_ood >= TAU_OOD: reason = "вне тематики"
        elif active is not None and cat not in active: reason = "категория отключена"
        elif conf < TAU_C: reason = "низкая уверенность"
        elif sim < TAU_S: reason = "нет ответа в базе знаний"
        auto = reason is None                                   # условие (4) + (4а)
        res = dict(category=cat, confidence=round(conf, 3), priority=prio, score=score, similarity=round(sim, 3),
                   p_ood=round(p_ood, 3), auto=auto, reason=reason, article=None, answer=None)
        if auto:
            res["article"] = dict(id=art["id"], title=art["title"], source=art["source"], updated=art["updated"])
            res["answer"] = art["text"]
        return res

    def suggest(self, text, k=3):
        """Подсказки оператору: k ближайших документов по всей базе знаний."""
        return [dict(id=a["id"], title=a["title"], category=a["category"], text=a["text"], source=a["source"], similarity=round(s, 3))
                for a, s in self.kb.search(text, k)]
