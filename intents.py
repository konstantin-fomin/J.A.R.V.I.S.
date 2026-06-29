"""Парсер свободного текста → намерение (Gemini → JSON) и роутер действий.

Свободный текст бота сначала проходит через parse_intent(); если intent="none",
бот падает в обычный chat/memory pipeline. Иначе IntentRouter превращает
намерение в действие над tasks/bills. См. раздел 8 в JARVIS_SPEC.md.

Модуль не зависит от Telegram — его легко тестировать отдельно. Telegram-обвязка
(кнопки Да/Нет, отправка сообщений) живёт в bot/handlers.py.
"""
import json
import logging
import re
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
    "create_obligation",
    "query_obligations",
    "complete_obligation",
    "delete_obligation",
    "inbox_reclassify",
    "log_decision",
    "query_decisions",
    "save_link",
    "query_reads",
    "mark_read",
    "query_weekly_review",
    "create_recurring_task",
    "query_recurring_tasks",
    "delete_recurring_template",
    "snooze",
    "undo_last",
    "edit_last",
    "none",
}

# Декларативная политика подтверждений (§17): intent → уровень риска.
#   safe      — выполнить сразу всегда (read-only query_* и save_link);
#   medium    — выполнить сразу, если confidence != "low", иначе переспросить;
#   dangerous — всегда подтверждение Да/Нет, независимо от уверенности.
# Роутер не разбрасывает проверки «if intent == delete_*», а смотрит сюда
# (см. _gate). undo_last/edit_last сюда не входят — у них свой путь.
RISK_LEVELS = {
    "create_task": "medium",
    "complete_task": "medium",
    "delete_task": "dangerous",
    "query_tasks": "medium",
    "mark_bill_paid": "medium",
    "query_bills": "medium",
    "create_event": "dangerous",
    "move_event": "dangerous",
    "delete_event": "dangerous",
    "query_events": "safe",
    "query_by_project": "safe",
    "capture": "medium",
    "create_contact": "medium",
    "update_contact": "medium",
    "query_contacts": "safe",
    "delete_contact": "dangerous",
    "create_obligation": "medium",
    "query_obligations": "safe",
    "complete_obligation": "medium",
    "delete_obligation": "dangerous",
    "inbox_reclassify": "medium",
    "log_decision": "safe",
    "query_decisions": "safe",
    "save_link": "safe",
    "query_reads": "safe",
    "mark_read": "medium",
    "query_weekly_review": "safe",
    "create_recurring_task": "medium",
    "query_recurring_tasks": "safe",
    "delete_recurring_template": "dangerous",
    "snooze": "medium",
}

# edit_last (§17): какие поля какой сущности можно править у последнего действия и
# как. entity_type → field (как его называет Gemini) → (колонка в сторе, режим).
# режим "set" — заменить значение, "append" — дописать к существующему тексту.
_EDIT_LAST_FIELDS = {
    "task": {
        "title": ("title", "set"),
        "priority": ("priority", "set"),
        "due_date": ("due_date", "set"),
        "due_time": ("due_time", "set"),
    },
    "contact": {
        "name": ("name", "set"),
        "birthday": ("birthday", "set"),
        "notes": ("notes", "append"),
        "note": ("notes", "append"),
    },
}

# Статусы разбора инбокса (§19.2) сверх pending/processed и их человекочитаемые
# подписи. inbox_reclassify принимает только эти значения; остальное — честный отказ.
INBOX_REVIEW_STATUSES = {
    "someday": "💤 когда-нибудь",
    "needs_decision": "🤔 нужно решить",
    "maybe_later": "⏳ может быть потом",
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
    "edit_task": ("task", "update"),
    "delete_task": ("task", "delete"),
    "mark_bill_paid": ("bill", "mark_paid"),
    "create_event": ("calendar_event", "create"),
    "move_event": ("calendar_event", "update"),
    "delete_event": ("calendar_event", "delete"),
    "create_contact": ("contact", "create"),
    "update_contact": ("contact", "update"),
    "delete_contact": ("contact", "delete"),
    "capture": ("inbox", "create"),
    "reclassify_inbox": ("inbox", "update"),
    "create_obligation": ("obligation", "create"),
    "complete_obligation": ("obligation", "update"),
    "delete_obligation": ("obligation", "delete"),
    "save_link": ("read", "create"),
    "mark_read": ("read", "update"),
}

# Snooze/defer (§18.1): именованные относительные смещения → (через сколько дней
# от сегодня, время). None во времени — «не трогать время задачи». Длительности
# («2h», «30m», «3d») обрабатываются отдельно регуляркой ниже.
_SNOOZE_NAMED = {
    "afternoon": (0, "15:00"),
    "evening": (0, "19:00"),
    "tonight": (0, "21:00"),
    "morning": (1, "09:00"),
    "tomorrow": (1, None),
    "next_week": (7, None),
}
_SNOOZE_DURATION = re.compile(r"^\+?(\d+)\s*([mhd])$")


