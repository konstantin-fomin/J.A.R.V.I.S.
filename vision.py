"""Фото из Telegram → мультимодальный запрос в Gemini → текстовый ответ (§5-bis).

Telegram присылает фото как JPEG. Картинку (плюс подпись пользователя, если есть)
отправляем в Gemini напрямую — так же, как голос в voice.py, и независимо от
LLM_PROVIDER (мультимодальность всегда через Gemini). Если подписи нет — просим
нейтральное описание. Ответ возвращается наружу и уходит пользователю обычным
текстом через chat-пайплайн.

Это намеренно минимальный путь: ни inbox-записи, ни привязки файлов тут нет —
если понадобится, это отдельный шаг. Модуль не зависит от Telegram —
тестируется отдельно (сетевой вызов _gemini_vision проверяем живьём).
"""
import logging

from google import genai
from google.genai import types

import config

logger = logging.getLogger(__name__)

# Что спросить у модели, если пользователь прислал фото без подписи.
DEFAULT_PROMPT = "Опиши, что на этом фото."


class VisionError(Exception):
    """Не удалось обработать фото (запрос к Gemini упал)."""


def _choose_prompt(caption: str | None) -> str:
    """Подпись пользователя как запрос к модели, иначе — нейтральное описание."""
    text = (caption or "").strip()
    return text if text else DEFAULT_PROMPT


def _interpret_vision(raw: str | None) -> str:
    """Сырой ответ модели → текст без обрамления (пустой ответ → пустая строка)."""
    return (raw or "").strip()


def _gemini_vision(image_bytes: bytes, mime_type: str, prompt: str) -> str:
    """Сырой вызов Gemini: картинка + промпт → ответ модели."""
    client = genai.Client(api_key=config.GEMINI_API_KEY)
    response = client.models.generate_content(
        model=config.GEMINI_MODEL,
        contents=[
            types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
            prompt,
        ],
    )
    return response.text or ""


def describe_photo(image_bytes: bytes, caption: str | None = None,
                   mime_type: str = "image/jpeg") -> str:
    """Полный путь: фото (+подпись) → Gemini → текст.

    Возвращает текст ответа модели (пустую строку, если модель ничего не дала).
    VisionError — на сбое запроса к Gemini.
    """
    prompt = _choose_prompt(caption)
    try:
        raw = _gemini_vision(image_bytes, mime_type, prompt)
    except Exception as e:
        logger.exception("Gemini не смог обработать фото")
        raise VisionError("ошибка обработки фото Gemini") from e
    return _interpret_vision(raw)
