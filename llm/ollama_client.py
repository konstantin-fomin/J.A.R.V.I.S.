"""Единый интерфейс к LLM-провайдерам + клиент Ollama.

Провайдер ответов выбирается через LLM_PROVIDER в .env:
ollama | groq | gemini | openrouter | openai | anthropic.

Groq, Gemini, OpenRouter и OpenAI ходят через OpenAI-совместимый API
(один SDK, разные base_url). Anthropic — через официальный SDK.
Эмбеддинги для памяти всегда считаются через Gemini API (text-embedding-004),
независимо от выбранного провайдера ответов.
"""
import anthropic
import google.generativeai as _genai
import ollama
from openai import OpenAI

import config


def gemini_embed(texts: list[str]) -> list[list[float]]:
    """Эмбеддинги через Gemini text-embedding-004 (бесплатно, мультиязычно)."""
    if not texts:
        return []
    _genai.configure(api_key=config.GEMINI_API_KEY)
    result = _genai.embed_content(
        model="models/text-embedding-004",
        content=texts,
    )
    return result["embedding"]


class OllamaClient:
    def __init__(self, base_url: str, model: str):
        self.model = model
        self._client = ollama.Client(host=base_url)

    def is_available(self) -> bool:
        """Проверка что сервер Ollama запущен."""
        try:
            self._client.list()
            return True
        except Exception:
            return False

    def installed_models(self) -> list[str]:
        return [m.model for m in self._client.list().models]

    def has_model(self, name: str) -> bool:
        """Модель считается установленной и без явного тега (qwen2.5:7b == qwen2.5:7b:latest)."""
        installed = self.installed_models()
        return any(m == name or m.split(":latest")[0] == name for m in installed)

    def chat(self, messages: list[dict]) -> str:
        """messages: [{"role": "system"|"user"|"assistant", "content": str}, ...]"""
        response = self._client.chat(model=self.model, messages=messages)
        return response.message.content


# OpenAI-совместимые провайдеры: name -> (base_url, api_key, model)
def _openai_compatible_providers() -> dict[str, tuple[str | None, str, str]]:
    return {
        "groq": ("https://api.groq.com/openai/v1", config.GROQ_API_KEY, config.GROQ_MODEL),
        "gemini": (
            "https://generativelanguage.googleapis.com/v1beta/openai/",
            config.GEMINI_API_KEY,
            config.GEMINI_MODEL,
        ),
        "openrouter": (
            "https://openrouter.ai/api/v1",
            config.OPENROUTER_API_KEY,
            config.OPENROUTER_MODEL,
        ),
        "openai": (None, config.OPENAI_API_KEY, config.OPENAI_MODEL),
    }


class LLMClient:
    """Роутер LLM: одна точка входа get_response(messages) для всего бота."""

    def __init__(self, ollama_client: OllamaClient):
        self.provider = config.LLM_PROVIDER
        self._ollama = ollama_client

    @property
    def model(self) -> str:
        if self.provider == "ollama":
            return self._ollama.model
        if self.provider == "anthropic":
            return config.ANTHROPIC_MODEL
        return _openai_compatible_providers()[self.provider][2]

    def check_config(self) -> str | None:
        """Возвращает текст ошибки конфигурации или None, если всё в порядке."""
        compatible = _openai_compatible_providers()
        if self.provider == "ollama":
            return None
        if self.provider == "anthropic":
            if not config.ANTHROPIC_API_KEY:
                return "LLM_PROVIDER=anthropic, но ANTHROPIC_API_KEY не задан в .env"
            return None
        if self.provider in compatible:
            _, api_key, _ = compatible[self.provider]
            if not api_key:
                return (
                    f"LLM_PROVIDER={self.provider}, "
                    f"но {self.provider.upper()}_API_KEY не задан в .env"
                )
            return None
        known = ", ".join(["ollama", "anthropic", *compatible])
        return f"Неизвестный LLM_PROVIDER «{self.provider}». Допустимые: {known}"

    def get_response(self, messages: list[dict]) -> str:
        """messages: [{"role": "system"|"user"|"assistant", "content": str}, ...]"""
        if self.provider == "ollama":
            return self._ollama.chat(messages)
        if self.provider == "anthropic":
            return self._anthropic_chat(messages)
        return self._openai_compatible_chat(messages)

    # Старое имя метода — чтобы код бота (handlers) не менялся
    chat = get_response

    def _anthropic_chat(self, messages: list[dict]) -> str:
        # Anthropic Messages API принимает системный промпт отдельным
        # параметром, а не сообщением с ролью system
        system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
        chat_messages = [m for m in messages if m["role"] != "system"]
        client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
        kwargs = {"system": system} if system else {}
        response = client.messages.create(
            model=config.ANTHROPIC_MODEL,
            max_tokens=config.LLM_MAX_TOKENS,
            messages=chat_messages,
            **kwargs,
        )
        return "".join(block.text for block in response.content if block.type == "text")

    def _openai_compatible_chat(self, messages: list[dict]) -> str:
        base_url, api_key, model = _openai_compatible_providers()[self.provider]
        client = OpenAI(api_key=api_key, base_url=base_url)
        response = client.chat.completions.create(
            model=model,
            messages=messages,
            max_tokens=config.LLM_MAX_TOKENS,
        )
        return response.choices[0].message.content or ""
