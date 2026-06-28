"""Telegram polling бот."""
import asyncio
import logging
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import config
from bills import BillStore
from bot.handlers import (
    BILL_PAID_PREFIX,
    INBOX_TO_TASK_PREFIX,
    SUGGEST_DISMISS_PREFIX,
    SUGGEST_TASK_PREFIX,
    Handlers,
    bills_markup,
    format_bills,
)
from calendar_client import events_to_remind
from llm.ollama_client import LLMClient
from memory.facts import FactExtractor
from memory.manager import MemoryManager
from suggestions import ProactiveSuggester, SuggestionLog, build_suggestion_text, propose_label
from tasks import TaskStore

logger = logging.getLogger(__name__)

# Время ежедневной проверки платежей (по времени сервера/UTC).
BILLS_REMINDER_TIME = time(hour=9, minute=0)

# Время ежедневной проверки журнала на повторяющиеся темы (§13).
SUGGEST_REMINDER_TIME = time(hour=10, minute=0)

# Максимальная длина короткого фрагмента заметки в pre-meeting bundle.
PREMEETING_SNIPPET_MAX = 120


def _snippet(text: str) -> str:
    """Однострочный короткий фрагмент заметки для перечисления под напоминанием."""
    flat = " ".join(text.split())
    if len(flat) <= PREMEETING_SNIPPET_MAX:
        return flat
    return flat[: PREMEETING_SNIPPET_MAX - 1].rstrip() + "…"


def build_reminder_text(event: dict, minutes: int, memory=None,
                        notes_count: int = 3, max_distance: float = 0.6) -> str:
    """Текст напоминания о встрече + (опционально) секция «Из твоих заметок».

    Семантический поиск идёт по тому же MemoryManager, что и обычный chat, по
    названию встречи и описанию (если есть). Релевантные совпадения (top-N в
    пределах порога) добавляются секцией. Нет релевантных — секции нет,
    пустой блок не показываем и нерелевантное не натягиваем."""
    base = (f"🔔 Через {minutes} мин встреча: «{event['title']}» "
            f"в {event['start'].strftime('%H:%M')}")
    if memory is None:
        return base
    query = event["title"]
    if event.get("description"):
        query += " " + event["description"]
    notes = memory.relevant_notes(query, notes_count, max_distance)
    if not notes:
        return base
    lines = [base, "", "📝 Из твоих заметок:"]
    lines += [f"• {_snippet(text)}" for text, _file in notes]
    return "\n".join(lines)


