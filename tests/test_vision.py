"""Тесты обработки фото: выбор промпта + интерпретация ответа Gemini.

Сам сетевой вызов Gemini (_gemini_vision) юнит-тестами не дёргаем — как в
test_voice.py, его проверяем живьём. Здесь — чистая логика: подпись→промпт,
сырой ответ→текст, и оркестрация describe_photo с инъекцией фейкового вызова.
"""
import pytest

import vision
from vision import (
    DEFAULT_PROMPT,
    VisionError,
    _choose_prompt,
    _interpret_vision,
    describe_photo,
)


# --- _choose_prompt ----------------------------------------------------------

def test_choose_prompt_uses_caption():
    assert _choose_prompt("что на картинке?") == "что на картинке?"


def test_choose_prompt_strips_caption():
    assert _choose_prompt("  опиши фото  ") == "опиши фото"


def test_choose_prompt_default_when_none():
    assert _choose_prompt(None) == DEFAULT_PROMPT


def test_choose_prompt_default_when_blank():
    assert _choose_prompt("   ") == DEFAULT_PROMPT


# --- _interpret_vision -------------------------------------------------------

def test_interpret_strips_whitespace():
    assert _interpret_vision("  на фото резюме \n") == "на фото резюме"


def test_interpret_empty_on_none():
    assert _interpret_vision(None) == ""


# --- describe_photo (оркестрация, фейковый сетевой вызов) ---------------------

def test_describe_photo_returns_model_text(monkeypatch):
    monkeypatch.setattr(vision, "_gemini_vision", lambda b, m, p: "это резюме Ивана")
    assert describe_photo(b"\xff\xd8jpeg", caption="что тут?") == "это резюме Ивана"


def test_describe_photo_passes_caption_as_prompt(monkeypatch):
    seen = {}

    def fake(img, mime, prompt):
        seen["prompt"] = prompt
        seen["mime"] = mime
        return "ответ"

    monkeypatch.setattr(vision, "_gemini_vision", fake)
    describe_photo(b"img", caption="сколько стоит?", mime_type="image/png")
    assert seen["prompt"] == "сколько стоит?"
    assert seen["mime"] == "image/png"


def test_describe_photo_uses_default_prompt_without_caption(monkeypatch):
    seen = {}
    monkeypatch.setattr(vision, "_gemini_vision",
                        lambda img, mime, prompt: seen.setdefault("prompt", prompt) or "x")
    describe_photo(b"img", caption=None)
    assert seen["prompt"] == DEFAULT_PROMPT


def test_describe_photo_raises_visionerror_on_api_failure(monkeypatch):
    def boom(img, mime, prompt):
        raise RuntimeError("Gemini 500")

    monkeypatch.setattr(vision, "_gemini_vision", boom)
    with pytest.raises(VisionError):
        describe_photo(b"img", caption="что?")
