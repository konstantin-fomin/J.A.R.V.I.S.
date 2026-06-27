"""Обработчики команд и сообщений Telegram-бота."""
import asyncio
import logging
from datetime import date

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import ContextTypes

import config
from bills import BillStore, current_month
from llm.ollama_client import LLMClient
from memory.facts import FactExtractor
from memory.manager import MemoryManager

logger = logging.getLogger(__name__)

TELEGRAM_MAX_LEN = 4096

START_TEXT = """Привет! Я твой личный ассистент с памятью.

Просто пиши мне — я отвечаю с учётом всего, что знаю о тебе.
Всё общение сохраняется в журнал в Obsidian, а важные факты
я сам раскладываю по темам в память.

Команды:
/plan — план на день с учётом твоих целей и журнала
/bills — платежи текущего месяца со статусами
/memory — что я о тебе помню (список файлов)
/forget <тема> — удалить файл памяти
📓 в начале сообщения — записать в дневник без ответа"""

PLAN_PROMPT = """Ты — личный ассистент. Составь план дня для пользователя.

Сегодня: {date}

Цели пользователя (goals.md):
{goals}

Журнал за последние 3 дня:
{journal}

Ответь структурированно и кратко, на русском:
🎯 Приоритеты на день — 1–3 главных пункта
📋 Задачи — конкретные шаги
💡 Советы — с учётом целей и контекста жизни пользователя"""


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


# Префикс callback_data для кнопки «оплачено»: "bill_paid:<instance_id>"
BILL_PAID_PREFIX = "bill_paid:"


def format_bills(instances: list[dict], header: str) -> str:
    """Список начислений со статусами — для /bills и напоминаний."""
    lines = [header, ""]
    for b in instances:
        mark = "✅" if b["status"] == "paid" else "⏳"
        amount = f" — {b['amount']:.0f}" if b["amount"] is not None else ""
        lines.append(f"{mark} {b['due_date']}  {b['name']}{amount}")
    return "\n".join(lines)


def bills_markup(instances: list[dict]) -> InlineKeyboardMarkup | None:
    """По кнопке «✅ Оплачено» на каждый ещё не оплаченный платёж."""
    rows = [
        [
            InlineKeyboardButton(
                f"✅ Оплачено · {b['name']}",
                callback_data=f"{BILL_PAID_PREFIX}{b['id']}",
            )
        ]
        for b in instances
        if b["status"] != "paid"
    ]
    return InlineKeyboardMarkup(rows) if rows else None


def _markup_without(
    markup: InlineKeyboardMarkup | None, callback_data: str
) -> InlineKeyboardMarkup | None:
    """Та же клавиатура без кнопки с указанным callback_data (после оплаты)."""
    if markup is None:
        return None
    rows = []
    for row in markup.inline_keyboard:
        kept = [b for b in row if b.callback_data != callback_data]
        if kept:
            rows.append(kept)
    return InlineKeyboardMarkup(rows) if rows else None


class Handlers:
    def __init__(
        self,
        memory: MemoryManager,
        llm: LLMClient,
        facts: FactExtractor,
        bills: BillStore,
    ):
        self.memory = memory
        self.llm = llm
        self.facts = facts
        self.bills = bills

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not _allowed(update):
            return
        await update.message.reply_text(START_TEXT)

    async def plan(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not _allowed(update):
            return
        await update.message.chat.send_action(ChatAction.TYPING)
        prompt = PLAN_PROMPT.format(
            date=date.today().isoformat(),
            goals=self.memory.goals() or "(целей пока нет)",
            journal=self.memory.recent_journal(days=3) or "(журнал пуст)",
        )
        try:
            answer = await asyncio.to_thread(
                self.llm.chat, [{"role": "user", "content": prompt}]
            )
        except Exception:
            logger.exception("Ошибка при составлении плана")
            await update.message.reply_text(
                "Не смог составить план 😔 Попробуй ещё раз."
            )
            return
        for part in _split_message(answer):
            await update.message.reply_text(part)

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

    async def bills_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not _allowed(update):
            return
        ym = current_month()
        self.bills.ensure_month(ym)
        instances = self.bills.list_instances(ym)
        if not instances:
            await update.message.reply_text(
                "На этот месяц начислений нет. Шаблоны платежей заводятся на дашборде."
            )
            return
        text = format_bills(instances, f"💳 Платежи за {ym}:")
        # Клавиатуру с кнопками «оплачено» вешаем на последнее сообщение
        parts = _split_message(text)
        markup = bills_markup(instances)
        for i, part in enumerate(parts):
            await update.message.reply_text(
                part, reply_markup=markup if i == len(parts) - 1 else None
            )

    async def mark_paid(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Callback кнопки «✅ Оплачено»: PATCH status=paid для instance_id."""
        query = update.callback_query
        if not _allowed(update):
            await query.answer()
            return
        try:
            instance_id = int(query.data[len(BILL_PAID_PREFIX):])
        except (ValueError, IndexError):
            await query.answer("Не понял кнопку 🤔")
            return

        instance = self.bills.get_instance(instance_id)
        if instance is None:
            await query.answer("Платёж не найден")
        elif instance["status"] == "paid":
            await query.answer("Уже отмечен оплаченным")
        else:
            instance = self.bills.set_status(instance_id, "paid")
            await query.answer(f"✅ {instance['name']} — оплачено")

        # Убираем нажатую кнопку, остальные платежи оставляем доступными
        new_markup = _markup_without(query.message.reply_markup, query.data)
        try:
            await query.edit_message_reply_markup(reply_markup=new_markup)
        except Exception:
            # Telegram кидает «message is not modified», если правка пустая — игнорируем
            logger.debug("Не удалось обновить клавиатуру платежей", exc_info=True)

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
            answer = await asyncio.to_thread(self.llm.chat, messages)
        except Exception:
            logger.exception("Ошибка при обработке сообщения")
            await update.message.reply_text(
                "Что-то пошло не так 😔 Проверь настройки провайдера и попробуй ещё раз."
            )
            return

        self.memory.log_message("я", text)
        self.memory.log_message("бот", answer)

        history.append({"role": "user", "content": text})
        history.append({"role": "assistant", "content": answer})
        del history[:-config.MAX_HISTORY_MESSAGES]

        # Фоновое извлечение фактов — не блокирует ответ пользователю
        context.application.create_task(
            asyncio.to_thread(self.facts.extract_and_save, text, answer)
        )

        for part in _split_message(answer):
            await update.message.reply_text(part)
