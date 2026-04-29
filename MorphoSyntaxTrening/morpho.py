import re

try:
    import pymorphy3
    _morph = pymorphy3.MorphAnalyzer()
    MORPH_AVAILABLE = True
except Exception:
    MORPH_AVAILABLE = False

POS_MAP = {
    "NOUN": "Существительное",
    "ADJF": "Прилагательное",
    "ADJS": "Прилагательное",
    "COMP": "Прилагательное",
    "VERB": "Глагол",
    "INFN": "Глагол",
    "PRTF": "Причастие",
    "PRTS": "Причастие",
    "GRND": "Деепричастие",
    "NUMR": "Числительное",
    "NPRO": "Местоимение",
    "PRED": "Наречие",
    "PREP": "Предлог",
    "CONJ": "Союз",
    "PRCL": "Частица",
    "INTJ": "Междометие",
    "ADVB": "Наречие",
}

POS_COLORS = {
    "Существительное": "#93c5fd",   # blue-300
    "Прилагательное":  "#86efac",   # green-300
    "Глагол":          "#fca5a5",   # red-300
    "Наречие":         "#d8b4fe",   # purple-300
    "Местоимение":     "#fcd34d",   # amber-300
    "Предлог":         "#d1d5db",   # gray-300
    "Союз":            "#f9a8d4",   # pink-300
    "Частица":         "#67e8f9",   # cyan-300
    "Причастие":       "#fdba74",   # orange-300
    "Деепричастие":    "#bef264",   # lime-300
    "Числительное":    "#5eead4",   # teal-300
    "Междометие":      "#c6a97d",   # warm tan-300
}

ALL_POS = list(POS_COLORS.keys())

# Притяжательные местоимения: pymorphy3 может дать ADJF
_PRONOUN_OVERRIDES = frozenset({"его", "её", "их"})


def tokenize_words(text: str) -> list[str]:
    return re.findall(r"[а-яёА-ЯЁa-zA-Z]+", text)


def get_word_pos(word: str) -> str:
    if not MORPH_AVAILABLE:
        return "Существительное"
    if word.lower() in _PRONOUN_OVERRIDES:
        return "Местоимение"
    p = _morph.parse(word)[0]
    pos = p.tag.POS
    grammemes = p.tag.grammemes
    # Местоименные прилагательные (этот, мой, твой, какой…) → Местоимение
    if pos in ("ADJF", "ADJS") and "Apro" in grammemes:
        return "Местоимение"
    # Порядковые числительные (первый, второй…) → Числительное
    if pos == "ADJF" and "Anum" in grammemes:
        return "Числительное"
    return POS_MAP.get(pos, "Существительное")


def analyze_sentence(text: str) -> list[dict]:
    return [{"word": w, "pos": get_word_pos(w)} for w in tokenize_words(text)]
