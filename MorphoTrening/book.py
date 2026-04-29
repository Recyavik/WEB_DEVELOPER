import os
import random
import re

BOOK_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'instance', 'book.txt')

_cache: list[str] = []
_cache_mtime: float = 0.0


def book_exists() -> bool:
    return os.path.exists(BOOK_PATH)


def _load_sentences() -> list[str]:
    global _cache, _cache_mtime
    if not book_exists():
        return []
    mtime = os.path.getmtime(BOOK_PATH)
    if _cache and mtime == _cache_mtime:
        return _cache
    with open(BOOK_PATH, 'r', encoding='utf-8', errors='ignore') as f:
        text = f.read()
    _cache = _extract_sentences(text)
    _cache_mtime = mtime
    return _cache


def book_info() -> dict | None:
    if not book_exists():
        return None
    sentences = _load_sentences()
    return {
        'size_kb': round(os.path.getsize(BOOK_PATH) / 1024, 1),
        'sentence_count': len(sentences),
    }


_QUESTION_RE = re.compile(
    r'^(кто|что|где|когда|как|какой|какая|какое|какие|который|чей|'
    r'почему|зачем|откуда|куда|сколько|разве|неужели)\b',
    re.IGNORECASE,
)


def _is_question(sentence: str) -> bool:
    if '?' in sentence:
        return True
    # Вопрос без знака из-за форматирования в источнике
    if _QUESTION_RE.match(sentence):
        return True
    return False


def _is_direct_speech(sentence: str) -> bool:
    # Russian guillemets or typographic quotes
    if re.search(r'[«»""„"]', sentence):
        return True
    # Starts with em/en dash → dialogue line
    if re.match(r'^[—–]', sentence):
        return True
    # Em dash following punctuation → speech attribution
    if re.search(r'[.!?,]\s*—\s+[а-яёА-ЯЁ]', sentence):
        return True
    # Colon introducing direct speech: «Он сказал: Иди домой.»
    if re.search(r':\s+[«"А-ЯЁ]', sentence):
        return True
    return False


def _extract_sentences(text: str) -> list[str]:
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r'\n{2,}', '\n', text)
    parts = re.split(r'(?<=[.!?…])\s+', text)
    result = []
    for p in parts:
        p = p.strip()
        # Проверяем до зачистки краёв, пока улики ещё на месте
        if _is_direct_speech(p):
            continue
        if _is_question(p):
            continue
        p = p.strip('"«»—–-').strip()
        if len(p) < 10:
            continue
        if not re.search(r'[а-яёА-ЯЁ]', p):
            continue
        result.append(p)
    return result


def _count_ru_words(sentence: str) -> int:
    return len(re.findall(r'\b[а-яёА-ЯЁ]{2,}\b', sentence))


def get_random_sentence(min_words: int = 5, max_words: int = 20) -> str | None:
    sentences = _load_sentences()
    filtered = [s for s in sentences if min_words <= _count_ru_words(s) <= max_words]
    if not filtered:
        return None
    return random.choice(filtered)


def save_book_from_upload(file_obj, filename: str) -> int:
    os.makedirs(os.path.dirname(BOOK_PATH), exist_ok=True)

    if filename.lower().endswith('.pdf'):
        try:
            from pdfminer.high_level import extract_text
            import io
            raw = file_obj.read()
            text = extract_text(io.BytesIO(raw))
        except ImportError:
            raise RuntimeError('pdfminer.six не установлен. Загрузите .txt файл.')
    else:
        raw = file_obj.read()
        try:
            text = raw.decode('utf-8')
        except UnicodeDecodeError:
            text = raw.decode('cp1251', errors='ignore')

    with open(BOOK_PATH, 'w', encoding='utf-8') as f:
        f.write(text)

    global _cache, _cache_mtime
    _cache = _extract_sentences(text)
    _cache_mtime = os.path.getmtime(BOOK_PATH)
    return len(_cache)


def delete_book() -> None:
    global _cache, _cache_mtime
    if book_exists():
        os.remove(BOOK_PATH)
    _cache = []
    _cache_mtime = 0.0
