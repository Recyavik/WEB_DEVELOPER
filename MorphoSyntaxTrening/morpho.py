"""
morpho.py — Russian morphological analyzer for MorphoSyntaxTrening.

Provides:
  analyze_sentence(text)      → list[dict]   (backward-compat, POS only)
  full_analyze(text, level)   → list[TokenAnalysis as dict]
"""

import re
from dataclasses import dataclass, field, asdict
from typing import Optional

# ── pymorphy3 ──────────────────────────────────────────────────────────────────
try:
    import pymorphy3
    _morph = pymorphy3.MorphAnalyzer()
    MORPH_AVAILABLE = True
except Exception:
    MORPH_AVAILABLE = False

# ── Natasha (optional, adds context disambiguation + dep-parse) ────────────────
try:
    from natasha import (
        Segmenter, MorphVocab,
        NewsEmbedding, NewsMorphTagger, NewsSyntaxParser,
        Doc,
    )
    _segmenter   = Segmenter()
    _morph_vocab = MorphVocab()
    _emb         = NewsEmbedding()
    _morph_tagger  = NewsMorphTagger(_emb)
    _syntax_parser = NewsSyntaxParser(_emb)
    NATASHA_AVAILABLE = True
except Exception:
    NATASHA_AVAILABLE = False

# ── POS mapping ────────────────────────────────────────────────────────────────
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

# ── Trainer level constants ────────────────────────────────────────────────────
LEVEL_INTRODUCTORY = "introductory"
LEVEL_ELEMENTARY   = "elementary"
LEVEL_BASIC        = "basic"
LEVEL_ADVANCED     = "advanced"
LEVEL_EXPERT       = "expert"

LEVEL_LABELS = {
    LEVEL_INTRODUCTORY: "Ознакомительный",
    LEVEL_ELEMENTARY:   "Начальный",
    LEVEL_BASIC:        "Базовый",
    LEVEL_ADVANCED:     "Углублённый",
    LEVEL_EXPERT:       "Продвинутый",
}

# ── Scoring: which feature keys are checked at each level ─────────────────────
# Required fields per level (used by server-side scoring and by JS form renderer)
LEVEL_REQUIRED_FIELDS = {
    LEVEL_INTRODUCTORY: ["pos"],
    LEVEL_ELEMENTARY:   ["pos", "lemma"],
    LEVEL_BASIC:        ["pos", "lemma", "var_features"],
    LEVEL_ADVANCED:     ["pos", "lemma", "var_features", "const_features", "syntax_role"],
    LEVEL_EXPERT:       ["pos", "lemma", "var_features", "const_features", "syntax_role"],
}

# Variable features checked at basic+ (per POS)
SCORED_VAR_FEATURES: dict[str, list[str]] = {
    "Существительное": ["число", "падеж"],
    "Прилагательное":  ["число", "падеж", "род"],
    "Глагол":          ["время", "число", "лицо", "род"],
    "Причастие":       ["число", "падеж", "род"],
    "Деепричастие":    [],
    "Местоимение":     ["число", "падеж"],
    "Числительное":    ["падеж"],
    "Наречие":         [],
    "Предлог":         [],
    "Союз":            [],
    "Частица":         [],
    "Междометие":      [],
}

# Constant features checked at advanced+ (per POS)
SCORED_CONST_FEATURES: dict[str, list[str]] = {
    "Существительное": ["род", "одушевлённость"],
    "Прилагательное":  ["разряд"],
    "Глагол":          ["вид", "переходность"],
    "Причастие":       ["вид", "залог"],
    "Деепричастие":    ["вид"],
    "Местоимение":     ["лицо"],
    "Числительное":    ["разряд"],
    "Наречие":         [],
    "Предлог":         [],
    "Союз":            [],
    "Частица":         [],
    "Междометие":      [],
}

