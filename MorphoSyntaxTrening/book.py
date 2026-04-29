import os
import random
import re
from pathlib import Path
from config import INSTANCE_DIR

BOOKS_DIR = INSTANCE_DIR / "books"

_cache: dict[int, tuple[float, list[str]]] = {}  # teacher_id → (mtime, sentences)


def book_path(teacher_id: int) -> Path:
    return BOOKS_DIR / f"{teacher_id}.txt"


def book_exists(teacher_id: int) -> bool:
    return book_path(teacher_id).exists()


def book_info(teacher_id: int) -> dict | None:
    if not book_exists(teacher_id):
        return None
    sentences = _load_sentences(teacher_id)
    return {
        "size_kb": round(book_path(teacher_id).stat().st_size / 1024, 1),
        "sentence_count": len(sentences),
    }


def _load_sentences(teacher_id: int) -> list[str]:
    p = book_path(teacher_id)
    if not p.exists():
        return []
    mtime = p.stat().st_mtime
    cached = _cache.get(teacher_id)
    if cached and cached[0] == mtime:
        return cached[1]
    text = p.read_text(encoding="utf-8", errors="ignore")
    sentences = _extract_sentences(text)
    _cache[teacher_id] = (mtime, sentences)
    return sentences


_QUESTION_RE = re.compile(
    r"^(кто|что|где|когда|как|какой|какая|какое|какие|который|чей|"
    r"почему|зачем|откуда|куда|сколько|разве|неужели)\b",
    re.IGNORECASE,
)


def _is_direct_speech(s: str) -> bool:
    if re.search(r'[«»""„"]', s):
        return True
    if re.match(r"^[—–]", s):
        return True
    if re.search(r"[.!?,]\s*—\s+[а-яёА-ЯЁ]", s):
        return True
    if re.search(r':\s+[«А-ЯЁ]', s):
        return True
    return False


def _extract_sentences(text: str) -> list[str]:
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{2,}", "\n", text)
    parts = re.split(r"(?<=[.!?…])\s+", text)
    result = []
    for p in parts:
        p = p.strip()
        if _is_direct_speech(p):
            continue
        if "?" in p or _QUESTION_RE.match(p):
            continue
        p = p.strip('"«»—–-').strip()
        if len(p) < 10 or not re.search(r"[а-яёА-ЯЁ]", p):
            continue
        result.append(p)
    return result


def get_random_sentence(teacher_id: int, min_words: int = 5, max_words: int = 20) -> str | None:
    sentences = _load_sentences(teacher_id)
    filtered = [s for s in sentences
                if min_words <= len(re.findall(r"\b[а-яёА-ЯЁ]{2,}\b", s)) <= max_words]
    return random.choice(filtered) if filtered else None


def save_book(teacher_id: int, file_obj, filename: str) -> int:
    BOOKS_DIR.mkdir(exist_ok=True)
    if filename.lower().endswith(".pdf"):
        try:
            from pdfminer.high_level import extract_text
            import io
            text = extract_text(io.BytesIO(file_obj.read()))
        except ImportError:
            raise RuntimeError("pdfminer.six не установлен. Загрузите .txt файл.")
    else:
        raw = file_obj.read()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("cp1251", errors="ignore")
    p = book_path(teacher_id)
    p.write_text(text, encoding="utf-8")
    sentences = _extract_sentences(text)
    _cache[teacher_id] = (p.stat().st_mtime, sentences)
    return len(sentences)


def delete_book(teacher_id: int) -> None:
    p = book_path(teacher_id)
    if p.exists():
        p.unlink()
    _cache.pop(teacher_id, None)
