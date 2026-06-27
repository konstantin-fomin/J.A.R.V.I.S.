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
from datetime import date

from bills import current_month

logger = logging.getLogger(__name__)

INTENTS = {
    "create_task",
    "complete_task",
    "delete_task",
    "query_tasks",
    "mark_bill_paid",
    "query_bills",
    "none",
}

PROMPT = """Ты — парсер намерений личного ассистента. Определи, что пользователь \
хочет сделать, и верни ТОЛЬКО один JSON-объект без markdown и пояснений.

Сегодня: {date}

Возможные intent:
- create_task — создать задачу/напоминание. Поля: title (строка), due_date ("ГГГГ-ММ-ДД" или null), due_time ("ЧЧ:ММ" или null), priority ("low"|"normal"|"high").
- complete_task — отметить задачу выполненной. Поле: title_hint.
- delete_task — удалить задачу. Поле: title_hint.
- query_tasks — показать задачи. Поле: filter ("today"|"all"|null).
- mark_bill_paid — отметить платёж оплаченным. Поле: name_hint.
- query_bills — показать платежи текущего месяца.
- none — это обычное сообщение/вопрос/разговор, а не команда.

Правила:
- Большинство сообщений — обычный разговор (none). Выбирай действие только если пользователь явно о нём просит.
- title_hint/name_hint — это нечёткие слова пользователя для поиска по подстроке, НЕ точный id.
- title_hint/name_hint возвращай в начальной форме (именительный падеж, единственное число): «квартиру»→«квартира», «задачу полить кактусы»→«полить кактус». Это нужно для поиска по подстроке.
- Относительные даты («завтра», «в пятницу», «через неделю») переводи в due_date относительно «сегодня».
- Заводить шаблон платежа через текст нельзя — это none.
- confidence: "high" если намерение явное и однозначное, "low" если есть сомнения.

Верни JSON строго такого вида (лишние поля оставляй null):
{{"intent": "...", "confidence": "high|low", "title": null, "due_date": null, "due_time": null, "priority": "normal", "title_hint": null, "name_hint": null, "filter": null}}

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

    def __init__(self, tasks, bills):
        self.tasks = tasks
        self.bills = bills

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

        if intent == "create_task":
            title = str(data.get("title") or "").strip()
            if not title:
                return Resolution("chat")  # нечего создавать — пусть отвечает обычно
            params = {"title": title, "priority": data.get("priority") or "normal", "source": "telegram"}
            for key in ("due_date", "due_time"):
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

        return Resolution("chat")

    @staticmethod
    def _auto_or_confirm(action: dict, label: str, low_confidence: bool) -> Resolution:
        if low_confidence:
            return Resolution("confirm", action=action, label=label)
        return Resolution("execute", action=action)

    def execute(self, action: dict) -> str:
        kind = action["type"]
        if kind == "create_task":
            task = self.tasks.create(**action["params"])
            due = ""
            if task["due_date"]:
                due = f" на {task['due_date']}" + (f" {task['due_time']}" if task["due_time"] else "")
            priority = " (важно)" if task["priority"] == "high" else ""
            return f"✅ Создал задачу: «{task['title']}»{due}{priority}"
        if kind == "complete_task":
            self.tasks.update(action["task_id"], status="done")
            return f"✅ Отметил выполненной: «{action['title']}»"
        if kind == "delete_task":
            self.tasks.delete(action["task_id"])
            return f"🗑 Удалил задачу: «{action['title']}»"
        if kind == "query_tasks":
            if action.get("filter") == "today":
                items = self.tasks.list(due_date=date.today().isoformat())
            else:
                items = self.tasks.list()
            return format_tasks(items)
        if kind == "mark_bill_paid":
            self.bills.set_status(action["instance_id"], "paid")
            return f"✅ Платёж «{action['name']}» отмечен оплаченным"
        if kind == "query_bills":
            from bot.handlers import format_bills  # отложенный импорт: bot.handlers импортирует этот модуль

            ym = current_month()
            self.bills.ensure_month(ym)
            items = self.bills.list_instances(ym)
            if not items:
                return "На этот месяц начислений нет."
            return format_bills(items, f"💳 Платежи за {ym}:")
        return "Не понял действие 🤔"