# Possessive pronouns pymorphy3 tags as ADJF
_PRONOUN_OVERRIDES = frozenset({"его", "её", "их"})

LEVEL_COLORS = {
    LEVEL_INTRODUCTORY: "#d1d5db",   # gray
    LEVEL_ELEMENTARY:   "#bfdbfe",   # blue
    LEVEL_BASIC:        "#bbf7d0",   # green
    LEVEL_ADVANCED:     "#fed7aa",   # orange
    LEVEL_EXPERT:       "#e9d5ff",   # purple
}

# ── Feature labels (Russian school terminology) ────────────────────────────────
GENDER_MAP = {
    "masc": "мужской", "femn": "женский", "neut": "средний",
    "Ms-f": "общий",
}
NUMBER_MAP = {"sing": "единственное", "plur": "множественное"}
CASE_MAP = {
    "nomn": "именительный", "gent": "родительный",
    "datv": "дательный",    "accs": "винительный",
    "ablt": "творительный", "loct": "предложный",
    "gen2": "родительный (2-й)", "acc2": "винительный (2-й)",
    "loc2": "предложный (2-й)", "voct": "звательный",
}
TENSE_MAP = {"past": "прошедшее", "pres": "настоящее", "futr": "будущее"}
MOOD_MAP  = {"indc": "изъявительное", "impr": "повелительное"}
PERSON_MAP = {"1per": "1-е", "2per": "2-е", "3per": "3-е"}
VOICE_MAP  = {"actv": "действительный", "pssv": "страдательный"}
ASPECT_MAP = {"perf": "совершенный", "impf": "несовершенный"}
TRANS_MAP  = {"tran": "переходный", "intr": "непереходный"}
ANIMACY_MAP = {"anim": "одушевлённое", "inan": "неодушевлённое"}
SHORT_MAP  = {"Apro": None, "Anum": None}  # handled separately

# UD → school dep-rel labels (Natasha uses UD)
DEPREL_MAP = {
    "nsubj":  "подлежащее",
    "ROOT":   "сказуемое",
    "root":   "сказуемое",
    "obj":    "дополнение",
    "iobj":   "дополнение",
    "obl":    "обстоятельство",
    "advmod": "обстоятельство",
    "amod":   "определение",
    "nmod":   "определение",
    "appos":  "приложение",
    "nummod": "числовое определение",
    "det":    "определение",
    "case":   "предлог",
    "mark":   "союзное слово",
    "cc":     "союз",
    "conj":   "однородный член",
    "parataxis": "обстоятельство",
    "punct":  "пунктуация",
    "flat":   "именная группа",
    "compound": "сложное слово",
    "aux":    "вспомогательный глагол",
    "cop":    "связка",
    "xcomp":  "именная часть сказуемого",
    "csubj":  "подлежащее",
    "ccomp":  "дополнение",
    "acl":    "определение",
    "advcl":  "обстоятельство",
    "vocative": "обращение",
    "discourse": "вводное слово",
    "expl":   "вводное слово",
}


# ── Data structure ─────────────────────────────────────────────────────────────
@dataclass
class TokenAnalysis:
    word:           str
    index:          int
    pos:            str              # Russian POS name
    lemma:          str = ""
    # constant features (не изменяются при склонении/спряжении)
    const_features: dict = field(default_factory=dict)
    # variable features (изменяются: падеж, число, время…)
    var_features:   dict = field(default_factory=dict)
    # syntax role in sentence
    syntax_role:    str = ""
    # human-readable explanation per level
    explanation:    str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    # backward-compat: correct_pos for introductory level
    @property
    def correct_pos(self) -> str:
        return self.pos


# ── Low-level pymorphy3 helpers ────────────────────────────────────────────────
def tokenize_words(text: str) -> list[str]:
    return re.findall(r"[а-яёА-ЯЁa-zA-Z]+", text)


def _get_grammeme(grammemes: frozenset, mapping: dict) -> str:
    for key, val in mapping.items():
        if key in grammemes:
            return val
    return ""


