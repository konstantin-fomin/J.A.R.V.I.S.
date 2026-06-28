"""Парсер свободного текста → намерение (Gemini → JSON) и роутер действий.

Свободный текст бота сначала проходит через parse_intent(); если intent="none",
бот падает в обычный chat/memory pipeline. Иначе IntentRouter превращает
намерение в действие над tasks/bills. См. раздел 8 в JARVIS_SPEC.md.

Модуль не зависит от Telegram — его легко тестировать отдельно. Telegram-обвязка
(кнопки Да/Нет, отправка сообщений) живёт в bot/handlers.py.
"""
import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from bills import current_month

if TYPE_CHECKING:
    from calendar_client import CalendarClient

logger = logging.getLogger(__name__)

INTENTS = {
    "create_task",
    "complete_task",
    "delete_task",
    "query_tasks",
    "mark_bill_paid",
    "query_bills",
    "create_event",
    "move_event",
    "delete_event",
    "query_events",
    "query_by_project",
    "capture",
    "create_contact",
    "update_contact",
    "query_contacts",
    "delete_contact",
    "save_link",
    "query_reads",
    "mark_read",
    "undo_last",
    "none",
}

# Окно (дней) для запроса «у кого скоро ДР» — шире, чем у ежедневного напоминания
# (config.BIRTHDAY_REMINDER_LEAD_DAYS), чтобы «покажи ближайшие ДР» давал обзор.
CONTACT_QUERY_BIRTHDAY_DAYS = 30

# Какие типы действий журналируются и как: action.type → (entity_type, action в журнале).
# Всё, что меняет данные, проходит через одно место (IntentRouter.execute) и
# попадает сюда. query_* и реверсы undo здесь отсутствуют — они не логируются.
_LOGGED = {
    "create_task": ("task", "create"),
    "complete_task": ("task", "update"),
    "delete_task": ("task", "delete"),
    "mark_bill_paid": ("bill", "mark_paid"),
    "create_event": ("calendar_event", "create"),
    "move_event": ("calendar_event", "update"),
    "delete_event": ("calendar_event", "delete"),
    "create_contact": ("contact", "create"),
    "update_contact": ("contact", "update"),
    "delete_contact": ("contact", "delete"),
    "save_link": ("read", "create"),
    "mark_read": ("read", "update"),
}

PROMPT = """Ты — парсер намерений личного ассистента. Определи, что пользователь \
хочет сделать, и верни ТОЛЬКО один JSON-объект без markdown и пояснений.

Сегодня: {date}

Возможные intent:
- create_task — создать задачу/напоминание. Поля: title (строка), due_date ("ГГГГ-ММ-ДД" или null), due_time ("ЧЧ:ММ" или null), priority ("low"|"normal"|"high"), project (тема/проект или null).
- complete_task — отметить задачу выполненной. Поле: title_hint.
- delete_task — удалить задачу. Поле: title_hint.
- query_tasks — показать задачи. Поле: filter ("today"|"all"|null).
- mark_bill_paid — отметить платёж оплаченным. Поле: name_hint.
- query_bills — показать платежи текущего месяца.
- create_event — создать встречу/событие в календаре. Поля: title, date ("ГГГГ-ММ-ДД"), start_time ("ЧЧ:ММ"), end_time ("ЧЧ:ММ" или null).
- move_event — перенести встречу на другое время. Поля: title_hint, date (новая дата), start_time (новое время).
- delete_event — удалить встречу из календаря. Поле: title_hint.
- query_events — показать встречи. Поле: filter ("today"|"week"|null).
- query_by_project — показать задачи по теме/проекту («что у меня по X», «покажи задачи по проекту X»). Поле: project (название темы).
- capture — записать мысль в инбокс (быстрый захват без разбора). Поле: note (что записать).
- create_contact — добавить человека в контакты («добавь контакт …», «запомни …»). Поля: name (имя), birthday ("ГГГГ-ММ-ДД" или null), note (заметка про человека или null).
- update_contact — отметить, что пообщался с человеком, и/или дописать заметку («созвонился с …», «виделся с …», «заметка про …»). Поля: name_hint (кто), note (что дописать или null). last_contact_date выставится на сегодня автоматически.
- query_contacts — показать контакты. Поле: filter ("upcoming_birthdays" — у кого скоро день рождения | "by_name" — поиск по имени | null — все). Для by_name заполни name.
- delete_contact — удалить контакт. Поле: name_hint.
- save_link — сохранить ссылку «на потом» / «почитать позже» / «в закладки». Срабатывает ТОЛЬКО если в сообщении есть URL И явный контекст отложенного чтения («почитаю потом», «на потом», «сохрани ссылку», «в закладки»). Просто URL без такого контекста — НЕ save_link (это обычное сообщение none). Поле: url (сам адрес).
- query_reads — показать список «почитать» («что у меня в почитать», «непрочитанные ссылки»). Полей нет.
- mark_read — отметить сохранённую ссылку прочитанной («прочитал статью про …», «отметь … прочитанным»). Поле: title_hint (по заголовку/теме ссылки).
- undo_last — отменить последнее выполненное действие («отмени», «отмени последнее», «верни как было»). Полей нет.
- none — это обычное сообщение/вопрос/разговор, а не команда.

Правила:
- Большинство сообщений — обычный разговор (none). Выбирай действие только если пользователь явно о нём просит.
- title_hint/name_hint — это нечёткие слова пользователя для поиска по подстроке, НЕ точный id.
- title_hint/name_hint/project возвращай в начальной форме (именительный падеж, единственное число): «квартиру»→«квартира», «задачу полить кактусы»→«полить кактус», «по ремонту»→«ремонт», «с мамой»→«мама». Это нужно для поиска по подстроке.
- project в create_task заполняй ТОЛЬКО если пользователь явно называет тему/проект («задача по ремонту», «для проекта лендинг»); иначе null.
- capture — это явный захват в инбокс. Срабатывает ТОЛЬКО на явный триггер: «запиши в инбокс», «в инбокс», «на заметку», «потом разберу», «закинь в инбокс». Без такого триггера это НЕ capture (обычная мысль/идея без триггера → none, конкретное дело → create_task). note — текст записи без самого триггера.
- Относительные даты («завтра», «в пятницу», «через неделю») переводи в due_date/date относительно «сегодня».
- Задача (task) — это дело/напоминание без конкретного времени-слота; встреча (event) — это про календарь, со временем начала. «Созвон в 15:00», «встреча с врачом завтра в 10» → create_event. «Купить молоко», «полить цветы» → create_task.
- Заводить шаблон платежа через текст нельзя — это none.
- confidence: "high" если намерение явное и однозначное, "low" если есть сомнения.

Верни JSON строго такого вида (лишние поля оставляй null):
{{"intent": "...", "confidence": "high|low", "title": null, "due_date": null, "due_time": null, "priority": "normal", "title_hint": null, "name_hint": null, "filter": null, "date": null, "start_time": null, "end_time": null, "project": null, "note": null, "name": null, "birthday": null, "url": null}}

Сообщение пользователя:
{text}"""


