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
    READS_DONE_PREFIX,
    SUGGEST_DISMISS_PREFIX,
    SUGGEST_TASK_PREFIX,
    Handlers,
    bills_markup,
    format_bills,
)
from calendar_client import events_to_remind
from contacts import ContactStore, days_until_birthday
from llm.ollama_client import LLMClient
from memory.facts import FactExtractor
from memory.manager import MemoryManager
from reads import ReadStore
from recurring import RecurringTaskStore
from scheduler_utils import quiet_defer
from suggestions import ProactiveSuggester, SuggestionLog, build_suggestion_text, propose_label
from tasks import TaskStore
from weekly_review import compose_summary, compute_week_stats, format_review

logger = logging.getLogger(__name__)

# Время ежедневной проверки платежей (по времени сервера/UTC).
BILLS_REMINDER_TIME = time(hour=9, minute=0)

# Время ежедневной проверки журнала на повторяющиеся темы (§13).
SUGGEST_REMINDER_TIME = time(hour=10, minute=0)

# Время ежедневной проверки ближайших дней рождения (§14).
BIRTHDAY_REMINDER_TIME = time(hour=9, minute=30)

# Время еженедельного дайджеста «почитать» (§15). Сам интервал — раз в неделю.
READS_DIGEST_TIME = time(hour=11, minute=0)

# Еженедельная сводка (§16): воскресенье вечером. Отдельное от reads_digest время,
# чтобы не прислать два сообщения подряд.
WEEKLY_REVIEW_TIME = time(hour=19, minute=0)

# Повторяющиеся задачи (§18.2): ранний утренний прогон генерации инстансов на
# сегодня и чуть позже — очистка старой выполненной истории. Оба job'а ничего не
# шлют в Telegram, поэтому тихих часов не касаются.
RECURRING_GENERATE_TIME = time(hour=0, minute=5)
RECURRING_CLEANUP_TIME = time(hour=4, minute=0)
# Сколько дней хранить выполненные recurring-инстансы перед очисткой.
RECURRING_KEEP_DAYS = 30

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
    if quiet_defer(context, remind_events):
        return
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
    if quiet_defer(context, remind_bills):
        return
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


