"""Тесты голосового пайплайна: конвертация ffmpeg и разбор транскрипции.

Сетевые вызовы Gemini здесь не дёргаем — проверяем чистую логику.
Живой end-to-end (ogg → Gemini → intent) гоняется отдельным скриптом вручную.
"""
import shutil
import subprocess

import pytest

import voice


def _make_ogg_opus(text_tone_seconds: float = 1.0) -> bytes:
    """Генерируем настоящий OGG/Opus (как присылает Telegram) через ffmpeg-синус."""
    proc = subprocess.run(
        [
            "ffmpeg", "-f", "lavfi", "-i", f"sine=frequency=440:duration={text_tone_seconds}",
            "-c:a", "libopus", "-f", "ogg", "pipe:1",
        ],
        capture_output=True,
        check=True,
    )
    return proc.stdout


def _make_silent_ogg(seconds: float = 1.0) -> bytes:
    """Чистая цифровая тишина в OGG/Opus."""
    proc = subprocess.run(
        ["ffmpeg", "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono", "-t", str(seconds),
         "-c:a", "libopus", "-f", "ogg", "pipe:1"],
        capture_output=True,
        check=True,
    )
    return proc.stdout


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="нужен ffmpeg")
def test_convert_ogg_to_wav_produces_valid_wav():
    ogg = _make_ogg_opus()
    wav = voice.convert_ogg_to_wav(ogg)
    # WAV-контейнер: RIFF....WAVE в заголовке
    assert wav[:4] == b"RIFF"
    assert wav[8:12] == b"WAVE"
    assert len(wav) > 44  # больше, чем пустой заголовок


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="нужен ffmpeg")
def test_convert_ogg_to_wav_rejects_garbage():
    with pytest.raises(voice.VoiceError):
        voice.convert_ogg_to_wav(b"not an audio file at all")


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="нужен ffmpeg")
def test_is_silent_true_for_silence():
    assert voice._is_silent(_make_silent_ogg()) is True


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="нужен ffmpeg")
def test_is_silent_false_for_tone():
    assert voice._is_silent(_make_ogg_opus()) is False


def test_interpret_returns_text_for_clear_transcription():
    assert voice._interpret_transcription("купи молоко") == "купи молоко"


def test_interpret_returns_none_when_marker_embedded():
    # Модель может вернуть маркер внутри хлама, а не ровно им — всё равно «не разобрал»
    assert voice._interpret_transcription(f"тишина... {voice.INAUDIBLE}") is None


def test_interpret_returns_none_on_prompt_echo():
    # Реальный сбой: на тишине Gemini эхом вернул кусок нашего промпта вместо речи
    echo = "Тишина в начале. Транскрибируй это голосовое сообщение. Верни ТОЛЬКО"
    assert voice._interpret_transcription(echo) is None


def test_interpret_strips_whitespace():
    assert voice._interpret_transcription("  привет  \n") == "привет"


def test_interpret_returns_none_on_inaudible_marker():
    assert voice._interpret_transcription(voice.INAUDIBLE) is None
    assert voice._interpret_transcription(f"  {voice.INAUDIBLE}  ") is None


def test_interpret_returns_none_on_empty():
    assert voice._interpret_transcription("") is None
    assert voice._interpret_transcription("   ") is None
    assert voice._interpret_transcription(None) is None