def _parse_json_object(raw: str) -> dict:
    """Достаёт JSON-объект из ответа модели (она может обернуть его в ```json)."""
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end <= start:
        return {}
    try:
        data = json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def parse_intent(llm, text: str, today: str | None = None) -> dict:
    """Возвращает нормализованный intent-объект. При любой ошибке — none
    (никогда не ломает обычный chat-pipeline)."""
    today = today or date.today().isoformat()
    try:
        raw = llm.chat([{"role": "user", "content": PROMPT.format(date=today, text=text)}])
    except Exception:
        logger.exception("intent: запрос к LLM не удался")
        return {"intent": "none", "confidence": "high"}
    data = _parse_json_object(raw)
    if data.get("intent") not in INTENTS:
        return {"intent": "none", "confidence": "high"}
    data["confidence"] = "low" if str(data.get("confidence", "")).strip().lower() == "low" else "high"
    return data


def format_tasks(items: list[dict]) -> str:
    if not items:
        return "Задач нет."
    marks = {"done": "✅", "cancelled": "✖️"}
    lines = ["📋 Задачи:", ""]
    for t in items:
        mark = marks.get(t["status"], "⏳")
        due = ""
        if t["due_date"]:
            due = f" — {t['due_date']}" + (f" {t['due_time']}" if t["due_time"] else "")
        pr = " ‼️" if t["priority"] == "high" else ""
        lines.append(f"{mark} {t['title']}{due}{pr}")
    return "\n".join(lines)


def format_events(events: list[dict]) -> str:
    if not events:
        return "Встреч нет."
    lines = ["📅 Встречи:", ""]
    for e in events:
        when = e["start"].strftime("%d.%m %H:%M")
        end = e["end"].strftime("%H:%M")
        lines.append(f"🕐 {when}–{end}  {e['title']}")
    return "\n".join(lines)


def format_contacts(items: list[dict], header: str) -> str:
    """Список контактов для query_contacts: имя + ДР + дата последнего контакта."""
    if not items:
        return "Контактов нет."
    lines = [header, ""]
    for c in items:
        bday = f" 🎂 {c['birthday']}" if c["birthday"] else ""
        last = f" · последний контакт {c['last_contact_date']}" if c["last_contact_date"] else ""
        lines.append(f"👤 {c['name']}{bday}{last}")
    return "\n".join(lines)


def format_reads(items: list[dict]) -> str:
    """Список «почитать»: заголовок (или url) + саммари под ним."""
    if not items:
        return "В «почитать» пусто 📭"
    lines = ["📑 Почитать позже:", ""]
    for r in items:
        lines.append(f"• {r['title'] or r['url']}")
        if r["summary"]:
            lines.append(f"  {r['summary']}")
    return "\n".join(lines)


@dataclass
class Resolution:
    """Что бот должен сделать с разобранным намерением.

    kind:
      chat    — это не команда, обычный chat-pipeline
      message — сразу ответить текстом (например, «не нашёл задачу…»)
      execute — выполнить action сразу и ответить результатом
      confirm — переспросить кнопками Да/Нет, выполнить action после «Да»
    """
    kind: str
    action: dict | None = None
    text: str | None = None
    label: str | None = None


