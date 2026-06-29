"""Точка входа: проверки, синхронизация памяти, запуск Telegram-бота и веба.

Telegram polling работает в отдельном потоке, веб-интерфейс (FastAPI/uvicorn)
— в главном. Ctrl+C останавливает uvicorn, поток бота — daemon и умирает вместе
с процессом.
"""
import logging
import sys
import threading

import uvicorn

import config
from bot.telegram_bot import run_bot_in_thread
from calendar_client import load_calendar
from llm.ollama_client import LLMClient, OllamaClient, gemini_embed
from memory.chroma import ChromaIndex
from memory.facts import FactExtractor
from memory.manager import MemoryManager
from bills import BillStore
from contacts import ContactStore
from inbox import InboxStore
from logger import ActionLog
from reads import ReadStore
from recurring import RecurringTaskStore
from memory.obsidian import ObsidianVault
from tasks import TaskStore
from web.server import create_app

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
            "Он нужен для семантического поиска по памяти (эмбеддинги).\n"
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
    facts = FactExtractor(llm, memory)
    tasks_store = TaskStore(config.TASKS_DB_PATH)
    bills_store = BillStore(config.BILLS_DB_PATH)
    action_log = ActionLog(config.ACTION_LOG_DB_PATH)
    inbox_store = InboxStore(config.INBOX_DB_PATH)
    contacts_store = ContactStore(config.CONTACTS_DB_PATH)
    reads_store = ReadStore(config.READS_DB_PATH)
    recurring_store = RecurringTaskStore(config.RECURRING_DB_PATH)
    # Календарь опционален: None, если нет credentials.json/token.json
    calendar = load_calendar()
    logger.info("Google Calendar: %s", "подключён" if calendar else "не настроен (token.json нет)")

    logger.info("Синхронизация памяти с %s ...", config.OBSIDIAN_VAULT_PATH)
    changed = memory.sync()
    logger.info("Готово: переиндексировано файлов — %d", changed)

    logger.info(
        "Запуск Telegram-бота (провайдер: %s, модель: %s)", llm.provider, llm.model
    )
    bot_thread = threading.Thread(
        target=run_bot_in_thread,
        args=(config.TELEGRAM_BOT_TOKEN, memory, llm, facts, bills_store, tasks_store, calendar, action_log, inbox_store, contacts_store, reads_store, recurring_store),
        daemon=True,
        name="telegram-polling",
    )
    bot_thread.start()

    logger.info(
        "Веб-интерфейс: http://%s:%d", config.WEB_HOST, config.WEB_PORT
    )
    uvicorn.run(
        create_app(memory, llm, facts, tasks_store, bills_store, calendar,
                   inbox_store, contacts_store, reads_store),
        host=config.WEB_HOST,
        port=config.WEB_PORT,
        log_level="warning",
    )


if __name__ == "__main__":
    main()