def normalize_snooze_offset(offset: str, now: datetime) -> dict | None:
    """Относительный offset от Gemini → конкретные {due_date[, due_time]} от now.

    Канонические значения: именованные (evening/morning/tomorrow/next_week/…) и
    длительности «<N>m|h|d» (с необязательным «+»). m/h ставят и дату, и время;
    d/именованные tomorrow/next_week — только дату (время задачи не трогаем).
    Не распознали — None (роутер ответит честным сообщением, не делая no-op)."""
    key = (offset or "").strip().lower()
    if key in _SNOOZE_NAMED:
        days, t = _SNOOZE_NAMED[key]
        result = {"due_date": (now.date() + timedelta(days=days)).isoformat()}
        if t is not None:
            result["due_time"] = t
        return result
    m = _SNOOZE_DURATION.match(key)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        if unit == "d":  # дни — смещаем дату, время не трогаем
            return {"due_date": (now.date() + timedelta(days=n)).isoformat()}
        delta = timedelta(minutes=n) if unit == "m" else timedelta(hours=n)
        target = now + delta
        return {"due_date": target.date().isoformat(), "due_time": target.strftime("%H:%M")}
    return None


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
- create_obligation — записать обязательство: чего ты ЖДЁШЬ от человека или что ТЫ кому-то должен («жду от Пети отчёт», «Маша должна мне денег» → waiting_on; «я должен Васе книгу», «надо вернуть долг Маше» → i_owe). Это про отношения с человеком, а не дело со сроком (то — create_task). Поля: title (что именно), person (кто), direction ("waiting_on" — жду от кого-то | "i_owe" — я должен), follow_up_date ("ГГГГ-ММ-ДД" — когда напомнить, или null), related_project (тема или null).
- query_obligations — показать обязательства («кто мне должен», «что я кому должен», «чего я жду от Пети»). Поля: direction ("waiting_on" | "i_owe" | null — все), person (фильтр по человеку или null).
- complete_obligation — закрыть обязательство («Петя прислал отчёт», «вернул долг Маше», «закрой обязательство …»). Поле: title_hint (по сути/человеку).
- delete_obligation — удалить запись обязательства («убери обязательство …»). Поле: title_hint.
- inbox_reclassify — переразобрать ПОСЛЕДНЮЮ запись инбокса: это не задача, а мысль на потом («это не задача, отложи подумать», «убери из задач, пусть полежит», «это на когда-нибудь»). Поле: status — куда отложить: "someday" (когда-нибудь), "needs_decision" (нужно решить), "maybe_later" (может быть потом).
- log_decision — записать ПРИНЯТОЕ решение в журнал решений («запиши решение: …», «зафиксируй: решили …», «для протокола: выбрали …»). Поле: text — исходная формулировка решения (с причиной/альтернативами, если есть).
- query_decisions — найти прошлое решение и его обоснование («почему мы отказались от X», «какие решения по Y», «что мы решили насчёт …»). Поле: text — суть вопроса.
- save_link — сохранить ссылку «на потом» / «почитать позже» / «в закладки». Срабатывает ТОЛЬКО если в сообщении есть URL И явный контекст отложенного чтения («почитаю потом», «на потом», «сохрани ссылку», «в закладки»). Просто URL без такого контекста — НЕ save_link (это обычное сообщение none). Поле: url (сам адрес).
- query_reads — показать список «почитать» («что у меня в почитать», «непрочитанные ссылки»). Полей нет.
- mark_read — отметить сохранённую ссылку прочитанной («прочитал статью про …», «отметь … прочитанным»). Поле: title_hint (по заголовку/теме ссылки).
- query_weekly_review — сводка/итоги за неделю («сводка за неделю», «что у меня было на этой неделе», «подведи итоги недели»). Полей нет.
- create_recurring_task — завести ПОВТОРЯЮЩУЮСЯ задачу/привычку («каждый день…», «по понедельникам…», «каждое 15-е число…», «напоминай еженедельно…»). Поля: title, recurrence_type ("daily"|"weekly"|"monthly"), day_of_week (0=Пн..6=Вс — для weekly, иначе null), day_of_month (1..31 — для monthly, иначе null), time ("ЧЧ:ММ" или null), project (или null). Это про регулярность; разовое дело с датой — это create_task.
- query_recurring_tasks — показать повторяющиеся задачи («какие у меня повторяющиеся задачи», «мои привычки», «что повторяется»). Полей нет.
- delete_recurring_template — удалить повторяющуюся задачу («убери повторяющуюся задачу …», «отмени привычку …»). Поле: title_hint.
- snooze — отложить ПОСЛЕДНЕЕ действие/задачу на потом, не повторяя его («отложи на вечер», «напомни через 2 часа», «не сегодня», «перенеси на завтра», «давай попозже»). Поле: offset — относительное смещение В КАНОНИЧЕСКОЙ ФОРМЕ, одно из: "evening" (вечер), "afternoon" (день), "tonight" (поздний вечер), "morning" (утро), "tomorrow" (завтра/«не сегодня»), "next_week" (через неделю), либо длительность вида "<N>m"/"<N>h"/"<N>d" («через 2 часа»→"2h", «через 30 минут»→"30m", «через 3 дня»→"3d"). НЕ абсолютная дата — именно относительное смещение.
- undo_last — отменить последнее выполненное действие («отмени», «отмени последнее», «верни как было»). Полей нет.
- edit_last — поправить ОДНО поле у последнего действия, не повторяя его («не завтра, а в пятницу», «сделай приоритет высоким», «переименуй в …», «добавь к прошлой заметке: …»). Поля: field, value. field — что меняем: "title" (название), "priority" ("low"|"normal"|"high"), "due_date" ("ГГГГ-ММ-ДД"), "due_time" ("ЧЧ:ММ"), "name" (имя контакта), "birthday" ("ГГГГ-ММ-ДД"), "note" (дописать к заметке контакта). value — новое значение (даты/время — в абсолютном виде, относительно «сегодня»).
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
- is_action_request: поставь true, ТОЛЬКО когда пользователь просит ВЫПОЛНИТЬ действие (создать/записать/изменить/удалить что-то), но НИ ОДИН intent выше не подходит — например, разом завести несколько платежей (отдельной команды нет). Тогда верни {{"intent": "none", "is_action_request": true}}: бот честно откажет, а не сымитирует выполнение. Для обычного разговора, вопроса или болтовни — false.