class IntentRouter:
    """Резолвит intent в конкретное действие над tasks/bills и выполняет его."""

    def __init__(self, tasks, bills, calendar: "CalendarClient | None" = None,
                 action_log=None, inbox=None, contacts=None, reads=None):
        self.tasks = tasks
        self.bills = bills
        self.calendar = calendar  # None если календарь не настроен
        self.log = action_log     # ActionLog | None — журнал для undo_last
        self.inbox = inbox        # InboxStore | None — быстрый захват
        self.contacts = contacts  # ContactStore | None — лёгкий CRM
        self.reads = reads        # ReadStore | None — read-it-later

    def _find_tasks(self, hint: str | None, statuses: list[str] | None = None) -> list[dict]:
        hint = (hint or "").strip().lower()
        if not hint:
            return []
        items = [t for t in self.tasks.list() if hint in t["title"].lower()]
        if statuses:
            items = [t for t in items if t["status"] in statuses]
        return items

    def _find_bills(self, hint: str | None) -> list[dict]:
        hint = (hint or "").strip().lower()
        if not hint:
            return []
        ym = current_month()
        self.bills.ensure_month(ym)
        return [
            b
            for b in self.bills.list_instances(ym)
            if hint in b["name"].lower() and b["status"] != "paid"
        ]

    def resolve(self, data: dict) -> Resolution:
        intent = data.get("intent", "none")
        low = data.get("confidence") == "low"

        if intent == "none":
            return Resolution("chat")

        if intent == "undo_last":
            return self._resolve_undo()

        if intent == "create_task":
            title = str(data.get("title") or "").strip()
            if not title:
                return Resolution("chat")  # нечего создавать — пусть отвечает обычно
            params = {"title": title, "priority": data.get("priority") or "normal", "source": "telegram"}
            for key in ("due_date", "due_time", "project"):
                if data.get(key):
                    params[key] = data[key]
            return self._auto_or_confirm(
                {"type": "create_task", "params": params}, f"создать задачу «{title}»?", low
            )

        if intent in ("complete_task", "delete_task"):
            hint = data.get("title_hint") or data.get("title")
            statuses = ["todo"] if intent == "complete_task" else None
            matches = self._find_tasks(hint, statuses)
            if not matches:
                return Resolution("message", text=f"Не нашёл задачу похожую на «{(hint or '').strip()}».")
            if len(matches) > 1:
                titles = ", ".join(f"«{t['title']}»" for t in matches[:5])
                return Resolution("message", text=f"Нашёл несколько задач: {titles}. Уточни, какую именно.")
            task = matches[0]
            if intent == "complete_task":
                return self._auto_or_confirm(
                    {"type": "complete_task", "task_id": task["id"], "title": task["title"]},
                    f"отметить выполненной «{task['title']}»?",
                    low,
                )
            # delete — всегда с подтверждением, независимо от confidence
            return Resolution(
                "confirm",
                action={"type": "delete_task", "task_id": task["id"], "title": task["title"]},
                label=f"удалить задачу «{task['title']}»?",
            )

        if intent == "mark_bill_paid":
            hint = data.get("name_hint") or data.get("name")
            matches = self._find_bills(hint)
            if not matches:
                return Resolution(
                    "message", text=f"Не нашёл неоплаченный платёж похожий на «{(hint or '').strip()}»."
                )
            if len(matches) > 1:
                names = ", ".join(f"«{b['name']}»" for b in matches[:5])
                return Resolution("message", text=f"Нашёл несколько платежей: {names}. Уточни, какой именно.")
            bill = matches[0]
            return self._auto_or_confirm(
                {"type": "mark_bill_paid", "instance_id": bill["id"], "name": bill["name"]},
                f"отметить платёж «{bill['name']}» оплаченным?",
                low,
            )

        if intent == "query_tasks":
            return self._auto_or_confirm(
                {"type": "query_tasks", "filter": data.get("filter")}, "показать задачи?", low
            )

        if intent == "query_bills":
            return self._auto_or_confirm({"type": "query_bills"}, "показать платежи?", low)

        if intent == "query_by_project":
            project = str(data.get("project") or "").strip()
            if not project:
                return Resolution("chat")  # без темы — обычный разговор
            # read-only → выполняем сразу, без подтверждения
            return Resolution("execute", action={"type": "query_by_project", "project": project})

        if intent == "capture":
            note = str(data.get("note") or "").strip()
            if not note:
                return Resolution("chat")  # нечего записывать
            return self._auto_or_confirm(
                {"type": "capture", "text": note}, f"записать в инбокс «{note}»?", low
            )

        if intent in ("create_contact", "update_contact", "query_contacts", "delete_contact"):
            if self.contacts is None:
                return Resolution("message", text="Контакты не настроены 🤔")
            return self._resolve_contact(intent, data, low)

        if intent in ("save_link", "query_reads", "mark_read"):
            if self.reads is None:
                return Resolution("message", text="Read-it-later не настроен 🤔")
            return self._resolve_read(intent, data, low)

        if intent in ("create_event", "move_event", "delete_event", "query_events"):
            if self.calendar is None:
                return Resolution(
                    "message",
                    text="Календарь не подключён. Нужно сгенерировать token.json "
                    "(см. §9 в JARVIS_SPEC.md).",
                )
            return self._resolve_calendar(intent, data)

        return Resolution("chat")

    @staticmethod
    def _auto_or_confirm(action: dict, label: str, low_confidence: bool) -> Resolution:
        if low_confidence:
            return Resolution("confirm", action=action, label=label)
        return Resolution("execute", action=action)

    # --- Контакты (лёгкий CRM, §14) ------------------------------------------
    # create/update — auto-or-confirm (как задачи); delete — всегда Да/Нет;
    # query_contacts — read-only, выполняется сразу. Все мутации логируются
    # (entity_type=contact) и отменяемы через undo_last.

    def _resolve_contact(self, intent: str, data: dict, low: bool) -> Resolution:
        assert self.contacts is not None  # resolve() гарантирует контакты до вызова

        if intent == "create_contact":
            name = str(data.get("name") or "").strip()
            if not name:
                return Resolution("chat")  # некого добавлять — обычный разговор
            params = {"name": name}
            if data.get("birthday"):
                params["birthday"] = data["birthday"]
            if data.get("note"):
                params["notes"] = data["note"]
            return self._auto_or_confirm(
                {"type": "create_contact", "params": params}, f"добавить контакт «{name}»?", low
            )

        if intent == "update_contact":
            hint = data.get("name_hint") or data.get("name")
            single = self._single_contact(self.contacts.find(hint), hint)
            if isinstance(single, Resolution):
                return single
            # сам факт «пообщался с X» проставляет дату последнего контакта
            fields = {"last_contact_date": date.today().isoformat()}
            note = str(data.get("note") or "").strip()
            if note:  # заметку дописываем к существующей, а не затираем
                existing = (single.get("notes") or "").strip()
                fields["notes"] = f"{existing}\n{note}" if existing else note
            return self._auto_or_confirm(
                {"type": "update_contact", "contact_id": single["id"],
                 "name": single["name"], "fields": fields},
                f"обновить контакт «{single['name']}»?", low,
            )

        if intent == "delete_contact":
            hint = data.get("name_hint") or data.get("name")
            single = self._single_contact(self.contacts.find(hint), hint)
            if isinstance(single, Resolution):
                return single
            return Resolution(
                "confirm",
                action={"type": "delete_contact", "contact_id": single["id"], "name": single["name"]},
                label=f"удалить контакт «{single['name']}»?",
            )

        if intent == "query_contacts":
            return Resolution("execute", action={"type": "query_contacts",
                                                 "filter": data.get("filter"),
                                                 "name": data.get("name")})
        return Resolution("chat")

    @staticmethod
    def _single_contact(matches: list[dict], hint: str | None):
        """Один матч → dict контакта; иначе Resolution('message') с пояснением."""
        if not matches:
            return Resolution("message", text=f"Не нашёл контакт похожий на «{(hint or '').strip()}».")
        if len(matches) > 1:
            names = ", ".join(f"«{c['name']}»" for c in matches[:5])
            return Resolution("message", text=f"Нашёл несколько контактов: {names}. Уточни, кого именно.")
        return matches[0]

    def _apply_contact(self, action: dict) -> tuple[str, str | None, dict | None, bool]:
        """Выполняет действие над контактом. Сюда не попадаем без настроенных
        контактов (resolve гарантирует), поэтому assert, а не мягкий guard."""
        assert self.contacts is not None
        kind = action["type"]
        if kind == "create_contact":
            c = self.contacts.create(**action["params"])
            bday = f" (др {c['birthday']})" if c["birthday"] else ""
            return f"✅ Добавил контакт: «{c['name']}»{bday}", str(c["id"]), c, True
        if kind == "update_contact":
            after = self.contacts.update(action["contact_id"], **action["fields"])
            return f"✅ Обновил контакт «{action['name']}»", str(action["contact_id"]), after, True
        if kind == "restore_contact":  # реверс undo: вернуть прежние поля контакта
            after = self.contacts.update(action["contact_id"], **action["fields"])
            return f"↩️ Вернул контакт «{action['name']}»", str(action["contact_id"]), after, True
        if kind == "delete_contact":
            self.contacts.delete(action["contact_id"])
            return f"🗑 Удалил контакт: «{action['name']}»", str(action["contact_id"]), None, True
        # query_contacts — read-only
        return self._query_contacts(action), None, None, False

    def _apply_read(self, action: dict) -> tuple[str, str | None, dict | None, bool]:
        """Действия read-it-later. Сюда не попадаем без настроенного ReadStore."""
        assert self.reads is not None
        kind = action["type"]
        if kind == "save_link":
            p = action["params"]  # url + (title/summary, дотянутые хендлером)
            r = self.reads.create(url=p["url"], title=p.get("title"), summary=p.get("summary"))
            head = r["title"] or r["url"]
            summ = f"\n{r['summary']}" if r["summary"] else ""
            return f"🔖 Сохранил в «почитать»: «{head}»{summ}", str(r["id"]), r, True
        if kind == "mark_read":
            after = self.reads.mark_read(action["read_id"])
            return f"✅ Отметил прочитанным: «{action['title']}»", str(action["read_id"]), after, True
        if kind == "restore_read":  # реверс undo: вернуть прежний статус
            after = self.reads.set_status(action["read_id"], action["status"])
            return f"↩️ Вернул ссылку в «{action['status']}»", str(action["read_id"]), after, True
        if kind == "delete_read":  # реверс undo save_link
            self.reads.delete(action["read_id"])
            return "↩️ Убрал ссылку из «почитать»", str(action["read_id"]), None, True
        # query_reads — read-only
        return format_reads(self.reads.list("unread")), None, None, False

    def _query_contacts(self, action: dict) -> str:
        assert self.contacts is not None
        filt = action.get("filter")
        if filt == "upcoming_birthdays":
            items = self.contacts.upcoming_birthdays(CONTACT_QUERY_BIRTHDAY_DAYS)
            return format_contacts(items, "🎂 Скоро дни рождения:") if items else \
                "Ближайших дней рождения нет."
        if filt == "by_name":
            items = self.contacts.find(action.get("name"))
            name = (action.get("name") or "").strip()
            return format_contacts(items, f"👤 Контакты по «{name}»:") if items else \
                f"Не нашёл контактов по «{name}»."
        items = self.contacts.list()
        return format_contacts(items, "👤 Контакты:")

    # --- Read-it-later (§15) -------------------------------------------------
    # save_link — всегда execute (хендлер обогатит params саммари до вызова
    # execute); query_reads — read-only; mark_read — по подстроке заголовка.
    # Все мутации логируются (entity_type=read) и отменяемы.

    def _resolve_read(self, intent: str, data: dict, low: bool) -> Resolution:
        assert self.reads is not None

        if intent == "save_link":
            url = str(data.get("url") or "").strip()
            if not url:
                return Resolution("chat")  # нет ссылки — обычный разговор
            # title/summary дотянет хендлер (там есть LLM и сеть), здесь только url
            return Resolution("execute", action={"type": "save_link", "params": {"url": url}})

        if intent == "query_reads":
            return Resolution("execute", action={"type": "query_reads"})

        if intent == "mark_read":
            hint = (data.get("title_hint") or data.get("title") or "").strip().lower()
            matches = [r for r in self.reads.list("unread")
                       if hint and (hint in (r["title"] or "").lower() or hint in r["url"].lower())]
            if not matches:
                return Resolution("message", text=f"Не нашёл непрочитанную ссылку похожую на «{hint}».")
            if len(matches) > 1:
                heads = ", ".join(f"«{r['title'] or r['url']}»" for r in matches[:5])
                return Resolution("message", text=f"Нашёл несколько ссылок: {heads}. Уточни, какую.")
            r = matches[0]
            return self._auto_or_confirm(
                {"type": "mark_read", "read_id": r["id"], "title": r["title"] or r["url"]},
                f"отметить прочитанной «{r['title'] or r['url']}»?", low,
            )
        return Resolution("chat")

    # --- Отмена последнего действия (undo_last) ------------------------------
    # Берём самую свежую запись журнала status='active', строим обратное
    # действие и помечаем запись undone (это делает execute по маркеру
    # _undo_log_id). Повторный undo_last возьмёт следующую active-запись.
    # Календарь реверсируется через Да/Нет (как любое изменение календаря);
    # task/bill — выполняются сразу.

    def _resolve_undo(self) -> Resolution:
        if self.log is None:
            return Resolution("message", text="Журнал действий не ведётся — отменять нечего.")
        rec = self.log.latest_active()
        if rec is None:
            return Resolution("message", text="Нечего отменять.")
        reverse = self._build_reverse(rec)
        if reverse is None:
            return Resolution("message", text="Не знаю, как отменить это действие 🤔")
        reverse["_undo_log_id"] = rec["id"]
        if rec["entity_type"] == "calendar_event":
            # изменения календаря всегда подтверждаем кнопками
            return Resolution("confirm", action=reverse, label=self._undo_label(rec))
        return Resolution("execute", action=reverse)

    @staticmethod
    def _build_reverse(rec: dict) -> dict | None:
        """Обратное действие для записи журнала (или None, если реверс неизвестен)."""
        et, act = rec["entity_type"], rec["action"]
        before, after = rec.get("before_state"), rec.get("after_state")
        eid = rec["entity_id"]

        if et == "task":
            if act == "create":  # создали → удаляем
                return {"type": "delete_task", "task_id": int(eid),
                        "title": (after or {}).get("title", "")}
            if act == "update" and before is not None:  # изменили → восстановить before
                fields = {k: before.get(k) for k in
                          ("title", "description", "status", "priority", "due_date", "due_time")}
                return {"type": "restore_task", "task_id": int(eid),
                        "fields": fields, "title": before.get("title", "")}
            if act == "delete" and before is not None:  # удалили → создаём заново (новый id — ок)
                params = {k: before[k] for k in
                          ("title", "description", "due_date", "due_time", "priority", "source")
                          if before.get(k) is not None}
                return {"type": "create_task", "params": params}

        if et == "bill":
            if act == "mark_paid":  # оплачено → вернуть прежний статус (pending)
                status = (before or {}).get("status") or "pending"
                name = (before or after or {}).get("name", "")
                return {"type": "set_bill_status", "instance_id": int(eid),
                        "status": status, "name": name}

        if et == "contact":
            if act == "create":  # создали → удаляем
                return {"type": "delete_contact", "contact_id": int(eid),
                        "name": (after or {}).get("name", "")}
            if act == "update" and before is not None:  # изменили → восстановить before
                fields = {k: before.get(k) for k in
                          ("name", "last_contact_date", "birthday", "notes")}
                return {"type": "restore_contact", "contact_id": int(eid),
                        "fields": fields, "name": before.get("name", "")}
            if act == "delete" and before is not None:  # удалили → создаём заново (новый id — ок)
                params = {k: before[k] for k in ("name", "last_contact_date", "birthday", "notes")
                          if before.get(k) is not None}
                return {"type": "create_contact", "params": params}

        if et == "read":
            if act == "create":  # сохранили ссылку → удаляем
                return {"type": "delete_read", "read_id": int(eid)}
            if act == "update" and before is not None:  # отметили прочитанной → вернуть статус
                return {"type": "restore_read", "read_id": int(eid),
                        "status": before.get("status", "unread")}

        if et == "calendar_event":
            if act == "create":  # создали встречу → удаляем
                return {"type": "delete_event", "event_id": eid,
                        "title": (after or {}).get("title", "")}
            if act == "update" and before is not None:  # перенесли → возвращаем на прежнее время
                return {"type": "move_event", "event_id": eid,
                        "title": before.get("title", ""),
                        "start": before["start"], "end": before["end"]}
            if act == "delete" and before is not None:  # удалили встречу → создаём заново
                return {"type": "create_event", "title": before.get("title", ""),
                        "start": before["start"], "end": before["end"]}

        return None

    @staticmethod
    def _undo_label(rec: dict) -> str:
        """Человекочитаемое описание для подтверждения отмены календарного действия."""
        after, before = rec.get("after_state") or {}, rec.get("before_state") or {}
        title = after.get("title") or before.get("title") or "встречу"
        descr = {
            "create": f"удалить встречу «{title}» (она была создана)",
            "update": f"вернуть встречу «{title}» на прежнее время",
            "delete": f"восстановить встречу «{title}»",
        }.get(rec["action"], f"отменить действие со встречей «{title}»")
        return f"отменить последнее действие — {descr}?"

    # --- Календарь -----------------------------------------------------------
    # ВСЕ изменения календаря (create/move/delete) идут через подтверждение
    # Да/Нет — встречи имеют последствия, порог осторожности выше, чем у задач.
    # query_events — read-only, выполняется сразу.

    def _resolve_calendar(self, intent: str, data: dict) -> Resolution:
        assert self.calendar is not None  # resolve() гарантирует календарь до вызова
        tz = ZoneInfo(self.calendar.timezone)

        if intent == "query_events":
            return Resolution("execute", action={"type": "query_events", "filter": data.get("filter")})

        if intent == "create_event":
            title = str(data.get("title") or "").strip()
            start = self._build_dt(data.get("date"), data.get("start_time"), tz)
            if not title or start is None:
                return Resolution("chat")  # нечего/некуда создавать — обычный чат
            end = self._build_dt(data.get("date"), data.get("end_time"), tz) or (start + timedelta(hours=1))
            label = f"создать встречу «{title}» {self._fmt_span(start, end)}?"
            label += self._conflict_suffix(start, end)
            return Resolution(
                "confirm",
                action={"type": "create_event", "title": title,
                        "start": start.isoformat(), "end": end.isoformat()},
                label=label,
            )

        if intent == "move_event":
            hint = data.get("title_hint") or data.get("title")
            matches = self._find_events(hint)
            single = self._single_event(matches, hint)
            if isinstance(single, Resolution):
                return single
            start = self._build_dt(data.get("date"), data.get("start_time"), tz)
            if start is None:
                return Resolution("chat")
            end = start + (single["end"] - single["start"])  # сохраняем длительность
            label = f"перенести «{single['title']}» на {self._fmt_span(start, end)}?"
            label += self._conflict_suffix(start, end, ignore_id=single["id"])
            return Resolution(
                "confirm",
                action={"type": "move_event", "event_id": single["id"], "title": single["title"],
                        "start": start.isoformat(), "end": end.isoformat()},
                label=label,
            )

        if intent == "delete_event":
            hint = data.get("title_hint") or data.get("title")
            matches = self._find_events(hint)
            single = self._single_event(matches, hint)
            if isinstance(single, Resolution):
                return single
            return Resolution(
                "confirm",
                action={"type": "delete_event", "event_id": single["id"], "title": single["title"]},
                label=f"удалить встречу «{single['title']}» ({self._fmt_span(single['start'], single['end'])})?",
            )

        return Resolution("chat")

    def _find_events(self, hint: str | None) -> list[dict]:
        """Предстоящие встречи (ближайшие 60 дней), название которых содержит hint."""
        assert self.calendar is not None
        hint = (hint or "").strip().lower()
        if not hint:
            return []
        tz = ZoneInfo(self.calendar.timezone)
        now = datetime.now(tz)
        events = self.calendar.list_events(now, now + timedelta(days=60))
        return [e for e in events if hint in e["title"].lower()]

    @staticmethod
    def _single_event(matches: list[dict], hint: str | None):
        """Один матч → dict встречи; иначе Resolution('message') с пояснением."""
        if not matches:
            return Resolution("message", text=f"Не нашёл встречу похожую на «{(hint or '').strip()}».")
        if len(matches) > 1:
            names = ", ".join(f"«{e['title']}»" for e in matches[:5])
            return Resolution("message", text=f"Нашёл несколько встреч: {names}. Уточни, какую именно.")
        return matches[0]

    def _conflict_suffix(self, start, end, ignore_id=None) -> str:
        """Предупреждение о пересечении для текста подтверждения (или пусто)."""
        assert self.calendar is not None
        conflicts = self.calendar.find_conflicts(start, end, ignore_id)
        if not conflicts:
            return ""
        parts = ", ".join(
            f"«{c['title']}» ({self._fmt_time(c['start'])}–{self._fmt_time(c['end'])})"
            for c in conflicts[:3]
        )
        return f"\n⚠️ Пересекается с: {parts}"

    @staticmethod
    def _build_dt(date_str, time_str, tz):
        """date+time из intent → aware datetime, либо None если поля пусты/кривые."""
        if not date_str or not time_str:
            return None
        try:
            d = date.fromisoformat(str(date_str))
            t = time.fromisoformat(str(time_str))
        except ValueError:
            return None
        return datetime.combine(d, t, tzinfo=tz)

    @staticmethod
    def _fmt_time(d) -> str:
        return d.strftime("%H:%M")

    @classmethod
    def _fmt_span(cls, start, end) -> str:
        if start.date() == end.date():
            return f"{start.strftime('%d.%m %H:%M')}–{cls._fmt_time(end)}"
        return f"{start.strftime('%d.%m %H:%M')}–{end.strftime('%d.%m %H:%M')}"

    def execute(self, action: dict) -> str:
        """Единая точка выполнения действия. Все мутации (см. _LOGGED) журналируются
        здесь: before_state снимается до изменения, after_state — после, чтобы их
        можно было отменить через undo_last.

        Реверс самой отмены (несёт _undo_log_id) новую запись не пишет, а помечает
        исходную undone — иначе повторный undo откатывал бы сам откат."""
        # Служебные ключи журнала вынимаем, чтобы не мешали логике действия.
        raw_message = action.pop("raw_message", None)
        source = action.pop("source", "telegram")
        undo_log_id = action.pop("_undo_log_id", None)
        meta = _LOGGED.get(action["type"])

        # Снимок состояния ДО мутации (для update/delete/mark_paid; у create — None).
        before = None
        if meta and self.log is not None and undo_log_id is None and meta[1] != "create":
            before = self._read_entity(meta[0], action)

        reply, entity_id, after, ok = self._apply(action)

        if self.log is not None and ok:
            if undo_log_id is not None:
                self.log.mark_undone(undo_log_id)
                reply = f"↩️ Отменил последнее действие.\n{reply}"
            elif meta is not None:
                self.log.log_action(
                    source=source, entity_type=meta[0], entity_id=entity_id,
                    action=meta[1], before_state=before, after_state=after,
                    raw_message=raw_message,
                )
        return reply

    def _apply(self, action: dict) -> tuple[str, str | None, dict | None, bool]:
        """Выполняет действие. Возвращает (ответ, entity_id, after_state, ok).
        ok=False для read-only (query_*) и неуспешных действий — их не журналируем."""
        kind = action["type"]
        if kind == "create_task":
            task = self.tasks.create(**action["params"])
            due = ""
            if task["due_date"]:
                due = f" на {task['due_date']}" + (f" {task['due_time']}" if task["due_time"] else "")
            priority = " (важно)" if task["priority"] == "high" else ""
            return f"✅ Создал задачу: «{task['title']}»{due}{priority}", str(task["id"]), task, True
        if kind == "complete_task":
            after = self.tasks.update(action["task_id"], status="done")
            return f"✅ Отметил выполненной: «{action['title']}»", str(action["task_id"]), after, True
        if kind == "restore_task":  # реверс undo: вернуть прежние поля задачи
            after = self.tasks.update(action["task_id"], **action["fields"])
            return f"↩️ Вернул задачу «{action['title']}»", str(action["task_id"]), after, True
        if kind == "delete_task":
            self.tasks.delete(action["task_id"])
            return f"🗑 Удалил задачу: «{action['title']}»", str(action["task_id"]), None, True
        if kind == "query_tasks":
            if action.get("filter") == "today":
                items = self.tasks.list(due_date=date.today().isoformat())
            else:
                items = self.tasks.list()
            return format_tasks(items), None, None, False
        if kind == "query_by_project":
            # Подстрочный матч по project (Python .lower() корректен и для кириллицы,
            # в отличие от SQL LIKE). Склонения нормализует парсер (именительный падеж).
            proj = (action.get("project") or "").strip()
            needle = proj.lower()
            items = [t for t in self.tasks.list() if needle and needle in (t["project"] or "").lower()]
            if not items:
                return f"По теме «{proj}» задач нет.", None, None, False
            return format_tasks(items), None, None, False
        if kind == "capture":
            if self.inbox is None:
                return "Инбокс не настроен 🤔", None, None, False
            item = self.inbox.create(action["text"], source="telegram")
            return f"📥 Записал в инбокс: «{item['text']}»", None, None, False
        if kind in ("create_contact", "update_contact", "restore_contact",
                    "delete_contact", "query_contacts"):
            return self._apply_contact(action)
        if kind in ("save_link", "mark_read", "restore_read", "delete_read", "query_reads"):
            return self._apply_read(action)
        if kind == "mark_bill_paid":
            after = self.bills.set_status(action["instance_id"], "paid")
            return f"✅ Платёж «{action['name']}» отмечен оплаченным", str(action["instance_id"]), after, True
        if kind == "set_bill_status":  # реверс undo: вернуть платёж в прежний статус
            after = self.bills.set_status(action["instance_id"], action["status"])
            return f"↩️ Платёж «{action['name']}» снова в ожидании", str(action["instance_id"]), after, True
        if kind == "query_bills":
            from bot.handlers import format_bills  # отложенный импорт: bot.handlers импортирует этот модуль

            ym = current_month()
            self.bills.ensure_month(ym)
            items = self.bills.list_instances(ym)
            if not items:
                return "На этот месяц начислений нет.", None, None, False
            return format_bills(items, f"💳 Платежи за {ym}:"), None, None, False

        if kind in ("create_event", "move_event", "delete_event", "query_events"):
            return self._execute_calendar(action)

        return "Не понял действие 🤔", None, None, False

    def _execute_calendar(self, action: dict) -> tuple[str, str | None, dict | None, bool]:
        assert self.calendar is not None  # сюда не попадаем без настроенного календаря
        kind = action["type"]
        try:
            if kind == "create_event":
                start = datetime.fromisoformat(action["start"])
                end = datetime.fromisoformat(action["end"])
                ev = self.calendar.create_event(action["title"], start, end)
                after = {"id": ev["id"], "title": action["title"],
                         "start": action["start"], "end": action["end"]}
                return (f"📅 Создал встречу: «{action['title']}» {self._fmt_span(start, end)}",
                        str(ev["id"]), after, True)
            if kind == "move_event":
                start = datetime.fromisoformat(action["start"])
                end = datetime.fromisoformat(action["end"])
                self.calendar.update_event(action["event_id"], start=start, end=end)
                after = {"title": action["title"], "start": action["start"], "end": action["end"]}
                return (f"📅 Перенёс «{action['title']}» на {self._fmt_span(start, end)}",
                        str(action["event_id"]), after, True)
            if kind == "delete_event":
                self.calendar.delete_event(action["event_id"])
                return f"🗑 Удалил встречу: «{action['title']}»", str(action["event_id"]), None, True
            if kind == "query_events":
                return self._query_events(action.get("filter")), None, None, False
        except Exception:
            logger.exception("Действие календаря %s упало", kind)
            return ("Не удалось выполнить действие с календарём 😔 Проверь, что token.json настроен.",
                    None, None, False)
        return "Не понял действие 🤔", None, None, False

    def _read_entity(self, entity_type: str, action: dict) -> dict | None:
        """Снимок текущего состояния сущности ДО мутации (для before_state)."""
        if entity_type == "task":
            return self.tasks.get(action["task_id"])
        if entity_type == "bill":
            return self.bills.get_instance(action["instance_id"])
        if entity_type == "contact":
            assert self.contacts is not None
            return self.contacts.get(action["contact_id"])
        if entity_type == "read":
            assert self.reads is not None
            return self.reads.get(action["read_id"])
        if entity_type == "calendar_event":
            assert self.calendar is not None
            return self._event_state(self.calendar.get_event(action["event_id"]))
        return None

    @staticmethod
    def _event_state(ev: dict | None) -> dict | None:
        """Событие календаря → JSON-safe dict (datetime → ISO-строка) для журнала."""
        if ev is None:
            return None
        def iso(v):
            return v.isoformat() if hasattr(v, "isoformat") else v
        return {"id": ev.get("id"), "title": ev.get("title"),
                "start": iso(ev["start"]), "end": iso(ev["end"])}

    def _query_events(self, filt) -> str:
        assert self.calendar is not None
        tz = ZoneInfo(self.calendar.timezone)
        now = datetime.now(tz)
        if filt == "week":
            start, end = now, now + timedelta(days=7)
        else:  # today / null
            start = datetime.combine(now.date(), time.min, tzinfo=tz)
            end = datetime.combine(now.date(), time.max, tzinfo=tz)
        return format_events(self.calendar.list_events(start, end))