def _parse_pymorphy(word: str):
    """Return best pymorphy3 parse, with pronoun/numeral overrides."""
    if not MORPH_AVAILABLE:
        return None
    if word.lower() in _PRONOUN_OVERRIDES:
        return None  # will be handled as override
    p = _morph.parse(word)[0]
    return p


def get_word_pos(word: str) -> str:
    """Quick POS lookup — used by backward-compat analyze_sentence()."""
    if not MORPH_AVAILABLE:
        return "Существительное"
    if word.lower() in _PRONOUN_OVERRIDES:
        return "Местоимение"
    p = _morph.parse(word)[0]
    pos = p.tag.POS
    grammemes = p.tag.grammemes
    if pos in ("ADJF", "ADJS") and "Apro" in grammemes:
        return "Местоимение"
    if pos == "ADJF" and "Anum" in grammemes:
        return "Числительное"
    return POS_MAP.get(pos, "Существительное")


def _pos_from_parse(p) -> str:
    """Extract Russian POS name from a pymorphy3 parse object."""
    if p is None:
        return "Существительное"
    pos = p.tag.POS
    grammemes = p.tag.grammemes
    if pos in ("ADJF", "ADJS") and "Apro" in grammemes:
        return "Местоимение"
    if pos == "ADJF" and "Anum" in grammemes:
        return "Числительное"
    return POS_MAP.get(pos, "Существительное")


# ── Constant features per POS ──────────────────────────────────────────────────
def _const_features(pos_ru: str, p) -> dict:
    """Extract constant (unchanging) grammatical features."""
    if p is None:
        return {}
    g = p.tag.grammemes
    result = {}

    if pos_ru == "Существительное":
        gender = _get_grammeme(g, GENDER_MAP)
        if gender:
            result["род"] = gender
        anim = _get_grammeme(g, ANIMACY_MAP)
        if anim:
            result["одушевлённость"] = anim
        # declension type by gender hint
        if gender == "мужской":
            result["склонение"] = "2-е"
        elif gender == "женский":
            result["склонение"] = "1-е"
        elif gender == "средний":
            result["склонение"] = "2-е"

    elif pos_ru == "Прилагательное":
        if "Qual" in g:
            result["разряд"] = "качественное"
        elif "Anum" not in g and "Apro" not in g:
            result["разряд"] = "относительное"

    elif pos_ru in ("Глагол",):
        aspect = _get_grammeme(g, ASPECT_MAP)
        if aspect:
            result["вид"] = aspect
        trans = _get_grammeme(g, TRANS_MAP)
        if trans:
            result["переходность"] = trans
        # conjugation class (1st / 2nd) — approximate
        lemma = p.normal_form
        if lemma.endswith(("ить", "еть")):
            result["спряжение"] = "2-е"
        else:
            result["спряжение"] = "1-е"

    elif pos_ru == "Причастие":
        aspect = _get_grammeme(g, ASPECT_MAP)
        if aspect:
            result["вид"] = aspect
        voice = _get_grammeme(g, VOICE_MAP)
        if voice:
            result["залог"] = voice

    elif pos_ru == "Деепричастие":
        aspect = _get_grammeme(g, ASPECT_MAP)
        if aspect:
            result["вид"] = aspect

    elif pos_ru == "Числительное":
        if "Anum" in g:
            result["разряд"] = "порядковое"
        elif "NUMR" in g:
            result["разряд"] = "количественное"

    elif pos_ru == "Местоимение":
        # personal pronoun person
        person = _get_grammeme(g, PERSON_MAP)
        if person:
            result["лицо"] = person

    return result


