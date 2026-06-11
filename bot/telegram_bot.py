"""Telegram polling бот."""
import asyncio

from telegram.ext import Application, CommandHandler, MessageHandler, filters

from bot.handlers import Handlers
from llm.ollama_client import LLMClient
from memory.facts import FactExtractor
from memory.manager import MemoryManager


def build_application(
    token: str, memory: MemoryManager, llm: LLMClient, facts: FactExtractor
) -> Application:
    handlers = Handlers(memory, llm, facts)
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", handlers.start))
    app.add_handler(CommandHandler("plan", handlers.plan))
    app.add_handler(CommandHandler("memory", handlers.show_memory))
    app.add_handler(CommandHandler("forget", handlers.forget))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.handle_text))
    return app


def run_bot(
    token: str, memory: MemoryManager, llm: LLMClient, facts: FactExtractor
) -> None:
    """Запускает бота в режиме polling (блокирующий вызов, главный поток)."""
    build_application(token, memory, llm, facts).run_polling(drop_pending_updates=True)


def run_bot_in_thread(
    token: str, memory: MemoryManager, llm: LLMClient, facts: FactExtractor
) -> None:
    """Polling в отдельном потоке: свой event loop, без обработчиков сигналов
    (их можно ставить только в главном потоке)."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    build_application(token, memory, llm, facts).run_polling(
        drop_pending_updates=True, stop_signals=None
    )