Верни JSON строго такого вида (лишние поля оставляй null):
{{"intent": "...", "confidence": "high|low", "is_action_request": false, "title": null, "due_date": null, "due_time": null, "priority": "normal", "title_hint": null, "name_hint": null, "filter": null, "date": null, "start_time": null, "end_time": null, "project": null, "note": null, "name": null, "birthday": null, "url": null, "field": null, "value": null, "recurrence_type": null, "day_of_week": null, "day_of_month": null, "time": null, "offset": null, "person": null, "direction": null, "follow_up_date": null, "related_project": null, "status": null, "text": null}}

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
    raw_intent = data.get("intent")
    if raw_intent not in INTENTS:
        # Имя intent не из набора. Если это непустая строка (не "none") — модель
        # пыталась выдать команду, которой мы не поддерживаем (как create_bill для
        # массового создания платежей, §3): помечаем unrecognized, чтобы хендлер
        # честно отказал, а не уходил в chat (иначе chat сконфабулирует «сделал»).
        # Пустой intent / сбой парсинга JSON — это просто болтовня (unrecognized=False).
        unrecognized = isinstance(raw_intent, str) and raw_intent.strip() not in ("", "none")
        return {"intent": "none", "confidence": "high", "unrecognized": unrecognized}
    data["confidence"] = "low" if str(data.get("confidence", "")).strip().lower() == "low" else "high"
    # (2) is_action_request: модель сама помечает «просьба о действии без подходящего
    # intent» (напр. массовое создание платежей — отдельной команды нет). Нормализуем
    # к bool (модель могла прислать строку "true"/"false"). Реальный Gemini для таких
    # сообщений отдаёт чистый none, поэтому unrecognized не срабатывает — нужен явный флаг.
    data["is_action_request"] = str(data.get("is_action_request")).strip().lower() == "true"
    return data


def route_after_resolve(intent_data: dict, resolution_kind: str) -> str:
    """Решает, как обработать сообщение после Router.resolve. §(б).

    Возвращает:
      "handle" — резолв дал действие/ответ (execute/confirm/message): обрабатываем штатно;
      "refuse" — это была команда, которой нет: честный отказ, НЕ chat. Два источника:
                 unrecognized (модель выдала выдуманное имя intent) ИЛИ is_action_request
                 (модель явно пометила «просьба о действии без подходящего intent», §(б)/(2));
      "chat"   — обычная болтовня (genuine none): идём в chat/memory-пайплайн.

    Различие refuse/chat и есть лечение конфабуляции: chat-пайплайн без доступа к
    сторам не должен «подтверждать» действие, которое мы вообще не умеем выполнять.
    """
    if resolution_kind != "chat":
        return "handle"
    if intent_data.get("unrecognized") or intent_data.get("is_action_request"):
        return "refuse"
    return "chat"


# (1) Детерминированный выходной guard chat-пайплайна — последняя линия защиты §(б).
# Глаголы-«я выполнил действие со стором», которых на chat-пути быть не может: бот
# без доступа к задачам/платежам/календарю их не совершал. Ловим ПРОШЕДШЕЕ совершённое
# («записал», «создал»), а не инфинитив («не могу записать» — это честный отказ, не
# трогаем) и не второе лицо («ты добавил» — про пользователя). Плюс ложное обещание
# «напомню тебе о …» (никакого напоминания не заведено). Память («запомнил») сюда НЕ
# входит — её реально пишет facts-экстрактор отдельным путём, это легитимно.
_ACTION_CLAIM = re.compile(
    r"\b("
    r"записал[ао]?|создал[ао]?|добавил[ао]?|отметил[ао]?|"
    r"занёс|занес|занесл[ао]|внёс|внес|внесл[ао]|"
    r"сохранил[ао]?|запланировал[ао]?|перенёс|перенес|перенесл[ао]|удалил[ао]?"
    r")\b"
    r"|напомню тебе о",
    re.IGNORECASE,
)
# Второе лицо: если оно стоит в предложении ДО глагола — действие приписано
# пользователю («ты уже добавил»), а не боту. Это не имитация, не трогаем.
_SECOND_PERSON = re.compile(r"\b(ты|вы|тебе|тобой|вами)\b", re.IGNORECASE)

