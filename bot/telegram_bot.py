"""Telegram polling бот."""
import asyncio
import logging
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

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
    Handlers,
    bills_markup,
    format_bills,
)
from calendar_client import events_to_remind
from llm.ollama_client import LLMClient
from memory.facts import FactExtractor
from memory.manager import MemoryManager
from tasks import TaskStore

logger = logging.getLogger(__name__)

# Время ежедневной проверки платежей (по времени сервера/UTC).
BILLS_REMINDER_TIME = time(hour=9, minute=0)


async def remind_events(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Периодически: напомнить о встречах, начинающихся в ближайшие N минут.

    Уже разосланные напоминания хранятся в job.data['reminded'], чтобы не
    дублировать между запусками (множество сбрасывается на рестарте бота)."""
    data = context.job.data
    calendar = data["calendar"]
    reminded: set = data["reminded"]
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
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"🔔 Через {minutes} мин встреча: «{ev['title']}» в {ev['start'].strftime('%H:%M')}",
        )


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


def build_application(
    token: str,
    memory: MemoryManager,
    llm: LLMClient,
    facts: FactExtractor,
    bills: BillStore,
    tasks: TaskStore,
    calendar=None,
    action_log=None,
) -> Application:
    handlers = Handlers(memory, llm, facts, bills, tasks, calendar, action_log)
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", handlers.start))
    app.add_handler(CommandHandler("plan", handlers.plan))
    app.add_handler(CommandHandler("bills", handlers.bills_cmd))
    app.add_handler(CommandHandler("memory", handlers.show_memory))
    app.add_handler(CommandHandler("forget", handlers.forget))
    app.add_handler(CallbackQueryHandler(handlers.mark_paid, pattern=f"^{BILL_PAID_PREFIX}"))
    app.add_handler(CallbackQueryHandler(handlers.confirm_intent, pattern=r"^intent_(yes|no)$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.handle_text))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handlers.handle_voice))

    # Ежедневная проверка платежей на завтра. JobQueue требует extra
    # python-telegram-bot[job-queue] (APScheduler) — без него job_queue=None.
    if app.job_queue is not None:
        app.job_queue.run_daily(
            remind_bills, time=BILLS_REMINDER_TIME, data=bills, name="remind_bills"
        )
        # Напоминания о встречах — только если календарь настроен
        if calendar is not None:
            app.job_queue.run_repeating(
                remind_events,
                interval=config.CALENDAR_REMINDER_INTERVAL,
                first=10,
                data={"calendar": calendar, "reminded": set()},
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
) -> None:
    """Запускает бота в режиме polling (блокирующий вызов, главный поток)."""
    build_application(token, memory, llm, facts, bills, tasks, calendar, action_log).run_polling(
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
) -> None:
    """Polling в отдельном потоке: свой event loop, без обработчиков сигналов
    (их можно ставить только в главном потоке)."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    build_application(token, memory, llm, facts, bills, tasks, calendar, action_log).run_polling(
        drop_pending_updates=True, stop_signals=None
    )