# ── Variable features per POS ──────────────────────────────────────────────────
def _var_features(pos_ru: str, p) -> dict:
    """Extract variable (inflected) grammatical features."""
    if p is None:
        return {}
    g = p.tag.grammemes
    result = {}

    def _add(key, mapping):
        val = _get_grammeme(g, mapping)
        if val:
            result[key] = val

    if pos_ru in ("Существительное", "Прилагательное", "Причастие",
                  "Местоимение", "Числительное"):
        _add("число", NUMBER_MAP)
        _add("падеж", CASE_MAP)
        if pos_ru != "Существительное":
            _add("род", GENDER_MAP)

    elif pos_ru == "Глагол":
        tense = _get_grammeme(g, TENSE_MAP)
        if tense:
            result["время"] = tense
        _add("число", NUMBER_MAP)
        mood = _get_grammeme(g, MOOD_MAP)
        if mood:
            result["наклонение"] = mood
        if tense == "настоящее" or tense == "будущее":
            _add("лицо", PERSON_MAP)
        if tense == "прошедшее":
            _add("род", GENDER_MAP)

    return result


# ── Syntax role guessing without Natasha ──────────────────────────────────────
_SYNTAX_ROLE_SIMPLE = {
    "Существительное": "дополнение / подлежащее",
    "Прилагательное":  "определение",
    "Глагол":          "сказуемое",
    "Наречие":         "обстоятельство",
    "Местоимение":     "подлежащее / дополнение",
    "Предлог":         "предлог",
    "Союз":            "союз",
    "Частица":         "частица",
    "Причастие":       "определение",
    "Деепричастие":    "обстоятельство",
    "Числительное":    "числовой компонент",
    "Междометие":      "вводное слово",
}


def _guess_syntax_role(word: str, pos_ru: str, p) -> str:
    if p is None:
        return _SYNTAX_ROLE_SIMPLE.get(pos_ru, "")
    g = p.tag.grammemes
    if pos_ru == "Существительное":
        case = _get_grammeme(g, CASE_MAP)
        if case == "именительный":
            return "подлежащее"
        if case in ("родительный", "дательный", "винительный",
                    "творительный", "предложный"):
            return "дополнение"
    if pos_ru == "Глагол":
        if p.tag.POS == "INFN":
            return "сказуемое / дополнение"
        return "сказуемое"
    return _SYNTAX_ROLE_SIMPLE.get(pos_ru, "")


# ── Natasha-based full sentence analysis ──────────────────────────────────────
def _natasha_analyze(text: str) -> list[dict] | None:
    """
    Run Natasha pipeline and return list of dicts:
      {word, lemma, pos_ud, feats, head_id, deprel}
    Returns None if Natasha is unavailable or fails.
    """
    if not NATASHA_AVAILABLE:
        return None
    try:
        doc = Doc(text)
        doc.segment(_segmenter)
        doc.tag_morph(_morph_tagger)
        for token in doc.tokens:
            token.lemmatize(_morph_vocab)
        doc.parse_syntax(_syntax_parser)

        results = []
        for token in doc.tokens:
            results.append({
                "word":    token.text,
                "lemma":   token.lemma or "",
                "pos_ud":  token.pos or "",
                "feats":   token.feats or {},
                "head_id": getattr(token, "head_id", None),
                "deprel":  getattr(token, "rel", "") or "",
            })
        return results
    except Exception:
        return None


# UD POS → Russian POS (Natasha uses Universal Dependencies tags)
_UD_POS_MAP = {
    "NOUN":  "Существительное",
    "PROPN": "Существительное",
    "ADJ":   "Прилагательное",
    "VERB":  "Глагол",
    "ADV":   "Наречие",
    "PRON":  "Местоимение",
    "ADP":   "Предлог",
    "CCONJ": "Союз",
    "SCONJ": "Союз",
    "PART":  "Частица",
    "INTJ":  "Междометие",
    "NUM":   "Числительное",
    "DET":   "Местоимение",
    "AUX":   "Глагол",
    "PUNCT": "",
    "SYM":   "",
    "X":     "",
}


