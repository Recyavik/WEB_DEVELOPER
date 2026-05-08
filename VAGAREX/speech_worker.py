"""
speech_worker.py — фоновый поток голосового распознавания (адаптация VEGA)

Слушает микрофон, запускает Whisper, фильтрует по слову-активатору «Вега»,
кладет распознанный текст в очередь command_queue.
"""
import logging
import queue
import threading
import time
from typing import Optional

import numpy as np
import pyaudio
from faster_whisper import WhisperModel

from config import WHISPER_MODEL, LANGUAGE, TRY_CUDA, WAKE_WORD
import nlu

log = logging.getLogger(__name__)

# ── Настройки микрофона ───────────────────────────────────────────────────────

CHANNELS   = 1
CHUNK      = 1024
RATE       = 16_000
FORMAT     = pyaudio.paInt16

START_RMS  = 165
STOP_RMS   = 110
MIN_SEC    = 0.30
MAX_SEC    = 4.0
SILENCE_S  = 0.32
POST_ROLL  = 0.12


def _rms(data: bytes) -> float:
    arr = np.frombuffer(data, dtype=np.int16).astype(np.float32)
    return float(np.sqrt(np.mean(arr ** 2))) if len(arr) else 0.0


class SpeechWorker:
    """Поток захвата аудио и распознавания речи."""

    def __init__(self, command_queue: queue.Queue,
                 stop_event: threading.Event,
                 listen_event: threading.Event,
                 ready_event: threading.Event):
        self.q      = command_queue
        self.stop   = stop_event
        self.listen = listen_event
        self.ready  = ready_event
        self._thread: Optional[threading.Thread] = None

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _load_whisper(self) -> WhisperModel:
        compute = "int8_float16" if TRY_CUDA else "int8"
        device  = "cuda" if TRY_CUDA else "cpu"
        try:
            if TRY_CUDA:
                return WhisperModel(WHISPER_MODEL, device=device, compute_type=compute)
        except Exception:
            pass
        return WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")

    def _run(self):
        log.info("Loading Whisper model '%s'...", WHISPER_MODEL)
        model = self._load_whisper()
        log.info("Whisper ready.")

        pa = pyaudio.PyAudio()
        stream = pa.open(
            format=FORMAT, channels=CHANNELS, rate=RATE,
            input=True, frames_per_buffer=CHUNK,
        )
        self.ready.set()

        try:
            while not self.stop.is_set():
                if not self.listen.is_set():
                    time.sleep(0.05)
                    continue

                chunk = stream.read(CHUNK, exception_on_overflow=False)
                if _rms(chunk) < START_RMS:
                    continue

                # Начинаем запись
                frames = [chunk]
                silence_chunks = 0
                max_chunks     = int(MAX_SEC * RATE / CHUNK)
                sil_thresh     = int(SILENCE_S * RATE / CHUNK)
                post_chunks    = int(POST_ROLL * RATE / CHUNK)

                for _ in range(max_chunks):
                    c = stream.read(CHUNK, exception_on_overflow=False)
                    frames.append(c)
                    if _rms(c) < STOP_RMS:
                        silence_chunks += 1
                    else:
                        silence_chunks = 0
                    if silence_chunks >= sil_thresh:
                        for _ in range(post_chunks):
                            frames.append(stream.read(CHUNK, exception_on_overflow=False))
                        break

                if len(frames) * CHUNK / RATE < MIN_SEC:
                    continue

                audio = np.frombuffer(b"".join(frames), dtype=np.int16).astype(np.float32) / 32768.0
                segs, _ = model.transcribe(audio, language=LANGUAGE, beam_size=2, best_of=2,
                                           without_timestamps=True)
                text = " ".join(s.text.strip() for s in segs).strip()
                if not text:
                    continue

                log.info("Whisper: %r", text)

                if not nlu.has_wake(text):
                    continue

                # Только «Вега» — откликаемся
                words = nlu.norm(text).split()
                wake_only = all(w in nlu.WAKE_VARIANTS for w in words if w)
                if wake_only:
                    self.q.put(("wake_only", text))
                else:
                    self.q.put(("text", text))

        finally:
            stream.stop_stream()
            stream.close()
            pa.terminate()
