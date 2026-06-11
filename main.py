"""Точка входа: проверяет окружение, синхронизирует память, запускает бота."""
import logging
import sys

import config
from bot.telegram_bot import run_bot
from llm.ollama_client import LLMClient, OllamaClient, gemini_embed
from memory.chroma import ChromaIndex
from memory.manager import MemoryManager
from memory.obsidian import ObsidianVault

logging.basicConfig(
    format="%(asctime)s %(name)s %(levelname)s: %(message)s",
    level=logging.INFO,
)
# httpx логирует каждый запрос к Telegram — слишком шумно
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


def check_environment(ollama_client: OllamaClient, llm: LLMClient) -> None:
    # Эмбеддинги всегда считаются через Gemini — ключ обязателен
    if not config.GEMINI_API_KEY:
        sys.exit(
            "GEMINI_API_KEY не задан в .env.\n"
            "Он нужен для семантического поиска по памяти (text-embedding-004).\n"
            "Задай его даже если LLM_PROVIDER не gemini."
        )
    # Ollama нужен только для локальных ответов
    if config.LLM_PROVIDER == "ollama":
        if not ollama_client.is_available():
            sys.exit(
                f"Ollama недоступен по адресу {config.OLLAMA_BASE_URL}.\n"
                "Запусти Ollama и попробуй снова."
            )
        if not ollama_client.has_model(config.OLLAMA_MODEL):
            sys.exit(
                f"Модель «{config.OLLAMA_MODEL}» не установлена. "
                f"Выполни: ollama pull {config.OLLAMA_MODEL}"
            )
    error = llm.check_config()
    if error:
        sys.exit(error)
    if not config.TELEGRAM_BOT_TOKEN:
        sys.exit("TELEGRAM_BOT_TOKEN не задан. Впиши токен от @BotFather в файл .env")


def main() -> None:
    ollama_client = OllamaClient(config.OLLAMA_BASE_URL, config.OLLAMA_MODEL)
    llm = LLMClient(ollama_client)
    check_environment(ollama_client, llm)

    vault = ObsidianVault(config.OBSIDIAN_VAULT_PATH)
    index = ChromaIndex(config.CHROMA_PERSIST_DIR, gemini_embed)
    memory = MemoryManager(vault, index, config.MAX_MEMORY_RESULTS)

    logger.info("Синхронизация памяти с %s ...", config.OBSIDIAN_VAULT_PATH)
    changed = memory.sync()
    logger.info("Готово: переиндексировано файлов — %d", changed)

    logger.info(
        "Запуск Telegram-бота (провайдер: %s, модель: %s)", llm.provider, llm.model
    )
    run_bot(config.TELEGRAM_BOT_TOKEN, memory, llm)


if __name__ == "__main__":
    main()
