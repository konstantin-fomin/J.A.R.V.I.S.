"""Обработчики команд и сообщений Telegram-бота."""
import asyncio
import logging
from datetime import date, datetime, time, timedelta
from typing import Callable
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import ContextTypes

import config
from bills import BillStore, current_month
from bot.telegram_format import edit_html, reply_html
from decisions import DecisionLogger
from intents import IntentRouter, format_tasks, guard_chat_answer, parse_intent, route_after_resolve
from llm.ollama_client import LLMClient
from memory.facts import FactExtractor
from memory.manager import MemoryManager
from reads import enrich_link
from tasks import TaskStore
from voice import VoiceError, transcribe_voice
from vision import VisionError, describe_photo
from weekly_review import compose_summary, compute_week_stats, format_review

logger = logging.getLogger(__name__)

START_TEXT = """Привет! Я твой личный ассистент с памятью.

Просто пиши мне — я отвечаю с учётом всего, что знаю о тебе.
Задачами и платежами можно управлять обычным текстом: «добавь задачу…»,
«отметь … выполненной», «удали задачу…», «я оплатил…», «какие задачи?».
Всё общение сохраняется в журнал в Obsidian, а важные факты
я сам раскладываю по темам в память.

Команды:
/today — снимок дня одним сообщением: встречи, задачи, платежи, инбокс
/plan — план на день с учётом твоих целей и журнала
/bills — платежи текущего месяца со статусами
/memory — что я о тебе помню (список файлов)
/forget <тема> — удалить файл памяти
/inbox — заметки на разбор (с кнопкой «→ в задачу»)
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


def _allowed(update: Update) -> bool:
    if config.ALLOWED_USER_ID is None:
        return True
    return update.effective_user is not None and update.effective_user.id == config.ALLOWED_USER_ID


# Префикс callback_data для кнопки «оплачено»: "bill_paid:<instance_id>"
BILL_PAID_PREFIX = "bill_paid:"

# callback_data кнопок Да/Нет под подтверждением intent-действия
INTENT_YES = "intent_yes"
INTENT_NO = "intent_no"

# Префикс callback_data кнопки «→ в задачу» в /inbox: "inbox2task:<item_id>"
INBOX_TO_TASK_PREFIX = "inbox2task:"

# Компактная кнопка для инбокс-записи, чей полный текст не влезает в лимит
# Telegram (см. render_actionable_list) — полный текст тогда идёт строкой.
INBOX_SHORT_ACTION = "→ задача"

# Префикс callback_data кнопки чекбокса задачи в /tasks-списках: "task_done:<task_id>"
TASK_DONE_PREFIX = "task_done:"

# Сколько позиций списка (задачи/платежи) рендерим кнопками — остаток только
# упоминаем строкой «…и ещё N», чтобы клавиатура не расползалась на весь экран.
MAX_LIST_ITEMS = 8

# Подписи кнопок постоянной reply-клавиатуры (под полем ввода). Нажатие шлёт
# этот же текст обычным сообщением — handle_text перехватывает его до
# intent-парсинга и рендерит те же списки, что /bills, /inbox и NL query_tasks.
BTN_TASKS = "📋 Задачи"
BTN_BILLS = "💰 Платежи"
BTN_INBOX = "📥 Инбокс"


def main_reply_keyboard() -> ReplyKeyboardMarkup:
    """Постоянная клавиатура-ярлык: вешается на ответ /start."""
    return ReplyKeyboardMarkup([[BTN_TASKS, BTN_BILLS, BTN_INBOX]], resize_keyboard=True)

# Префиксы callback_data кнопок Да/Нет под проактивной подсказкой (§13).
# В data — theme_hash темы (40 hex-символов), по нему достаём формулировку из
# SuggestionLog (переживает рестарт бота).
SUGGEST_TASK_PREFIX = "sg_task:"   # «Да» → создать задачу
SUGGEST_DISMISS_PREFIX = "sg_skip:"  # «Нет» → ничего не делать

# Префикс callback_data кнопки «✓ Прочитано» в дайджесте «почитать»: "reads_done:<id>"
READS_DONE_PREFIX = "reads_done:"


# Платёж считается «скоро» для /today, если due_date не дальше этого числа дней.
SOON_BILL_DAYS = 3


def build_today_snapshot(
    *,
    next_meeting: dict | None = None,
    tasks_today: int = 0,
    tasks_done: int = 0,
    soon_bill: dict | None = None,
    inbox_pending: int = 0,
) -> str:
    """Снимок дня одним сообщением (/today) — та же логика сигналов, что в
    build_reminder_text/now-strip дашборда: ближайшая встреча, сколько задач
    осталось на сегодня, ближайший платёж (due ≤ SOON_BILL_DAYS). Плюс счётчик
    инбокса, которого на дашборде нет. Пусто — честно говорим об этом, не молчим."""
    parts = []
    if next_meeting:
        parts.append(f"🕐 Дальше: {next_meeting['time']} «{next_meeting['title']}»")
    if tasks_today > 0:
        left = tasks_today - tasks_done
        parts.append(
            f"📋 Осталось задач: {left}" if left > 0 else "📋 Все задачи на сегодня выполнены ✅"
        )
    if soon_bill:
        parts.append(f"💳 Платёж «{soon_bill['name']}» — {soon_bill['when']}")
    body = "\n".join(parts) if parts else "Пока ничего срочного 🙂"
    return f"📆 Сегодня\n\n{body}\n📥 Инбокс: {inbox_pending}"


def cap_list(items: list) -> tuple[list, int]:
    """Обрезает список до MAX_LIST_ITEMS. Возвращает (показанные, сколько ещё)."""
    if len(items) <= MAX_LIST_ITEMS:
        return items, 0
    return items[:MAX_LIST_ITEMS], len(items) - MAX_LIST_ITEMS


# Telegram Bot API: text кнопки InlineKeyboardButton — 1-64 символа. Дольше —
# клиент обрежет/не примет, поэтому это порог «влезает целиком vs нет».
BUTTON_TEXT_LIMIT = 64


def render_actionable_list(
    items: list[dict],
    *,
    is_actionable: Callable[[dict], bool],
    button_label: Callable[[dict], str],
    text_line: Callable[[dict], str],
    short_action: str,
    callback_data: Callable[[dict], str],
) -> tuple[list[str], InlineKeyboardMarkup | None]:
    """Общий рендер списка с чекбоксами (инбокс/задачи/платежи): НИКОГДА не
    обрезаем текст записи многоточием. Если полная подпись кнопки (button_label)
    умещается в лимит Telegram (BUTTON_TEXT_LIMIT) — запись показываем ТОЛЬКО
    кнопкой с этой подписью, без дублирования текстом. Если не умещается —
    текст идёт отдельной строкой целиком (text_line), а рядом — компактная
    кнопка-действие (short_action) без повтора содержимого. Неактивные записи
    (is_actionable=False — уже выполнено/оплачено/…) кнопки не получают вообще,
    всегда только текстом."""
    lines: list[str] = []
    rows: list[list[InlineKeyboardButton]] = []
    for it in items:
        if not is_actionable(it):
            lines.append(text_line(it))
            continue
        label = button_label(it)
        cb = callback_data(it)
        if len(label) <= BUTTON_TEXT_LIMIT:
            rows.append([InlineKeyboardButton(label, callback_data=cb)])
        else:
            lines.append(text_line(it))
            rows.append([InlineKeyboardButton(short_action, callback_data=cb)])
    markup = InlineKeyboardMarkup(rows) if rows else None
    return lines, markup


def _visible_tasks(items: list[dict], today: str) -> list[dict]:
    """Done-задача остаётся в списке только в день, когда её отметили — та же
    логика, что на дашборде (dashboard/index.html loadTasks: сравнение
    updated_at с today), иначе старые done-задачи копятся и захламляют список.
    due_date у done-задачи не важен, как и там."""
    return [t for t in items if t["status"] != "done" or (t["updated_at"] or "")[:10] == today]


TASK_SHORT_ACTION = "✅ отметить"


def _task_due_suffix(t: dict) -> str:
    due = ""
    if t["due_date"]:
        due = f" — {t['due_date']}" + (f" {t['due_time']}" if t["due_time"] else "")
    return due + (" ‼️" if t["priority"] == "high" else "")


def _task_line(t: dict) -> str:
    mark = {"done": "✅", "cancelled": "✖️"}.get(t["status"], "⏳")
    return f"{mark} {t['title']}{_task_due_suffix(t)}"


def _task_button_label(t: dict) -> str:
    return f"☑️ {t['title']}{_task_due_suffix(t)}"


def tasks_markup(items: list[dict]) -> InlineKeyboardMarkup | None:
    """Клавиатура чекбоксов для списка задач (используется и напрямую в тестах,
    и как «текущая клавиатура» для восстановления перед снятием кнопки)."""
    _, markup = render_actionable_list(
        items,
        is_actionable=lambda t: t["status"] not in ("done", "cancelled"),
        button_label=_task_button_label,
        text_line=_task_line,
        short_action=TASK_SHORT_ACTION,
        callback_data=lambda t: f"{TASK_DONE_PREFIX}{t['id']}",
    )
    return markup


BILL_SHORT_ACTION = "✅ отметить"


def _bill_line(b: dict) -> str:
    mark = "✅" if b["status"] == "paid" else "⏳"
    amount = f" — {b['amount']:.0f}" if b["amount"] is not None else ""
    return f"{mark} {b['due_date']}  {b['name']}{amount}"


def _bill_button_label(b: dict) -> str:
    return f"✅ Оплачено · {b['name']}"


def format_bills(instances: list[dict], header: str) -> str:
    """Список начислений со статусами, без кнопок — используется только вне
    Telegram-бота (сейчас нет ни одного живого вызова: код в intents.py
    оставлен на случай прямого вызова IntentRouter.execute вне handlers)."""
    lines = [header, ""] + [_bill_line(b) for b in instances]
    return "\n".join(lines)


def render_bills(instances: list[dict], header: str) -> tuple[str, InlineKeyboardMarkup | None]:
    """Общий рендер платежей (для /bills и напоминания «завтра платежи») —
    см. render_actionable_list: полный текст никогда не обрезаем."""
    shown, extra = cap_list(instances)
    lines, markup = render_actionable_list(
        shown,
        is_actionable=lambda b: b["status"] != "paid",
        button_label=_bill_button_label,
        text_line=_bill_line,
        short_action=BILL_SHORT_ACTION,
        callback_data=lambda b: f"{BILL_PAID_PREFIX}{b['id']}",
    )
    text = header + ("\n\n" + "\n".join(lines) if lines else "")
    if extra:
        text += f"\n…и ещё {extra}"
    return text, markup


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
        tasks: TaskStore,
        calendar=None,
        action_log=None,
        inbox=None,
        suggestlog=None,
        contacts=None,
        reads=None,
        recurring=None,
        obligations=None,
    ):
        self.memory = memory
        self.llm = llm
        self.facts = facts
        self.bills = bills
        self.tasks = tasks
        self.calendar = calendar
        self.inbox = inbox
        self.suggestlog = suggestlog  # SuggestionLog | None — лог проактивных подсказок
        self.contacts = contacts      # ContactStore | None — лёгкий CRM
        self.reads = reads            # ReadStore | None — read-it-later
        self.recurring = recurring    # RecurringTaskStore | None — повторяющиеся задачи
        self.obligations = obligations  # ObligationStore | None — обязательства (§19.1)
        self.action_log = action_log  # ActionLog | None — нужен для weekly review
        # Журнал решений (§19.3): исполняется хендлером (нужны LLM+память), не роутером.
        self.decisions = DecisionLogger(llm, memory)
        self.router = IntentRouter(tasks, bills, calendar, action_log, inbox, contacts, reads,
                                   recurring, obligations=obligations)

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not _allowed(update):
            return
        await reply_html(update.message, START_TEXT, reply_markup=main_reply_keyboard())

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
            await reply_html(update.message, "Не смог составить план 😔 Попробуй ещё раз.")
            return
        await reply_html(update.message, answer)

    async def today_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/today: снимок дня одним сообщением (см. build_today_snapshot)."""
        if not _allowed(update):
            return
        today = date.today()
        today_str = today.isoformat()
        open_tasks = [t for t in self.tasks.list() if t["status"] != "cancelled"]
        tt = [t for t in open_tasks if t["due_date"] == today_str or not t["due_date"]]
        tasks_done = len([t for t in tt if t["status"] == "done"])

        soon_bill = None
        for offset in range(SOON_BILL_DAYS + 1):
            d = today + timedelta(days=offset)
            self.bills.ensure_month(d.strftime("%Y-%m"))
            due = self.bills.due_on(d.isoformat(), status="pending")
            if due:
                when = "сегодня" if offset == 0 else ("завтра" if offset == 1 else f"через {offset} дн.")
                soon_bill = {"name": due[0]["name"], "when": when}
                break

        next_meeting = None
        if self.calendar is not None:
            tz = ZoneInfo(config.CALENDAR_TIMEZONE)
            now = datetime.now(tz)
            horizon = datetime.combine(today, time.max, tzinfo=tz)
            try:
                events = self.calendar.list_events(now, horizon)
            except Exception:
                logger.exception("Не удалось получить события для /today")
                events = []
            if events:
                ev = min(events, key=lambda e: e["start"])
                next_meeting = {"time": ev["start"].strftime("%H:%M"), "title": ev["title"]}

        inbox_pending = len(self.inbox.list("pending")) if self.inbox else 0

        text = build_today_snapshot(
            next_meeting=next_meeting, tasks_today=len(tt), tasks_done=tasks_done,
            soon_bill=soon_bill, inbox_pending=inbox_pending,
        )
        await reply_html(update.message, text)

    async def show_memory(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not _allowed(update):
            return
        files = self.memory.list_files()
        if not files:
            await reply_html(update.message, "Память пока пуста.")
            return
        listing = "\n".join(f"• {f}" for f in files)
        await reply_html(update.message, f"Файлы памяти:\n{listing}")

    async def forget(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not _allowed(update):
            return
        topic = " ".join(context.args) if context.args else ""
        if not topic:
            await reply_html(update.message, "Укажи тему: /forget работа\nСписок тем — в /memory")
            return
        deleted = self.memory.forget(topic)
        if deleted:
            await reply_html(update.message, f"Удалил {deleted} 🗑")
        else:
            await reply_html(update.message, f"Не нашёл файл памяти «{topic}». Посмотри список в /memory")

    async def bills_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not _allowed(update):
            return
        await self._query_bills(update)

    async def _query_tasks(self, update: Update, action: dict) -> None:
        """Рендер списка задач с чекбоксами: общий путь для /tasks-подобных
        NL-запросов (query_tasks) — список капается на MAX_LIST_ITEMS, под
        каждой показанной незакрытой задачей кнопка mark_task_done."""
        today = date.today().isoformat()
        if action.get("filter") == "today":
            items = self.tasks.list(due_date=today)
        else:
            items = self.tasks.list()
        items = _visible_tasks(items, today)
        if not items:
            await reply_html(update.message, format_tasks(items))
            return
        shown, extra = cap_list(items)
        lines, markup = render_actionable_list(
            shown,
            is_actionable=lambda t: t["status"] not in ("done", "cancelled"),
            button_label=_task_button_label,
            text_line=_task_line,
            short_action=TASK_SHORT_ACTION,
            callback_data=lambda t: f"{TASK_DONE_PREFIX}{t['id']}",
        )
        text = "📋 Задачи:" + ("\n\n" + "\n".join(lines) if lines else "")
        if extra:
            text += f"\n…и ещё {extra}"
        await reply_html(update.message, text, reply_markup=markup)

    async def _query_bills(self, update: Update) -> None:
        """Рендер платежей текущего месяца с чекбоксами «оплачено» — общий путь
        для /bills и NL-запроса query_bills."""
        ym = current_month()
        self.bills.ensure_month(ym)
        instances = self.bills.list_instances(ym)
        if not instances:
            await reply_html(
                update.message,
                "На этот месяц начислений нет. Шаблоны платежей заводятся на дашборде.",
            )
            return
        text, markup = render_bills(instances, f"💳 Платежи за {ym}:")
        # Клавиатуру с кнопками «оплачено» вешаем на последнее сообщение (см. reply_html)
        await reply_html(update.message, text, reply_markup=markup)

    async def mark_task_done(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Callback чекбокса задачи: как mark_paid/inbox_to_task — но само
        завершение идёт через IntentRouter.execute (complete_task уже в _LOGGED),
        поэтому действие журналируется и отменяемо через undo_last."""
        query = update.callback_query
        if not _allowed(update):
            await query.answer()
            return
        try:
            task_id = int(query.data[len(TASK_DONE_PREFIX):])
        except (ValueError, IndexError):
            await query.answer("Не понял кнопку 🤔")
            return

        task = self.tasks.get(task_id)
        if task is None:
            await query.answer("Задача не найдена")
        elif task["status"] == "done":
            await query.answer("Уже отмечена выполненной")
        else:
            await asyncio.to_thread(
                self.router.execute,
                {"type": "complete_task", "task_id": task_id, "title": task["title"]},
            )
            await query.answer(f"✅ {task['title']} — выполнено")

        # Убираем нажатую кнопку, остальные задачи оставляем доступными
        new_markup = _markup_without(query.message.reply_markup, query.data)
        try:
            await query.edit_message_reply_markup(reply_markup=new_markup)
        except Exception:
            logger.debug("Не удалось обновить клавиатуру задач", exc_info=True)

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

    # Группировка /inbox по статусу разбора (§19.2): порядок и заголовки секций.
    # processed сюда не входит — это «разобранные» записи, их не показываем.
    _INBOX_GROUPS = [
        ("pending", "📥 На разбор:"),
        ("needs_decision", "🤔 Нужно решить:"),
        ("someday", "💤 Когда-нибудь:"),
        ("maybe_later", "⏳ Может быть потом:"),
    ]

    async def inbox_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/inbox: активные заметки, сгруппированные по статусу разбора (§19.2),
        у каждой кнопка «→ в задачу»."""
        if not _allowed(update):
            return
        items = self.inbox.list() if self.inbox else []
        active = [it for it in items if it["status"] != "processed"]
        if not active:
            await reply_html(update.message, "Инбокс пуст 📥")
            return
        lines: list[str] = []
        rows: list[list[InlineKeyboardButton]] = []
        for status, header in self._INBOX_GROUPS:
            group = [it for it in active if it["status"] == status]
            if not group:
                continue
            if lines:
                lines.append("")
            lines.append(header)
            group_lines, group_markup = render_actionable_list(
                group,
                is_actionable=lambda it: True,
                button_label=lambda it: f"→ в задачу: {it['text']}",
                text_line=lambda it: f"• {it['text']}",
                short_action=INBOX_SHORT_ACTION,
                callback_data=lambda it: f"{INBOX_TO_TASK_PREFIX}{it['id']}",
            )
            lines.extend(group_lines)
            if group_markup:
                rows.extend(group_markup.inline_keyboard)
        markup = InlineKeyboardMarkup(rows) if rows else None
        await reply_html(update.message, "\n".join(lines), reply_markup=markup)

    async def inbox_to_task(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Callback «→ в задачу»: конвертирует inbox_item в задачу через обычный
        create_task path (значит, действие журналируется и отменяемо) и помечает
        запись processed."""
        query = update.callback_query
        if not _allowed(update):
            await query.answer()
            return
        try:
            item_id = int(query.data[len(INBOX_TO_TASK_PREFIX):])
        except (ValueError, IndexError):
            await query.answer("Не понял кнопку 🤔")
            return

        item = self.inbox.get(item_id) if self.inbox else None
        if item is None:
            await query.answer("Запись не найдена")
        elif item["status"] == "processed":
            await query.answer("Уже разобрано")
        else:
            await asyncio.to_thread(
                self.router.execute,
                {"type": "create_task", "params": {"title": item["text"], "source": "inbox"}},
            )
            self.inbox.set_status(item_id, "processed")
            await query.answer("✅ В задачу")

        # Убираем нажатую кнопку, остальные заметки оставляем
        new_markup = _markup_without(query.message.reply_markup, query.data)
        try:
            await query.edit_message_reply_markup(reply_markup=new_markup)
        except Exception:
            logger.debug("Не удалось обновить клавиатуру инбокса", exc_info=True)

    async def suggest_to_task(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Callback «Да» под проактивной подсказкой: создаёт задачу из темы.

        Формулировку темы достаём из SuggestionLog по theme_hash (а не из памяти
        процесса) — кнопка может прийти после рестарта бота. Создаём через обычный
        create_task path: значит, действие журналируется и отменяемо (undo_last)."""
        query = update.callback_query
        if not _allowed(update):
            await query.answer()
            return
        theme_hash = query.data[len(SUGGEST_TASK_PREFIX):]
        label = self.suggestlog.label_for(theme_hash) if self.suggestlog else None
        if not label:
            await query.answer("Подсказка устарела 🤔")
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception:
                logger.debug("Не смог убрать клавиатуру подсказки", exc_info=True)
            return
        await asyncio.to_thread(
            self.router.execute,
            {"type": "create_task",
             "params": {"title": label, "source": "suggestion"},
             "source": "suggestion"},
        )
        await query.answer("✅ В задачу")
        await edit_html(query, f"✅ Создал задачу: «{label}»")

    async def suggest_dismiss(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Callback «Нет» под проактивной подсказкой: ничего не создаём.

        Тема уже помечена показанной (mark_suggested при отправке), так что
        repeat_block_days не даст спамить ей снова в ближайшие дни."""
        query = update.callback_query
        if not _allowed(update):
            await query.answer()
            return
        await query.answer("Ок, не буду")
        await edit_html(query, "Ок, не превращаю в задачу 👌")

    async def handle_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not _allowed(update):
            return
        text = update.message.text.strip()

        # Пересланное сообщение — сразу в инбокс (source=forward), минуя
        # intent-парсинг: сам факт форварда уже однозначный сигнал «сохрани».
        if update.message.forward_origin is not None:
            await self._capture_forward(update, text)
            return

        # Кнопки постоянной reply-клавиатуры — те же списки, что /bills,
        # /inbox и NL query_tasks, без похода к LLM.
        if text == BTN_TASKS:
            await self._query_tasks(update, {"filter": None})
            return
        if text == BTN_BILLS:
            await self._query_bills(update)
            return
        if text == BTN_INBOX:
            await self.inbox_cmd(update, context)
            return

        # Режим дневника: записать без ответа модели
        if text.startswith("📓"):
            entry = text.removeprefix("📓").strip()
            self.memory.log_message("дневник", entry)
            await reply_html(update.message, "Записал в дневник 📓")
            return

        await self._process_text(update, context, text)

    async def _capture_forward(self, update: Update, text: str) -> None:
        """Пересланное сообщение → инбокс с source=forward, через обычный
        capture path (IntentRouter.execute — capture уже в _LOGGED, значит
        журналируется и отменяемо undo_last), но без гейта подтверждения:
        форвард — это уже явное действие пользователя."""
        reply = await asyncio.to_thread(
            self.router.execute, {"type": "capture", "text": text, "capture_source": "forward"},
        )
        await reply_html(update.message, reply)

    async def handle_voice(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Голосовое: скачать OGG → ffmpeg → WAV → Gemini-транскрипция → общий pipeline.

        Распознанный текст уходит в тот же _process_text, что и обычные сообщения,
        поэтому намерения (create_task/mark_bill_paid/…) работают и голосом."""
        if not _allowed(update):
            return
        voice = update.message.voice or update.message.audio
        if voice is None:
            return

        await update.message.chat.send_action(ChatAction.TYPING)
        try:
            tg_file = await context.bot.get_file(voice.file_id)
            ogg_bytes = bytes(await tg_file.download_as_bytearray())
            text = await asyncio.to_thread(transcribe_voice, ogg_bytes)
        except VoiceError:
            logger.exception("Не удалось обработать голосовое")
            await reply_html(update.message, "Не смог распознать голосовое 😔 Попробуй ещё раз или напиши текстом.")
            return
        except Exception:
            logger.exception("Сбой при скачивании/обработке голосового")
            await reply_html(update.message, "Что-то пошло не так с голосовым 😔 Напиши, пожалуйста, текстом.")
            return

        # Транскрипция неуверенная/звук непонятен — честно просим повторить текстом
        if not text:
            await reply_html(update.message, "Не разобрал, что в голосовом 🎧 Повтори, пожалуйста, текстом.")
            return

        await self._process_text(update, context, text)

    async def handle_photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Фото: скачать JPEG → Gemini (с подписью, если есть) → ответ текстом (§5-bis).

        Минимальный путь: мультимодальный ответ уходит как обычное текстовое
        сообщение. Пока без inbox-записи и привязки файлов — это отдельный шаг."""
        if not _allowed(update):
            return
        photo = update.message.photo
        if not photo:
            return
        caption = update.message.caption  # подпись к фото или None

        await update.message.chat.send_action(ChatAction.TYPING)
        try:
            # photo — список размеров по возрастанию; берём самый крупный.
            tg_file = await context.bot.get_file(photo[-1].file_id)
            image_bytes = bytes(await tg_file.download_as_bytearray())
            answer = await asyncio.to_thread(describe_photo, image_bytes, caption)
        except VisionError:
            logger.exception("Не удалось обработать фото")
            await reply_html(update.message, "Не смог разобрать фото 😔 Попробуй ещё раз или опиши текстом.")
            return
        except Exception:
            logger.exception("Сбой при скачивании/обработке фото")
            await reply_html(update.message, "Что-то пошло не так с фото 😔 Напиши, пожалуйста, текстом.")
            return

        if not answer:
            await reply_html(update.message, "Не понял, что на фото 🖼 Попробуй другое фото или опиши текстом.")
            return

        await reply_html(update.message, answer)

    async def _process_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
        """Общий путь для текста и расшифрованного голоса: намерение → действие/чат."""
        # Сначала пробуем распознать намерение (создать/выполнить/удалить задачу,
        # отметить платёж и т.п.). Любая ошибка парсинга → intent none → обычный чат.
        await update.message.chat.send_action(ChatAction.TYPING)
        try:
            intent_data = await asyncio.to_thread(parse_intent, self.llm, text)
        except Exception:
            logger.exception("Парсинг намерения упал — уходим в обычный чат")
            intent_data = {"intent": "none", "confidence": "high"}

        resolution = self.router.resolve(intent_data)
        # Прокидываем исходный текст в журнал действий (для execute и confirm —
        # action один и тот же объект, в т.ч. когда он осядет в pending_action).
        if resolution.action is not None:
            resolution.action["raw_message"] = text
        route = route_after_resolve(intent_data, resolution.kind)
        if route == "handle":
            await self._handle_resolution(update, context, resolution)
            return
        if route == "refuse":
            # Модель пыталась выдать команду, которой нет (§3/§(б)). Честный отказ
            # вместо chat-пайплайна — иначе он сконфабулирует «сделал».
            await reply_html(
                update.message,
                "Не понял, что нужно сделать — такой команды я пока не умею. "
                "Можешь переформулировать или сделать это вручную/одной поддерживаемой командой.",
            )
            return

        # genuine none → обычный chat/memory pipeline
        await self._chat(update, context, text)

    async def _handle_resolution(self, update, context, resolution) -> None:
        if resolution.kind == "message":
            await reply_html(update.message, resolution.text)
            return
        if resolution.kind == "execute":
            if resolution.action["type"] == "save_link":
                await self._save_link(update, resolution.action)
                return
            if resolution.action["type"] == "query_weekly_review":
                await self._weekly_review(update)
                return
            if resolution.action["type"] in ("log_decision", "query_decisions"):
                await self._decision(update, resolution.action)
                return
            if resolution.action["type"] == "query_tasks":
                await self._query_tasks(update, resolution.action)
                return
            if resolution.action["type"] == "query_bills":
                await self._query_bills(update)
                return
            reply = await asyncio.to_thread(self.router.execute, resolution.action)
            await reply_html(update.message, reply)
            return
        if resolution.kind == "confirm":
            context.chat_data["pending_action"] = resolution.action
            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("✅ Да", callback_data=INTENT_YES),
                        InlineKeyboardButton("❌ Нет", callback_data=INTENT_NO),
                    ]
                ]
            )
            await reply_html(update.message, f"Уточню: {resolution.label}", reply_markup=keyboard)

    async def _save_link(self, update: Update, action: dict) -> None:
        """save_link: качаем страницу + саммари (сеть+LLM, поэтому в to_thread),
        дотягиваем title/summary в params и сохраняем штатным execute (логируется,
        отменяемо). Сам сейв вынесен сюда из IntentRouter — там нет LLM/сети."""
        url = action["params"]["url"]
        await update.message.chat.send_action(ChatAction.TYPING)
        info = await asyncio.to_thread(enrich_link, self.llm, url)
        action["params"].update(title=info["title"], summary=info["summary"])
        reply = await asyncio.to_thread(self.router.execute, action)
        await reply_html(update.message, reply)

    async def _decision(self, update: Update, action: dict) -> None:
        """Журнал решений (§19.3): извлечение+запись (log_decision) или поиск
        (query_decisions). Обе операции трогают LLM/память — в to_thread. Router
        лишь гейтит safe и отдаёт сюда исходный текст."""
        await update.message.chat.send_action(ChatAction.TYPING)
        text = action.get("text") or ""
        if action["type"] == "log_decision":
            reply = await asyncio.to_thread(self.decisions.log_decision, text)
        else:
            reply = await asyncio.to_thread(self.decisions.query_decisions, text)
        await reply_html(update.message, reply)

    async def _weekly_review(self, update: Update) -> None:
        """query_weekly_review: цифры считает compute_week_stats (чистый Python над
        сторами), а compose_summary оборачивает их через LLM. IntentRouter без LLM,
        поэтому собираем здесь. LLM упал — шлём детерминированный format_review."""
        await update.message.chat.send_action(ChatAction.TYPING)
        end = date.today()
        start = end - timedelta(days=6)  # последние 7 дней включительно
        stats = await asyncio.to_thread(
            compute_week_stats, start, end,
            tasks=self.tasks, bills=self.bills, contacts=self.contacts,
            reads=self.reads, log=self.action_log,
        )
        try:
            text = await asyncio.to_thread(compose_summary, self.llm, stats)
        except Exception:
            logger.exception("weekly review: LLM упал — шлю детерминированную сводку")
            text = format_review(stats)
        await reply_html(update.message, text)

    async def read_done(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Callback «✓ Прочитано» в дайджесте: отмечает ссылку прочитанной штатным
        путём (логируется, отменяемо undo_last)."""
        query = update.callback_query
        if not _allowed(update):
            await query.answer()
            return
        try:
            read_id = int(query.data[len(READS_DONE_PREFIX):])
        except (ValueError, IndexError):
            await query.answer("Не понял кнопку 🤔")
            return
        item = self.reads.get(read_id) if self.reads else None
        if item is None:
            await query.answer("Ссылка не найдена")
        elif item["status"] == "read":
            await query.answer("Уже прочитано")
        else:
            await asyncio.to_thread(
                self.router.execute,
                {"type": "mark_read", "read_id": read_id, "title": item["title"] or item["url"]},
            )
            await query.answer("✅ Прочитано")
        new_markup = _markup_without(query.message.reply_markup, query.data)
        try:
            await query.edit_message_reply_markup(reply_markup=new_markup)
        except Exception:
            logger.debug("Не удалось обновить клавиатуру дайджеста", exc_info=True)

    async def confirm_intent(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Callback кнопок Да/Нет под подтверждением intent-действия."""
        query = update.callback_query
        if not _allowed(update):
            await query.answer()
            return
        action = context.chat_data.pop("pending_action", None)
        if action is None:
            await query.answer("Действие устарело")
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception:
                logger.debug("Не смог убрать клавиатуру подтверждения", exc_info=True)
            return
        if query.data == INTENT_NO:
            await query.answer("Отменено")
            await edit_html(query, "Отменено ❌")
            return
        await query.answer()
        reply = await asyncio.to_thread(self.router.execute, action)
        await edit_html(query, reply)

    async def _chat(self, update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
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
            # (1) Последняя линия защиты §(б): если модель всё же сымитировала
            # выполнение действия — заменяем честным отказом, не отправляем как есть.
            answer = guard_chat_answer(answer)
        except Exception:
            logger.exception("Ошибка при обработке сообщения")
            await reply_html(update.message, "Что-то пошло не так 😔 Проверь настройки провайдера и попробуй ещё раз.")
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

        await reply_html(update.message, answer)