CHAT_GUARD_REFUSAL = (
    "Честно говоря, я не могу сам выполнить это действие — в режиме разговора у меня "
    "нет доступа к задачам, платежам и календарю, поэтому ничего не записал. "
    "Сделай это поддерживаемой командой или вручную."
)


def guard_chat_answer(answer: str) -> str:
    """§(б), уровень (1): если ответ модели УТВЕРЖДАЕТ, что выполнил действие со
    стором (а на chat-пути доступа к сторам нет — значит это конфабуляция), заменяем
    его честным отказом. Детерминированно и независимо от того, сработал ли сигнал
    is_action_request (2): последняя линия защиты, не полагается на послушание LLM.

    Разбираем по предложениям: глагол совершённого действия считаем имитацией, только
    если перед ним в этом же предложении нет второго лица («ты добавил» — про юзера)."""
    for sentence in re.split(r"(?<=[.!?])\s+", answer):
        m = _ACTION_CLAIM.search(sentence)
        if m is None:
            continue
        if _SECOND_PERSON.search(sentence[: m.start()]):
            continue  # действие приписано пользователю, а не боту
        return CHAT_GUARD_REFUSAL
    return answer


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


def format_obligations(items: list[dict], header: str) -> str:
    """Список обязательств (§19.1): кто/что + направление + дата follow-up."""
    if not items:
        return "Обязательств нет."
    arrows = {"waiting_on": "⏳ жду от", "i_owe": "💸 я должен"}
    lines = [header, ""]
    for o in items:
        head = arrows.get(o["direction"], o["direction"])
        fu = f" · напомнить {o['follow_up_date']}" if o["follow_up_date"] else ""
        lines.append(f"{head} {o['person']}: {o['title']}{fu}")
    return "\n".join(lines)


_WEEKDAYS_RU = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


