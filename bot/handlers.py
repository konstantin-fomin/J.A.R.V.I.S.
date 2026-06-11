"""Обработчики команд и сообщений Telegram-бота."""
import logging
from datetime import date

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import ContextTypes

import config
from llm.ollama_client import OllamaClient
from memory.manager import MemoryManager

logger = logging.getLogger(__name__)

TELEGRAM_MAX_LEN = 4096

START_TEXT = """Привет! Я твой личный ассистент с памятью.

Просто пиши мне — я отвечаю с учётом всего, что знаю о тебе.
Всё общение сохраняется в журнал в Obsidian.

Команды:
/memory — что я о тебе помню (список файлов)
/forget <тема> — удалить файл памяти
📓 в начале сообщения — записать в дневник без ответа"""


def _split_message(text: str) -> list[str]:
    """Telegram не принимает сообщения длиннее 4096 символов."""
    if len(text) <= TELEGRAM_MAX_LEN:
        return [text]
    parts = []
    while text:
        parts.append(text[:TELEGRAM_MAX_LEN])
        text = text[TELEGRAM_MAX_LEN:]
    return parts


def _allowed(update: Update) -> bool:
    if config.ALLOWED_USER_ID is None:
        return True
    return update.effective_user is not None and update.effective_user.id == config.ALLOWED_USER_ID


class Handlers:
    def __init__(self, memory: MemoryManager, llm: OllamaClient):
        self.memory = memory
        self.llm = llm

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not _allowed(update):
            return
        await update.message.reply_text(START_TEXT)

    async def show_memory(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not _allowed(update):
            return
        files = self.memory.list_files()
        if not files:
            await update.message.reply_text("Память пока пуста.")
            return
        listing = "\n".join(f"• {f}" for f in files)
        for part in _split_message(f"Файлы памяти:\n{listing}"):
            await update.message.reply_text(part)

    async def forget(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not _allowed(update):
            return
        topic = " ".join(context.args) if context.args else ""
        if not topic:
            await update.message.reply_text(
                "Укажи тему: /forget работа\nСписок тем — в /memory"
            )
            return
        deleted = self.memory.forget(topic)
        if deleted:
            await update.message.reply_text(f"Удалил {deleted} 🗑")
        else:
            await update.message.reply_text(
                f"Не нашёл файл памяти «{topic}». Посмотри список в /memory"
            )

    async def handle_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not _allowed(update):
            return
        text = update.message.text.strip()

        # Режим дневника: записать без ответа модели
        if text.startswith("📓"):
            entry = text.removeprefix("📓").strip()
            self.memory.log_message("дневник", entry)
            await update.message.reply_text("Записал в дневник 📓")
            return

        await update.message.chat.send_action(ChatAction.TYPING)

        history: list[dict] = context.chat_data.setdefault("history", [])
        try:
            memory_context = self.memory.remember(text)
            messages = [
                {
                    "role": "system",
                    "content": config.SYSTEM_PROMPT.format(
                        date=date.today().isoformat(),
                        memory_context=memory_context,
                    ),
                },
                *history,
                {"role": "user", "content": text},
            ]
            answer = self.llm.chat(messages)
        except Exception:
            logger.exception("Ошибка при обработке сообщения")
            await update.message.reply_text(
                "Что-то пошло не так 😔 Проверь, что Ollama запущен, и попробуй ещё раз."
            )
            return

        self.memory.log_message("я", text)
        self.memory.log_message("бот", answer)

        history.append({"role": "user", "content": text})
        history.append({"role": "assistant", "content": answer})
        del history[:-config.MAX_HISTORY_MESSAGES]

        for part in _split_message(answer):
            await update.message.reply_text(part)