async def remind_events(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Периодически: напомнить о встречах, начинающихся в ближайшие N минут.

    Уже разосланные напоминания хранятся в job.data['reminded'], чтобы не
    дублировать между запусками (множество сбрасывается на рестарте бота)."""
    data = context.job.data
    calendar = data["calendar"]
    reminded: set = data["reminded"]
    memory = data.get("memory")
    chat_id = config.ALLOWED_USER_ID
    if chat_id is None:
        return
    tz = ZoneInfo(config.CALENDAR_TIMEZONE)
    now = datetime.now(tz)
    horizon = now + timedelta(minutes=config.CALENDAR_REMINDER_LEAD_MINUTES)
    try:
        events = calendar.list_events(now, horizon)
    except Exception:
        logger.exception("Не удалось получить события для напоминания")
        return
    for ev in events_to_remind(events, now, horizon, reminded):
        reminded.add(ev["id"])
        minutes = max(0, round((ev["start"] - now).total_seconds() / 60))
        try:
            text = build_reminder_text(
                ev, minutes, memory,
                notes_count=config.PREMEETING_NOTES_COUNT,
                max_distance=config.MEMORY_RELEVANCE_MAX_DISTANCE,
            )
        except Exception:
            # поиск по памяти не должен ломать само напоминание
            logger.exception("Не удалось собрать заметки к встрече — шлю без них")
            text = (f"🔔 Через {minutes} мин встреча: «{ev['title']}» "
                    f"в {ev['start'].strftime('%H:%M')}")
        await context.bot.send_message(chat_id=chat_id, text=text)


async def remind_bills(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Раз в день: если на завтра есть неоплаченные начисления — напомнить."""
    bills: BillStore = context.job.data
    chat_id = config.ALLOWED_USER_ID
    if chat_id is None:
        logger.warning("ALLOWED_USER_ID не задан — некому слать напоминание о платежах")
        return
    tomorrow = date.today() + timedelta(days=1)
    # tomorrow может быть уже в новом месяце — создаём начисления заранее
    bills.ensure_month(tomorrow.strftime("%Y-%m"))
    due = bills.due_on(tomorrow.isoformat(), status="pending")
    if not due:
        return
    text = format_bills(due, f"🔔 Завтра ({tomorrow.isoformat()}) платежи:")
    await context.bot.send_message(
        chat_id=chat_id, text=text, reply_markup=bills_markup(due)
    )


async def suggest_from_notes(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Раз в день: ищет в журнале повторяющиеся темы и предлагает превратить их
    в задачу (§13). Для каждой темы — отдельное сообщение с кнопками Да/Нет.

    mark_suggested зовём СРАЗУ после успешной отправки (не после ответа
    пользователя): иначе краш/рестарт бота между показом и ответом мог бы
    предложить ту же тему повторно. find_suggestions блокирующий (читает индекс
    и дёргает LLM для формулировки) — уводим в поток."""
    suggester: ProactiveSuggester = context.job.data
    chat_id = config.ALLOWED_USER_ID
    if chat_id is None:
        logger.warning("ALLOWED_USER_ID не задан — некому слать подсказки из заметок")
        return
    try:
        suggestions = await asyncio.to_thread(suggester.find_suggestions)
    except Exception:
        logger.exception("Не удалось собрать проактивные подсказки")
        return
    for s in suggestions:
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Да", callback_data=f"{SUGGEST_TASK_PREFIX}{s['hash']}"),
            InlineKeyboardButton("❌ Нет", callback_data=f"{SUGGEST_DISMISS_PREFIX}{s['hash']}"),
        ]])
        try:
            await context.bot.send_message(
                chat_id=chat_id, text=build_suggestion_text(s["label"]), reply_markup=keyboard
            )
        except Exception:
            logger.exception("Не удалось отправить подсказку «%s»", s["label"])
            continue
        # Отправили — фиксируем показ немедленно (защита от дублей при рестарте).
        suggester.log.mark_suggested(s["hash"], s["label"])


def build_application(
    token: str,
    memory: MemoryManager,
    llm: LLMClient,
    facts: FactExtractor,
    bills: BillStore,
    tasks: TaskStore,
    calendar=None,
    action_log=None,
    inbox=None,
) -> Application:
    # Проактивные подсказки (§13): лог общий для job (показ/пометка) и хендлеров
    # (кнопка «Да» достаёт формулировку темы по hash). Тема формулируется LLM.
    suggest_log = SuggestionLog(config.SUGGESTIONS_DB_PATH)
    suggester = ProactiveSuggester(
        memory.index,
        label_fn=lambda texts: propose_label(llm, texts),
        log=suggest_log,
        window_days=config.SUGGEST_WINDOW_DAYS,
        max_distance=config.SUGGEST_MAX_DISTANCE,
        min_cluster=config.SUGGEST_MIN_CLUSTER,
        repeat_block_days=config.SUGGEST_REPEAT_BLOCK_DAYS,
    )
    handlers = Handlers(memory, llm, facts, bills, tasks, calendar, action_log, inbox, suggest_log)
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", handlers.start))
    app.add_handler(CommandHandler("plan", handlers.plan))
    app.add_handler(CommandHandler("bills", handlers.bills_cmd))
    app.add_handler(CommandHandler("memory", handlers.show_memory))
    app.add_handler(CommandHandler("forget", handlers.forget))
    app.add_handler(CommandHandler("inbox", handlers.inbox_cmd))
    app.add_handler(CallbackQueryHandler(handlers.mark_paid, pattern=f"^{BILL_PAID_PREFIX}"))
    app.add_handler(CallbackQueryHandler(handlers.inbox_to_task, pattern=f"^{INBOX_TO_TASK_PREFIX}"))
    app.add_handler(CallbackQueryHandler(handlers.confirm_intent, pattern=r"^intent_(yes|no)$"))
    app.add_handler(CallbackQueryHandler(handlers.suggest_to_task, pattern=f"^{SUGGEST_TASK_PREFIX}"))
    app.add_handler(CallbackQueryHandler(handlers.suggest_dismiss, pattern=f"^{SUGGEST_DISMISS_PREFIX}"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.handle_text))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handlers.handle_voice))

    # Ежедневная проверка платежей на завтра. JobQueue требует extra
    # python-telegram-bot[job-queue] (APScheduler) — без него job_queue=None.
    if app.job_queue is not None:
        app.job_queue.run_daily(
            remind_bills, time=BILLS_REMINDER_TIME, data=bills, name="remind_bills"
        )
        # Ежедневный разбор журнала на повторяющиеся темы (проактивные подсказки)
        app.job_queue.run_daily(
            suggest_from_notes, time=SUGGEST_REMINDER_TIME, data=suggester,
            name="suggest_from_notes",
        )
        # Напоминания о встречах — только если календарь настроен
        if calendar is not None:
            app.job_queue.run_repeating(
                remind_events,
                interval=config.CALENDAR_REMINDER_INTERVAL,
                first=10,
                data={"calendar": calendar, "reminded": set(), "memory": memory},
                name="remind_events",
            )
    else:
        logger.warning(
            "JobQueue недоступен (нет APScheduler) — напоминания отключены. "
            "Установи python-telegram-bot[job-queue]."
        )
    return app


def run_bot(
    token: str,
    memory: MemoryManager,
    llm: LLMClient,
    facts: FactExtractor,
    bills: BillStore,
    tasks: TaskStore,
    calendar=None,
    action_log=None,
    inbox=None,
) -> None:
    """Запускает бота в режиме polling (блокирующий вызов, главный поток)."""
    build_application(token, memory, llm, facts, bills, tasks, calendar, action_log, inbox).run_polling(
        drop_pending_updates=True
    )


def run_bot_in_thread(
    token: str,
    memory: MemoryManager,
    llm: LLMClient,
    facts: FactExtractor,
    bills: BillStore,
    tasks: TaskStore,
    calendar=None,
    action_log=None,
    inbox=None,
) -> None:
    """Polling в отдельном потоке: свой event loop, без обработчиков сигналов
    (их можно ставить только в главном потоке)."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    build_application(token, memory, llm, facts, bills, tasks, calendar, action_log, inbox).run_polling(
        drop_pending_updates=True, stop_signals=None
    )