def format_recurring(items: list[dict]) -> str:
    """Список активных шаблонов повторяющихся задач (§18.2)."""
    if not items:
        return "Повторяющихся задач нет."
    lines = ["🔁 Повторяющиеся задачи:", ""]
    for t in items:
        rtype = t["recurrence_type"]
        if rtype == "weekly" and t["day_of_week"] is not None:
            when = f"еженедельно ({_WEEKDAYS_RU[t['day_of_week']]})"
        elif rtype == "monthly" and t["day_of_month"] is not None:
            when = f"ежемесячно ({t['day_of_month']}-го)"
        else:
            when = {"daily": "ежедневно", "weekly": "еженедельно",
                    "monthly": "ежемесячно"}.get(rtype, rtype)
        at = f" в {t['time']}" if t["time"] else ""
        lines.append(f"🔁 {t['title']} — {when}{at}")
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
                 action_log=None, inbox=None, contacts=None, reads=None, recurring=None,
                 obligations=None, decisions=None):
        self.tasks = tasks
        self.bills = bills
        self.calendar = calendar  # None если календарь не настроен
        self.log = action_log     # ActionLog | None — журнал для undo_last
        self.inbox = inbox        # InboxStore | None — быстрый захват
        self.contacts = contacts  # ContactStore | None — лёгкий CRM
        self.reads = reads        # ReadStore | None — read-it-later
        self.recurring = recurring  # RecurringTaskStore | None — повторяющиеся задачи
        self.obligations = obligations  # ObligationStore | None — обязательства (§19.1)
        self.decisions = decisions  # DecisionLogger | None — журнал решений (§19.3, исполняет хендлер)

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

        if intent == "edit_last":
            return self._resolve_edit_last(data)

        if intent == "snooze":
            return self._resolve_snooze(data, low)

        if intent == "create_task":
            title = str(data.get("title") or "").strip()
            if not title:
                return Resolution("chat")  # нечего создавать — пусть отвечает обычно
            params = {"title": title, "priority": data.get("priority") or "normal", "source": "telegram"}
            for key in ("due_date", "due_time", "project"):
                if data.get(key):
                    params[key] = data[key]
            return self._gate(
                intent, {"type": "create_task", "params": params}, f"создать задачу «{title}»?", low
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
                return self._gate(
                    intent,
                    {"type": "complete_task", "task_id": task["id"], "title": task["title"]},
                    f"отметить выполненной «{task['title']}»?",
                    low,
                )
            # delete_task — dangerous: всегда с подтверждением, независимо от confidence
            return self._gate(
                intent,
                {"type": "delete_task", "task_id": task["id"], "title": task["title"]},
                f"удалить задачу «{task['title']}»?",
                low,
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
            return self._gate(
                intent,
                {"type": "mark_bill_paid", "instance_id": bill["id"], "name": bill["name"]},
                f"отметить платёж «{bill['name']}» оплаченным?",
                low,
            )

        if intent == "query_tasks":
            return self._gate(
                intent, {"type": "query_tasks", "filter": data.get("filter")}, "показать задачи?", low
            )

        if intent == "query_bills":
            return self._gate(intent, {"type": "query_bills"}, "показать платежи?", low)

        if intent == "query_by_project":
            project = str(data.get("project") or "").strip()
            if not project:
                return Resolution("chat")  # без темы — обычный разговор
            return self._gate(intent, {"type": "query_by_project", "project": project}, "", low)

        if intent == "capture":
            note = str(data.get("note") or "").strip()
            if not note:
                return Resolution("chat")  # нечего записывать
            return self._gate(
                intent, {"type": "capture", "text": note}, f"записать в инбокс «{note}»?", low
            )

        if intent in ("create_contact", "update_contact", "query_contacts", "delete_contact"):
            if self.contacts is None:
                return Resolution("message", text="Контакты не настроены 🤔")
            return self._resolve_contact(intent, data, low)

        if intent in ("create_obligation", "query_obligations",
                      "complete_obligation", "delete_obligation"):
            if self.obligations is None:
                return Resolution("message", text="Обязательства не настроены 🤔")
            return self._resolve_obligation(intent, data, low)

        if intent == "inbox_reclassify":
            return self._resolve_inbox_reclassify(data, low)

        if intent in ("log_decision", "query_decisions"):
            # Обе операции требуют LLM/память — роутер лишь гейтит их safe и
            # отдаёт текст; реальную работу делает хендлер (как save_link/weekly).
            text = str(data.get("text") or data.get("note") or "").strip()
            return self._gate(intent, {"type": intent, "text": text}, "", low)

        if intent in ("save_link", "query_reads", "mark_read"):
            if self.reads is None:
                return Resolution("message", text="Read-it-later не настроен 🤔")
            return self._resolve_read(intent, data, low)

        if intent == "query_weekly_review":
            # read-only (safe); расчёт+саммари делает хендлер (у него есть LLM)
            return self._gate(intent, {"type": "query_weekly_review"}, "", low)

        if intent in ("create_recurring_task", "query_recurring_tasks", "delete_recurring_template"):
            if self.recurring is None:
                return Resolution("message", text="Повторяющиеся задачи не настроены 🤔")
            return self._resolve_recurring(intent, data, low)

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
    def _gate(intent: str, action: dict, label: str, low: bool) -> Resolution:
        """Единый шлюз execute/confirm по таблице рисков RISK_LEVELS (§17):
        safe → сразу; medium → сразу, если confidence != low; dangerous → всегда
        подтверждение. Неизвестный intent трактуем как medium (осторожнее)."""
        risk = RISK_LEVELS.get(intent, "medium")
        if risk == "dangerous" or (risk == "medium" and low):
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
            return self._gate(
                intent, {"type": "create_contact", "params": params}, f"добавить контакт «{name}»?", low
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
            return self._gate(
                intent,
                {"type": "update_contact", "contact_id": single["id"],
                 "name": single["name"], "fields": fields},
                f"обновить контакт «{single['name']}»?", low,
            )

        if intent == "delete_contact":
            hint = data.get("name_hint") or data.get("name")
            single = self._single_contact(self.contacts.find(hint), hint)
            if isinstance(single, Resolution):
                return single
            return self._gate(
                intent,
                {"type": "delete_contact", "contact_id": single["id"], "name": single["name"]},
                f"удалить контакт «{single['name']}»?", low,
            )

        if intent == "query_contacts":
            return self._gate(intent, {"type": "query_contacts",
                                       "filter": data.get("filter"),
                                       "name": data.get("name")}, "", low)
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
        if kind == "edit_contact":  # edit_last: правка поля последнего контакта (логируется как update)
            after = self.contacts.update(action["contact_id"], **action["fields"])
            return f"✏️ Изменил контакт «{(after or {}).get('name', action['name'])}»", \
                str(action["contact_id"]), after, True
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

    # --- Обязательства (§19.1) -----------------------------------------------
    # create — auto-or-confirm (medium); query — read-only (safe); complete —
    # medium по подстроке среди открытых; delete — всегда Да/Нет (dangerous).
    # Все мутации логируются (entity_type=obligation) и отменяемы через undo_last.

    _DIRECTIONS = {"waiting_on", "i_owe"}

    def _resolve_obligation(self, intent: str, data: dict, low: bool) -> Resolution:
        assert self.obligations is not None  # resolve() гарантирует стор до вызова

        if intent == "create_obligation":
            title = str(data.get("title") or "").strip()
            person = str(data.get("person") or "").strip()
            if not title or not person:
                return Resolution("chat")  # некого/нечего ждать — обычный разговор
            direction = str(data.get("direction") or "").strip().lower()
            if direction not in self._DIRECTIONS:
                direction = "waiting_on"
            params = {"title": title, "person": person, "direction": direction}
            for key in ("since_date", "follow_up_date", "related_project"):
                if data.get(key):
                    params[key] = data[key]
            verb = "жду от" if direction == "waiting_on" else "должен"
            return self._gate(
                intent, {"type": "create_obligation", "params": params},
                f"записать: {verb} {person} — «{title}»?", low,
            )

        if intent == "query_obligations":
            direction = str(data.get("direction") or "").strip().lower() or None
            if direction not in self._DIRECTIONS:
                direction = None
            return self._gate(intent, {"type": "query_obligations", "direction": direction,
                                       "person": data.get("person")}, "", low)

        # complete/delete — поиск по title_hint среди открытых обязательств
        hint = data.get("title_hint") or data.get("title")
        matches = self.obligations.find(hint, status="open")
        if not matches:
            return Resolution("message", text=f"Не нашёл открытое обязательство похожее на «{(hint or '').strip()}».")
        if len(matches) > 1:
            titles = ", ".join(f"«{o['title']}»" for o in matches[:5])
            return Resolution("message", text=f"Нашёл несколько обязательств: {titles}. Уточни, какое именно.")
        o = matches[0]
        if intent == "complete_obligation":
            return self._gate(
                intent, {"type": "complete_obligation", "obligation_id": o["id"], "title": o["title"]},
                f"закрыть обязательство «{o['title']}»?", low,
            )
        return self._gate(  # delete_obligation — dangerous: всегда Да/Нет
            intent, {"type": "delete_obligation", "obligation_id": o["id"], "title": o["title"]},
            f"удалить обязательство «{o['title']}»?", low,
        )

    def _apply_obligation(self, action: dict) -> tuple[str, str | None, dict | None, bool]:
        """Действия над обязательствами. Сюда не попадаем без настроенного стора."""
        assert self.obligations is not None
        kind = action["type"]
        if kind == "create_obligation":
            o = self.obligations.create(**action["params"])
            verb = "Жду от" if o["direction"] == "waiting_on" else "Должен"
            return f"📌 {verb} {o['person']}: «{o['title']}»", str(o["id"]), o, True
        if kind == "complete_obligation":
            after = self.obligations.update(action["obligation_id"], status="done")
            return f"✅ Закрыл обязательство: «{action['title']}»", str(action["obligation_id"]), after, True
        if kind == "restore_obligation":  # реверс undo: вернуть прежние поля
            after = self.obligations.update(action["obligation_id"], **action["fields"])
            return f"↩️ Вернул обязательство «{action['title']}»", str(action["obligation_id"]), after, True
        if kind == "delete_obligation":
            self.obligations.delete(action["obligation_id"])
            return f"🗑 Удалил обязательство: «{action['title']}»", str(action["obligation_id"]), None, True
        # query_obligations — read-only
        items = self.obligations.list(direction=action.get("direction"),
                                      person=action.get("person"), status="open")
        return format_obligations(items, "📌 Открытые обязательства:"), None, None, False

    # --- Переклассификация инбокса (inbox_reclassify, §19.2) -----------------
    # Тем же приёмом, что edit_last/snooze: берём latest_active() из журнала; если
    # это запись инбокса — меняем её статус разбора через обычный update-путь
    # (логируется как update, отменяемо через undo_last). Risk medium (как edit).

    def _resolve_inbox_reclassify(self, data: dict, low: bool) -> Resolution:
        if self.log is None:
            return Resolution("message", text="Журнал действий не ведётся — нечего переразбирать.")
        if self.inbox is None:
            return Resolution("message", text="Инбокс не настроен 🤔")
        rec = self.log.latest_active()
        if rec is None or rec["entity_type"] != "inbox":
            return Resolution("message", text="Последнее действие — не запись инбокса 🤔")
        status = str(data.get("status") or "").strip().lower()
        if status not in INBOX_REVIEW_STATUSES:
            return Resolution("message", text="Не понял, куда отложить (когда-нибудь / нужно решить / может быть потом) 🤔")
        label = INBOX_REVIEW_STATUSES[status]
        return self._gate(
            "inbox_reclassify",
            {"type": "reclassify_inbox", "item_id": int(rec["entity_id"]), "status": status},
            f"отложить запись инбокса в «{label}»?", low,
        )

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
            return self._gate(intent, {"type": "save_link", "params": {"url": url}}, "", low)

        if intent == "query_reads":
            return self._gate(intent, {"type": "query_reads"}, "", low)

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
            return self._gate(
                intent,
                {"type": "mark_read", "read_id": r["id"], "title": r["title"] or r["url"]},
                f"отметить прочитанной «{r['title'] or r['url']}»?", low,
            )
        return Resolution("chat")

    # --- Повторяющиеся задачи (§18.2) ----------------------------------------
    # create — auto-or-confirm (medium); query — read-only (safe); delete —
    # всегда Да/Нет (dangerous). Шаблоны в журнал не пишутся (как bill_templates):
    # это конфигурация повторяемости, а не разовая мутация под undo.

    _RECURRENCE_TYPES = {"daily", "weekly", "monthly"}

    def _resolve_recurring(self, intent: str, data: dict, low: bool) -> Resolution:
        assert self.recurring is not None  # resolve() гарантирует стор до вызова

        if intent == "create_recurring_task":
            title = str(data.get("title") or "").strip()
            rtype = str(data.get("recurrence_type") or "").strip().lower()
            if not title or rtype not in self._RECURRENCE_TYPES:
                return Resolution("chat")  # нечего/непонятно как повторять — обычный чат
            params = {"title": title, "recurrence_type": rtype}
            for key in ("day_of_week", "day_of_month", "time", "project"):
                if data.get(key) is not None:
                    params[key] = data[key]
            return self._gate(
                intent, {"type": "create_recurring_template", "params": params},
                f"создать повторяющуюся задачу «{title}» ({rtype})?", low,
            )

        if intent == "query_recurring_tasks":
            return self._gate(intent, {"type": "query_recurring_tasks"}, "", low)

        if intent == "delete_recurring_template":
            hint = (data.get("title_hint") or data.get("title") or "").strip().lower()
            matches = [t for t in self.recurring.list_templates()
                       if hint and hint in t["title"].lower()]
            if not matches:
                return Resolution("message", text=f"Не нашёл повторяющуюся задачу похожую на «{hint}».")
            if len(matches) > 1:
                titles = ", ".join(f"«{t['title']}»" for t in matches[:5])
                return Resolution("message", text=f"Нашёл несколько: {titles}. Уточни, какую именно.")
            t = matches[0]
            return self._gate(
                intent,
                {"type": "delete_recurring_template", "template_id": t["id"], "title": t["title"]},
                f"удалить повторяющуюся задачу «{t['title']}»?", low,
            )
        return Resolution("chat")

    def _apply_recurring(self, action: dict) -> tuple[str, str | None, dict | None, bool]:
        """Действия над шаблонами повторяющихся задач. В журнал не пишутся
        (нет в _LOGGED), поэтому entity_id/after не важны — возвращаем как read-only."""
        assert self.recurring is not None
        kind = action["type"]
        if kind == "create_recurring_template":
            t = self.recurring.create_template(**action["params"])
            return f"🔁 Создал повторяющуюся задачу: «{t['title']}» ({t['recurrence_type']})", \
                None, None, False
        if kind == "delete_recurring_template":
            self.recurring.delete_template(action["template_id"])
            return f"🗑 Удалил повторяющуюся задачу: «{action['title']}»", None, None, False
        # query_recurring_tasks — read-only
        return format_recurring(self.recurring.list_templates(active_only=True)), None, None, False

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

    # --- Правка последнего действия (edit_last, §17) -------------------------
    # Берём ту же latest_active(), что и undo, и правим ОДНО поле найденной
    # сущности через обычный update-путь — значит правка логируется как update и
    # отменяема через undo_last. Неприменимое поле → честное сообщение, не no-op.

    def _resolve_edit_last(self, data: dict) -> Resolution:
        if self.log is None:
            return Resolution("message", text="Журнал действий не ведётся — нечего править.")
        rec = self.log.latest_active()
        if rec is None:
            return Resolution("message", text="Нет последнего действия, которое можно изменить.")
        field = str(data.get("field") or "").strip()
        value = data.get("value")
        spec = _EDIT_LAST_FIELDS.get(rec["entity_type"], {}).get(field)
        if spec is None or value is None or str(value).strip() == "":
            return Resolution("message", text="К последнему действию это изменение не подходит 🤔")
        action = self._build_edit(rec, *spec, value)
        if action is None:  # сущность есть, но править её этим путём нечем
            return Resolution("message", text="К последнему действию это изменение не подходит 🤔")
        return Resolution("execute", action=action)

    # --- Отложить последнее действие (snooze/defer, §18.1) -------------------
    # Тот же latest_active(), что и edit_last, но value не произвольный, а
    # предустановленный нормализатором offset → due_date/due_time. Применимо
    # только к задаче; строит обычный edit_task (логируется как update, отменяемо
    # через undo_last). Risk medium (как edit) — сразу при confidence != low.

    def _resolve_snooze(self, data: dict, low: bool) -> Resolution:
        if self.log is None:
            return Resolution("message", text="Журнал действий не ведётся — нечего откладывать.")
        rec = self.log.latest_active()
        if rec is None:
            return Resolution("message", text="Нет последнего действия, которое можно отложить.")
        fields = normalize_snooze_offset(str(data.get("offset") or ""), datetime.now())
        if not fields:
            return Resolution("message", text="Не понял, на когда отложить 🤔")
        if rec["entity_type"] != "task":
            return Resolution("message", text="Отложить можно только задачу/напоминание 🤔")
        title = (rec.get("after_state") or rec.get("before_state") or {}).get("title", "")
        action = {"type": "edit_task", "task_id": int(rec["entity_id"]),
                  "fields": fields, "title": title}
        when = fields["due_date"] + (f" {fields['due_time']}" if fields.get("due_time") else "")
        return self._gate("snooze", action, f"отложить «{title}» на {when}?", low)

    def _build_edit(self, rec: dict, column: str, mode: str, value) -> dict | None:
        """Действие-обновление для edit_last: подставляет новое значение поля
        последней сущности. mode='append' дописывает к текущему тексту (через \\n)."""
        et, eid = rec["entity_type"], rec["entity_id"]
        value = str(value).strip()
        if et == "task":
            title = (rec.get("after_state") or rec.get("before_state") or {}).get("title", "")
            return {"type": "edit_task", "task_id": int(eid),
                    "fields": {column: value}, "title": title}
        if et == "contact":
            if self.contacts is None:
                return None  # контакты отключены — честно не умеем
            if mode == "append":
                existing = ((self.contacts.get(int(eid)) or {}).get(column) or "").strip()
                value = f"{existing}\n{value}" if existing else value
            name = (rec.get("after_state") or rec.get("before_state") or {}).get("name", "")
            return {"type": "edit_contact", "contact_id": int(eid),
                    "fields": {column: value}, "name": name}
        return None

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

        if et == "obligation":
            if act == "create":  # создали → удаляем
                return {"type": "delete_obligation", "obligation_id": int(eid),
                        "title": (after or {}).get("title", "")}
            if act == "update" and before is not None:  # закрыли/изменили → восстановить before
                fields = {k: before.get(k) for k in
                          ("title", "person", "direction", "since_date",
                           "follow_up_date", "status", "related_project")}
                return {"type": "restore_obligation", "obligation_id": int(eid),
                        "fields": fields, "title": before.get("title", "")}
            if act == "delete" and before is not None:  # удалили → создаём заново (новый id — ок)
                params = {k: before[k] for k in
                          ("title", "person", "direction", "since_date",
                           "follow_up_date", "related_project", "source")
                          if before.get(k) is not None}
                return {"type": "create_obligation", "params": params}

        if et == "inbox":
            if act == "create":  # захватили мысль → удаляем запись
                return {"type": "delete_inbox", "item_id": int(eid)}
            if act == "update" and before is not None:  # переклассифицировали → вернуть прежний статус
                return {"type": "reclassify_inbox", "item_id": int(eid),
                        "status": before.get("status", "pending")}

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

        # Изменения календаря — dangerous, query_events — safe; решает _gate по
        # таблице рисков (low здесь не влияет: dangerous всегда confirm).
        if intent == "query_events":
            return self._gate(intent, {"type": "query_events", "filter": data.get("filter")}, "", False)

        if intent == "create_event":
            title = str(data.get("title") or "").strip()
            start = self._build_dt(data.get("date"), data.get("start_time"), tz)
            if not title or start is None:
                return Resolution("chat")  # нечего/некуда создавать — обычный чат
            end = self._build_dt(data.get("date"), data.get("end_time"), tz) or (start + timedelta(hours=1))
            label = f"создать встречу «{title}» {self._fmt_span(start, end)}?"
            label += self._conflict_suffix(start, end)
            return self._gate(
                intent,
                {"type": "create_event", "title": title,
                 "start": start.isoformat(), "end": end.isoformat()},
                label, False,
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
            return self._gate(
                intent,
                {"type": "move_event", "event_id": single["id"], "title": single["title"],
                 "start": start.isoformat(), "end": end.isoformat()},
                label, False,
            )

        if intent == "delete_event":
            hint = data.get("title_hint") or data.get("title")
            matches = self._find_events(hint)
            single = self._single_event(matches, hint)
            if isinstance(single, Resolution):
                return single
            return self._gate(
                intent,
                {"type": "delete_event", "event_id": single["id"], "title": single["title"]},
                f"удалить встречу «{single['title']}» ({self._fmt_span(single['start'], single['end'])})?",
                False,
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
        if kind == "edit_task":  # edit_last: правка поля последней задачи (логируется как update)
            after = self.tasks.update(action["task_id"], **action["fields"])
            return f"✏️ Изменил задачу «{after['title']}»", str(action["task_id"]), after, True
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
            return f"📥 Записал в инбокс: «{item['text']}»", str(item["id"]), item, True
        if kind == "reclassify_inbox":  # inbox_reclassify / реверс undo — смена статуса разбора
            assert self.inbox is not None
            after = self.inbox.set_status(action["item_id"], action["status"])
            label = INBOX_REVIEW_STATUSES.get(action["status"], action["status"])
            return f"🗂 Отложил в «{label}»", str(action["item_id"]), after, True
        if kind == "delete_inbox":  # реверс undo capture
            assert self.inbox is not None
            self.inbox.delete(action["item_id"])
            return "↩️ Убрал запись из инбокса", str(action["item_id"]), None, True
        if kind in ("create_contact", "update_contact", "edit_contact", "restore_contact",
                    "delete_contact", "query_contacts"):
            return self._apply_contact(action)
        if kind in ("save_link", "mark_read", "restore_read", "delete_read", "query_reads"):
            return self._apply_read(action)
        if kind in ("create_obligation", "complete_obligation", "restore_obligation",
                    "delete_obligation", "query_obligations"):
            return self._apply_obligation(action)
        if kind in ("create_recurring_template", "delete_recurring_template", "query_recurring_tasks"):
            return self._apply_recurring(action)
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
        if entity_type == "obligation":
            assert self.obligations is not None
            return self.obligations.get(action["obligation_id"])
        if entity_type == "inbox":
            assert self.inbox is not None
            return self.inbox.get(int(action["item_id"]))
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
