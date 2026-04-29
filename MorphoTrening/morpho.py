import re

try:
    import pymorphy3
    _morph = pymorphy3.MorphAnalyzer()
    MORPH_AVAILABLE = True
except Exception:
    MORPH_AVAILABLE = False

POS_MAP = {
    'NOUN': 'Существительное',
    'ADJF': 'Прилагательное',
    'ADJS': 'Прилагательное',
    'COMP': 'Прилагательное',
    'VERB': 'Глагол',
    'INFN': 'Глагол',
    'PRTF': 'Причастие',
    'PRTS': 'Причастие',
    'GRND': 'Деепричастие',
    'NUMR': 'Числительное',
    'NUMB': 'Числительное',
    'NPRO': 'Местоимение',
    'PRED': 'Наречие',
    'PREP': 'Предлог',
    'CONJ': 'Союз',
    'PRCL': 'Частица',
    'INTJ': 'Междометие',
    'ADVB': 'Наречие',
}

POS_COLORS = {
    'Существительное': '#3b82f6',
    'Прилагательное':  '#22c55e',
    'Глагол':          '#ef4444',
    'Наречие':         '#a855f7',
    'Местоимение':     '#f59e0b',
    'Предлог':         '#6b7280',
    'Союз':            '#ec4899',
    'Частица':         '#06b6d4',
    'Причастие':       '#f97316',
    'Деепричастие':    '#84cc16',
    'Числительное':    '#14b8a6',
    'Междометие':      '#b45309',
}

ALL_POS = list(POS_COLORS.keys())


# его/её/их как притяжательные — pymorphy3 может дать ADJF, школьная грамматика: Местоимение
_PRONOUN_OVERRIDES = frozenset({'его', 'её', 'их'})


def tokenize_words(text: str) -> list[str]:
    return re.findall(r'[а-яёА-ЯЁa-zA-Z]+', text)


def get_word_pos(word: str) -> str:
    if not MORPH_AVAILABLE:
        return 'Существительное'

    if word.lower() in _PRONOUN_OVERRIDES:
        return 'Местоимение'

    p = _morph.parse(word)[0]
    pos = p.tag.POS
    grammemes = p.tag.grammemes

    # Местоименные прилагательные (Apro): этот, тот, мой, твой, наш, ваш, какой, который...
    # pymorphy3 тегирует их как ADJF, но в школьной грамматике — Местоимение
    if pos in ('ADJF', 'ADJS') and 'Apro' in grammemes:
        return 'Местоимение'

    # Порядковые числительные (Anum): первый, второй, третий...
    # pymorphy3 тегирует как ADJF, но в школьной грамматике — Числительное
    if pos == 'ADJF' and 'Anum' in grammemes:
        return 'Числительное'

    return POS_MAP.get(pos, 'Существительное')


def analyze_sentence(text: str) -> list[dict]:
    return [{'word': w, 'pos': get_word_pos(w)} for w in tokenize_words(text)]