# ── Level-specific explanation builder ────────────────────────────────────────
def _build_explanation(pos_ru: str, lemma: str, const: dict, var: dict,
                       syntax: str, level: str) -> str:
    parts = [f"Часть речи: {pos_ru}"]
    if level in (LEVEL_ELEMENTARY, LEVEL_BASIC, LEVEL_ADVANCED, LEVEL_EXPERT):
        if lemma:
            parts.append(f"Начальная форма: {lemma}")
        if const:
            parts.append("Постоянные признаки: " +
                         ", ".join(f"{k} — {v}" for k, v in const.items()))
    if level in (LEVEL_BASIC, LEVEL_ADVANCED, LEVEL_EXPERT):
        if var:
            parts.append("Непостоянные признаки: " +
                         ", ".join(f"{k} — {v}" for k, v in var.items()))
    if level in (LEVEL_ADVANCED, LEVEL_EXPERT):
        if syntax:
            parts.append(f"Синтаксическая роль: {syntax}")
    return "; ".join(parts)


# ── Main analysis function ─────────────────────────────────────────────────────
def full_analyze(text: str, level: str = LEVEL_ADVANCED) -> list[dict]:
    """
    Full morphological analysis of `text`.
    Returns list of TokenAnalysis dicts.
    Level controls how much detail is computed and stored.
    """
    words = tokenize_words(text)
    if not words:
        return []

    # Try Natasha first for context-aware analysis (all levels — POS accuracy matters)
    natasha_data = _natasha_analyze(text)

    # Build a word→natasha_token lookup (by surface form, order-sensitive)
    nat_by_index: list[dict | None] = []
    if natasha_data:
        nat_iter = iter(natasha_data)
        for w in words:
            matched = None
            for nt in nat_iter:
                if nt["word"].lower() == w.lower():
                    matched = nt
                    break
            nat_by_index.append(matched)
    else:
        nat_by_index = [None] * len(words)

    results = []
    for i, word in enumerate(words):
        nt = nat_by_index[i] if i < len(nat_by_index) else None

        # POS determination
        if word.lower() in _PRONOUN_OVERRIDES:
            pos_ru = "Местоимение"
            p = _morph.parse(word)[0] if MORPH_AVAILABLE else None
        elif nt and nt["pos_ud"]:
            pos_ru = _UD_POS_MAP.get(nt["pos_ud"], "")
            if not pos_ru:
                # Natasha gave PUNCT or unknown — skip token
                continue
            # ROOT-as-NOUN heuristic: in Russian the sentence root is almost
            # always a verb.  If Natasha's morphology tagger says NOUN but the
            # syntax parser marks this token as ROOT, prefer a verbal pymorphy3
            # parse when one exists (catches homonyms like "стекло" NOUN/VERB).
            if pos_ru == "Существительное" and nt.get("deprel", "").upper() == "ROOT" \
                    and MORPH_AVAILABLE:
                for candidate in _morph.parse(word):
                    if _pos_from_parse(candidate) in ("Глагол", "Причастие", "Деепричастие"):
                        pos_ru = _pos_from_parse(candidate)
                        p = candidate
                        break
                else:
                    p = _morph.parse(word)[0] if MORPH_AVAILABLE else None
            # For all other cases find a pymorphy3 parse that matches Natasha's
            # POS so feature extraction uses the correct inflection.
            elif MORPH_AVAILABLE:
                parses = _morph.parse(word)
                p = parses[0]
                for candidate in parses:
                    if _pos_from_parse(candidate) == pos_ru:
                        p = candidate
                        break
            else:
                p = None
        else:
            p = _morph.parse(word)[0] if MORPH_AVAILABLE else None
            pos_ru = _pos_from_parse(p)

        # Lemma
        if nt and nt["lemma"]:
            lemma = nt["lemma"]
        elif p:
            lemma = p.normal_form
        else:
            lemma = word.lower()

        # Features (only for non-introductory levels)
        if level == LEVEL_INTRODUCTORY:
            const = {}
            var   = {}
            syntax_role = ""
        else:
            const = _const_features(pos_ru, p)
            var   = _var_features(pos_ru, p)
            # Syntax role: prefer Natasha deprel, fall back to heuristic
            if nt and nt["deprel"]:
                syntax_role = DEPREL_MAP.get(nt["deprel"], nt["deprel"])
            else:
                syntax_role = _guess_syntax_role(word, pos_ru, p)

        explanation = _build_explanation(pos_ru, lemma, const, var, syntax_role, level)

        token = TokenAnalysis(
            word=word,
            index=i,
            pos=pos_ru,
            lemma=lemma,
            const_features=const,
            var_features=var,
            syntax_role=syntax_role,
            explanation=explanation,
        )
        results.append(token.to_dict())

    # ── Post-process: ensure at least one predicate ───────────────────────────
    # Russian sentences always have a verb/predicate. If none was detected,
    # find the best candidate with a verbal pymorphy3 parse and switch it.
    # This catches homonyms like "стекло" (NOUN=glass / VERB=flowed) that
    # context-free analyzers mis-tag as nouns.
    _verbal = {"Глагол", "Причастие", "Деепричастие"}
    if MORPH_AVAILABLE and results and not any(r["pos"] in _verbal for r in results):
        best_i, best_p, best_pos = None, None, None
        for i, r in enumerate(results):
            for candidate in _morph.parse(r["word"]):
                cpos = _pos_from_parse(candidate)
                if cpos == "Глагол":
                    best_i, best_p, best_pos = i, candidate, cpos
                    break  # inner loop
                if cpos in _verbal and best_pos is None:
                    best_i, best_p, best_pos = i, candidate, cpos
            if best_pos == "Глагол":
                break  # outer loop — stop at first full verb found
        if best_i is not None:
            r = results[best_i]
            r["pos"]        = best_pos
            r["lemma"]      = best_p.normal_form
            r["const_features"] = _const_features(best_pos, best_p)
            r["var_features"]   = _var_features(best_pos, best_p)
            r["syntax_role"] = "сказуемое"   # predicate — always override
            r["explanation"] = _build_explanation(
                best_pos, r["lemma"], r["const_features"],
                r["var_features"], r["syntax_role"], level)

    return results


