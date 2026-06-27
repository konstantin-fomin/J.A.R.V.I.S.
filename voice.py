"""Голосовые сообщения: OGG/Opus (Telegram) → WAV (ffmpeg) → текст (Gemini).

Telegram присылает voice как OGG с кодеком Opus. Gemini API формально принимает
контейнер OGG, но на практике это OGG Vorbis — на OGG/Opus были отказы. Поэтому
перед отправкой конвертируем в WAV через ffmpeg (см. JARVIS_SPEC.md §5).

Транскрипция всегда идёт через Gemini напрямую (как и эмбеддинги памяти),
независимо от LLM_PROVIDER. Готовый текст возвращается наружу и скармливается
в обычный parse_intent — никакой отдельной логики намерений тут нет.

Модуль не зависит от Telegram — его легко тестировать отдельно.
"""
import logging
import subprocess

from google import genai
from google.genai import types

import config

logger = logging.getLogger(__name__)

# Маркер, который модель возвращает, если речь неразборчива/это не речь/тишина.
INAUDIBLE = "[INAUDIBLE]"

TRANSCRIBE_PROMPT = (
    "Транскрибируй это голосовое сообщение. Верни ТОЛЬКО распознанный текст "
    "дословно, без кавычек, пояснений и markdown. "
    f"Если речь неразборчива, это не речь или тишина — верни ровно {INAUDIBLE}."
)

# Тише этого порога (dBFS) считаем аудио тишиной и не гоняем в Gemini.
# Чистая цифровая тишина у ffmpeg ≈ -91 dB, обычная речь ≈ -20…-40 dB.
SILENCE_THRESHOLD_DB = -55.0

# Отрывок промпта: если модель «эхает» инструкцию вместо речи (бывает на тишине),
# считаем транскрипцию несостоявшейся.
_PROMPT_ECHO_MARKER = "Транскрибируй это голосовое сообщение"


class VoiceError(Exception):
    """Не удалось обработать голосовое (конвертация или транскрипция)."""


def convert_ogg_to_wav(ogg_bytes: bytes) -> bytes:
    """OGG/Opus → WAV (моно, 16 кГц) через ffmpeg. Без временных файлов: pipe→pipe."""
    try:
        proc = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error",
             "-i", "pipe:0", "-ar", "16000", "-ac", "1", "-f", "wav", "pipe:1"],
            input=ogg_bytes,
            capture_output=True,
            check=True,
        )
    except FileNotFoundError as e:
        raise VoiceError("ffmpeg не установлен") from e
    except subprocess.CalledProcessError as e:
        stderr = e.stderr.decode("utf-8", "replace").strip()
        raise VoiceError(f"ffmpeg не смог конвертировать аудио: {stderr}") from e
    if not proc.stdout:
        raise VoiceError("ffmpeg вернул пустой WAV")
    return proc.stdout


def _mean_volume_db(audio_bytes: bytes) -> float:
    """Средняя громкость аудио (dBFS) по ffmpeg volumedetect. -inf если измерить нельзя."""
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-i", "pipe:0", "-af", "volumedetect", "-f", "null", "-"],
        input=audio_bytes,
        capture_output=True,
    )
    for line in proc.stderr.decode("utf-8", "replace").splitlines():
        if "mean_volume:" in line:
            try:
                return float(line.split("mean_volume:")[1].split("dB")[0].strip())
            except ValueError:
                break
    return float("-inf")


def _is_silent(audio_bytes: bytes) -> bool:
    """Аудио практически беззвучно (тишина/пустая запись) — транскрибировать нечего."""
    return _mean_volume_db(audio_bytes) < SILENCE_THRESHOLD_DB


def _interpret_transcription(raw: str | None) -> str | None:
    """Сырой ответ модели → текст, либо None если разобрать не удалось.

    None означает «не разобрал, проси повторить текстом»: пустой ответ, маркер
    INAUDIBLE (в т.ч. внутри мусора) или эхо нашего же промпта вместо речи."""
    text = (raw or "").strip()
    if not text or INAUDIBLE in text or _PROMPT_ECHO_MARKER in text:
        return None
    return text


def _gemini_transcribe(wav_bytes: bytes) -> str:
    """Сырой вызов Gemini: WAV → ответ модели (текст или INAUDIBLE)."""
    client = genai.Client(api_key=config.GEMINI_API_KEY)
    response = client.models.generate_content(
        model=config.GEMINI_MODEL,
        contents=[
            types.Part.from_bytes(data=wav_bytes, mime_type="audio/wav"),
            TRANSCRIBE_PROMPT,
        ],
    )
    return response.text or ""


def transcribe_voice(ogg_bytes: bytes) -> str | None:
    """Полный путь: OGG/Opus → WAV → Gemini → текст.

    Возвращает распознанный текст или None, если речь неразборчива/непонятна
    (тогда бот попросит повторить текстом). VoiceError — на сбое конвертации/API.
    """
    wav_bytes = convert_ogg_to_wav(ogg_bytes)
    # Тишину/пустую запись отсекаем заранее: Gemini на ней склонен галлюцинировать
    # (эхать промпт), а не возвращать INAUDIBLE.
    if _is_silent(wav_bytes):
        return None
    try:
        raw = _gemini_transcribe(wav_bytes)
    except Exception as e:
        logger.exception("Gemini не смог транскрибировать голосовое")
        raise VoiceError("ошибка транскрипции Gemini") from e
    return _interpret_transcription(raw)