async def birthday_reminder(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Раз в день: если у кого-то из контактов ДР в ближайшие N дней — напомнить (§14)."""
    if quiet_defer(context, birthday_reminder):
        return
    contacts: ContactStore = context.job.data
    chat_id = config.ALLOWED_USER_ID
    if chat_id is None:
        logger.warning("ALLOWED_USER_ID не задан — некому слать напоминание о ДР")
        return
    today = date.today()
    upcoming = contacts.upcoming_birthdays(config.BIRTHDAY_REMINDER_LEAD_DAYS, today)
    if not upcoming:
        return
    lines = ["🎂 Скоро дни рождения:", ""]
    for c in upcoming:
        days = days_until_birthday(date.fromisoformat(c["birthday"]), today)
        when = "сегодня" if days == 0 else ("завтра" if days == 1 else f"через {days} дн")
        lines.append(f"• {c['name']} — {when}")
    await context.bot.send_message(chat_id=chat_id, text="\n".join(lines))


async def reads_digest(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Раз в неделю: дайджест непрочитанных ссылок (§15). Саммари уже лежит в БД
    (посчитано при сохранении) — здесь только показываем. У каждой записи кнопка
    «✓ Прочитано». Пусто — молчим."""
    if quiet_defer(context, reads_digest):
        return
    reads: ReadStore = context.job.data
    chat_id = config.ALLOWED_USER_ID
    if chat_id is None:
        logger.warning("ALLOWED_USER_ID не задан — некому слать дайджест «почитать»")
        return
    items = reads.list("unread")
    if not items:
        return
    lines = ["📑 Дайджест «почитать» (непрочитанное):", ""]
    rows = []
    for r in items:
        head = r["title"] or r["url"]
        lines.append(f"• {head}")
        if r["summary"]:
            lines.append(f"  {r['summary']}")
        label = head if len(head) <= 30 else head[:29] + "…"
        rows.append([InlineKeyboardButton(f"✓ Прочитано: {label}",
                                          callback_data=f"{READS_DONE_PREFIX}{r['id']}")])
    await context.bot.send_message(
        chat_id=chat_id, text="\n".join(lines), reply_markup=InlineKeyboardMarkup(rows)
    )


async def weekly_review(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Раз в неделю (воскресенье вечером): сводка за последние 7 дней (§16).

    Цифры считает compute_week_stats (чистый Python над сторами), compose_summary
    оборачивает их через Gemini. LLM упал — шлём детерминированный format_review.
    Тяжёлое (SQLite + LLM) уводим в поток, чтобы не блокировать event loop."""
    if quiet_defer(context, weekly_review):
        return
    data = context.job.data
    chat_id = config.ALLOWED_USER_ID
    if chat_id is None:
        logger.warning("ALLOWED_USER_ID не задан — некому слать сводку за неделю")
        return
    end = date.today()
    start = end - timedelta(days=6)
    stats = await asyncio.to_thread(
        compute_week_stats, start, end,
        tasks=data["tasks"], bills=data["bills"], contacts=data["contacts"],
        reads=data["reads"], log=data["log"],
    )
    try:
        text = await asyncio.to_thread(compose_summary, data["llm"], stats)
    except Exception:
        logger.exception("weekly review: LLM упал — шлю детерминированную сводку")
        text = format_review(stats)
    await context.bot.send_message(chat_id=chat_id, text=text)


async def suggest_from_notes(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Раз в день: ищет в журнале повторяющиеся темы и предлагает превратить их
    в задачу (§13). Для каждой темы — отдельное сообщение с кнопками Да/Нет.

    mark_suggested зовём СРАЗУ после успешной отправки (не после ответа
    пользователя): иначе краш/рестарт бота между показом и ответом мог бы
    предложить ту же тему повторно. find_suggestions блокирующий (читает индекс
    и дёргает LLM для формулировки) — уводим в поток."""
    if quiet_defer(context, suggest_from_notes):
        return
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


async def generate_recurring_tasks(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Раз в день: генерирует task-инстансы на сегодня из активных recurring-шаблонов
    (§18.2). Ничего не шлёт пользователю — тихих часов не касается."""
    data = context.job.data
    recurring: RecurringTaskStore = data["recurring"]
    tasks: TaskStore = data["tasks"]
    created = await asyncio.to_thread(recurring.ensure_day, date.today(), tasks)
    if created:
        logger.info("Повторяющиеся задачи: создано инстансов на сегодня — %d", created)


async def cleanup_recurring_tasks(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Раз в день: удаляет выполненные recurring-инстансы старше RECURRING_KEEP_DAYS
    (§18.2). Обычные задачи не трогает. Ничего не шлёт пользователю."""
    tasks: TaskStore = context.job.data
    cutoff = (date.today() - timedelta(days=RECURRING_KEEP_DAYS)).isoformat()
    removed = await asyncio.to_thread(tasks.purge_recurring_done, cutoff)
    if removed:
        logger.info("Повторяющиеся задачи: вычищено старых выполненных инстансов — %d", removed)


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
    contacts=None,
    reads=None,
    recurring=None,
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
    handlers = Handlers(memory, llm, facts, bills, tasks, calendar, action_log, inbox,
                        suggest_log, contacts, reads, recurring)
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
    app.add_handler(CallbackQueryHandler(handlers.read_done, pattern=f"^{READS_DONE_PREFIX}"))
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
        # Ежедневное напоминание о ближайших днях рождения — если контакты настроены
        if contacts is not None:
            app.job_queue.run_daily(
                birthday_reminder, time=BIRTHDAY_REMINDER_TIME, data=contacts,
                name="birthday_reminder",
            )
        # Еженедельный дайджест «почитать» — если read-it-later настроен
        if reads is not None:
            app.job_queue.run_repeating(
                reads_digest, interval=timedelta(weeks=1), first=READS_DIGEST_TIME,
                data=reads, name="reads_digest",
            )
        # Еженедельная сводка — воскресенье вечером. first вычисляем как ближайшее
        # воскресенье в WEEKLY_REVIEW_TIME (Пн=0..Вс=6), дальше раз в неделю.
        now = datetime.now()
        days_ahead = (6 - now.weekday()) % 7
        first_review = datetime.combine(now.date() + timedelta(days=days_ahead), WEEKLY_REVIEW_TIME)
        if first_review <= now:
            first_review += timedelta(weeks=1)
        app.job_queue.run_repeating(
            weekly_review, interval=timedelta(weeks=1), first=first_review,
            data={"tasks": tasks, "bills": bills, "contacts": contacts,
                  "reads": reads, "log": action_log, "llm": llm},
            name="weekly_review",
        )
        # Повторяющиеся задачи (§18.2): генерация инстансов на сегодня + очистка
        # старой выполненной истории. Оба — если стор настроен; сообщений не шлют.
        if recurring is not None:
            app.job_queue.run_daily(
                generate_recurring_tasks, time=RECURRING_GENERATE_TIME,
                data={"recurring": recurring, "tasks": tasks}, name="generate_recurring_tasks",
            )
            app.job_queue.run_daily(
                cleanup_recurring_tasks, time=RECURRING_CLEANUP_TIME, data=tasks,
                name="cleanup_recurring_tasks",
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
    contacts=None,
    reads=None,
    recurring=None,
) -> None:
    """Запускает бота в режиме polling (блокирующий вызов, главный поток)."""
    build_application(token, memory, llm, facts, bills, tasks, calendar, action_log,
                      inbox, contacts, reads, recurring).run_polling(
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
    contacts=None,
    reads=None,
    recurring=None,
) -> None:
    """Polling в отдельном потоке: свой event loop, без обработчиков сигналов
    (их можно ставить только в главном потоке)."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    build_application(token, memory, llm, facts, bills, tasks, calendar, action_log,
                      inbox, contacts, reads, recurring).run_polling(
        drop_pending_updates=True, stop_signals=None
    )
