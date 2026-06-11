"""Telegram polling бот."""
from telegram.ext import Application, CommandHandler, MessageHandler, filters

from bot.handlers import Handlers
from llm.ollama_client import OllamaClient
from memory.manager import MemoryManager


def run_bot(token: str, memory: MemoryManager, llm: OllamaClient) -> None:
    """Запускает бота в режиме polling (блокирующий вызов)."""
    handlers = Handlers(memory, llm)
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", handlers.start))
    app.add_handler(CommandHandler("memory", handlers.show_memory))
    app.add_handler(CommandHandler("forget", handlers.forget))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.handle_text))
    app.run_polling(drop_pending_updates=True)