# ── Single-word re-analysis with forced POS ───────────────────────────────────
def analyze_word_as_pos(word: str, forced_pos: str) -> dict:
    """Return morphological features for *word* treated as *forced_pos*.

    Searches pymorphy3 parses for one matching forced_pos; falls back to the
    top parse if none match.  Returns a plain dict ready for JSON serialisation.
    """
    if not MORPH_AVAILABLE:
        return {"lemma": word.lower(), "const_features": {}, "var_features": {}, "syntax_role": ""}

    parses = _morph.parse(word)
    chosen = None
    for p in parses:
        if _pos_from_parse(p) == forced_pos:
            chosen = p
            break
    if chosen is None and parses:
        chosen = parses[0]
    if chosen is None:
        return {"lemma": word.lower(), "const_features": {}, "var_features": {}, "syntax_role": ""}

    lemma  = chosen.normal_form
    const  = _const_features(forced_pos, chosen)
    var    = _var_features(forced_pos, chosen)
    syntax = _guess_syntax_role(word, forced_pos, chosen)
    return {"lemma": lemma, "const_features": const, "var_features": var, "syntax_role": syntax}


# ── Backward-compat API ────────────────────────────────────────────────────────
def analyze_sentence(text: str) -> list[dict]:
    """Introductory-level analysis: returns [{word, pos, index}, ...]."""
    return [
        {"word": t["word"], "pos": t["pos"], "index": t["index"]}
        for t in full_analyze(text, level=LEVEL_INTRODUCTORY)
    ]
