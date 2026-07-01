"""Тесты чекбоксов в списках задач/платежей (эргономика бота, п.4):

Когда бот показывает список задач или платежей (query_tasks/query_bills или
/bills), под первыми 8 позициями — инлайн-кнопка отметить выполненным/оплаченным.
Кнопка задач — новый TASK_DONE_PREFIX/mark_task_done, паттерн 1-в-1 как уже
существующий mark_paid (bills)/inbox_to_task: распарсить id из callback_data,
выполнить через IntentRouter.execute (журналируется, отменяемо), убрать нажатую
кнопку из клавиатуры. Позиций больше 8 — остаток не рендерим кнопками, только
пометка «…и ещё N». Сеть/LLM не дёргаем.
"""
import asyncio
import sqlite3
from datetime import date
from types import SimpleNamespace

import pytest

import config
from bills import BillStore
from bot.handlers import (
    MAX_LIST_ITEMS,
    TASK_DONE_PREFIX,
    Handlers,
    cap_list,
    tasks_markup,
)
from inbox import InboxStore
from logger import ActionLog
from tasks import TaskStore


def _set(db_path, table, column, value, row_id):
    """Напрямую правит поле в SQLite — для детерминированного backdating
    updated_at в тестах (тот же паттерн, что в test_weekly_review.py)."""
    con = sqlite3.connect(db_path)
    con.execute(f"UPDATE {table} SET {column} = ? WHERE id = ?", (value, row_id))
    con.commit()
    con.close()


@pytest.fixture(autouse=True)
def _no_user_restriction(monkeypatch):
    monkeypatch.setattr(config, "ALLOWED_USER_ID", None)


# --- cap_list: чистая функция -------------------------------------------------

def test_cap_list_returns_all_and_zero_extra_when_within_limit():
    items = list(range(5))
    shown, extra = cap_list(items)
    assert shown == items
    assert extra == 0


def test_cap_list_caps_at_max_and_reports_extra():
    items = list(range(11))
    shown, extra = cap_list(items)
    assert shown == list(range(MAX_LIST_ITEMS))
    assert extra == 11 - MAX_LIST_ITEMS


# --- tasks_markup: кнопки чекбоксов -------------------------------------------

def _task(id_, title, status="todo"):
    return {"id": id_, "title": title, "status": status, "due_date": None, "due_time": None,
            "priority": "normal"}


def test_tasks_markup_has_button_per_open_task():
    items = [_task(1, "купить молоко"), _task(2, "позвонить маме")]
    markup = tasks_markup(items)
    assert markup is not None
    callbacks = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert callbacks == [f"{TASK_DONE_PREFIX}1", f"{TASK_DONE_PREFIX}2"]


def test_tasks_markup_button_text_includes_task_title():
    """Регрессия: кнопка не должна быть голой галочкой без подписи."""
    items = [_task(1, "купить молоко")]
    markup = tasks_markup(items)
    texts = [b.text for row in markup.inline_keyboard for b in row]
    assert texts == ["☑️ купить молоко"]


def test_tasks_markup_skips_done_and_cancelled():
    items = [_task(1, "готово", status="done"), _task(2, "отменено", status="cancelled")]
    assert tasks_markup(items) is None


def test_tasks_markup_none_when_empty():
    assert tasks_markup([]) is None


# --- Handlers: _query_tasks / _query_bills / mark_task_done -------------------

class FakeMessage:
    def __init__(self):
        self.calls: list[dict] = []

    async def reply_text(self, text, parse_mode=None, **kwargs):
        self.calls.append({"text": text, "parse_mode": parse_mode, **kwargs})


class FakeUpdate:
    def __init__(self, message):
        self.message = message
        self.effective_user = None


class FakeQuery:
    def __init__(self, data, reply_markup=None):
        self.data = data
        self.message = SimpleNamespace(reply_markup=reply_markup)
        self.answers: list[str | None] = []
        self.edited_markups: list = []

    async def answer(self, text=None):
        self.answers.append(text)

    async def edit_message_reply_markup(self, reply_markup=None):
        self.edited_markups.append(reply_markup)
        self.message.reply_markup = reply_markup


class FakeCallbackUpdate:
    def __init__(self, query):
        self.callback_query = query
        self.effective_user = None


def _handlers(tmp_path):
    tasks = TaskStore(tmp_path / "t.db")
    bills = BillStore(tmp_path / "b.db")
    inbox = InboxStore(tmp_path / "i.db")
    alog = ActionLog(tmp_path / "a.db")
    h = Handlers(memory=None, llm=None, facts=None, bills=bills, tasks=tasks,
                calendar=None, action_log=alog, inbox=inbox)  # type: ignore[arg-type]
    return h, tasks, bills, inbox, alog


def test_query_tasks_shows_checkbox_for_each_open_task(tmp_path):
    h, tasks, *_ = _handlers(tmp_path)
    tasks.create("купить молоко")
    tasks.create("позвонить маме")
    message = FakeMessage()
    asyncio.run(h._query_tasks(FakeUpdate(message), {"filter": None}))
    call = message.calls[0]
    assert "купить молоко" in call["text"]
    markup = call["reply_markup"]
    assert len([b for row in markup.inline_keyboard for b in row]) == 2


def test_query_tasks_caps_list_and_notes_extra(tmp_path):
    h, tasks, *_ = _handlers(tmp_path)
    for i in range(11):
        tasks.create(f"задача {i}")
    message = FakeMessage()
    asyncio.run(h._query_tasks(FakeUpdate(message), {"filter": None}))
    call = message.calls[0]
    assert "…и ещё 3" in call["text"]
    assert len([b for row in call["reply_markup"].inline_keyboard for b in row]) == MAX_LIST_ITEMS


def test_query_tasks_empty_has_no_markup(tmp_path):
    h, *_ = _handlers(tmp_path)
    message = FakeMessage()
    asyncio.run(h._query_tasks(FakeUpdate(message), {"filter": None}))
    call = message.calls[0]
    assert "Задач нет" in call["text"]
    assert "reply_markup" not in call or call.get("reply_markup") is None


# --- _query_tasks: done-задачи без даты видны только в день выполнения ---------
# Та же логика, что на дашборде (dashboard/index.html loadTasks): done-задача
# остаётся в списке, только если updated_at == сегодня — иначе старые
# тестовые/архивные done-задачи копятся в списке навсегда.

def test_query_tasks_hides_done_task_not_updated_today(tmp_path):
    h, tasks, *_ = _handlers(tmp_path)
    fresh = tasks.create("сделано сегодня")
    tasks.update(fresh["id"], status="done")
    stale = tasks.create("сделано давно")
    tasks.update(stale["id"], status="done")
    _set(tmp_path / "t.db", "tasks", "updated_at", "2020-01-01T09:00:00+00:00", stale["id"])
    tasks.create("ещё не сделано")

    message = FakeMessage()
    asyncio.run(h._query_tasks(FakeUpdate(message), {"filter": None}))
    text = message.calls[0]["text"]
    assert "сделано сегодня" in text
    assert "сделано давно" not in text
    assert "ещё не сделано" in text


def test_query_tasks_hides_stale_done_task_even_with_due_date(tmp_path):
    """Правило применяется ко всем done-задачам, а не только без due_date —
    как и в дашборде: due_date у done-задачи игнорируется, важен updated_at."""
    h, tasks, *_ = _handlers(tmp_path)
    today = date.today().isoformat()
    stale = tasks.create("оплачено давно", due_date=today)
    tasks.update(stale["id"], status="done")
    _set(tmp_path / "t.db", "tasks", "updated_at", "2020-01-01T09:00:00+00:00", stale["id"])

    message = FakeMessage()
    asyncio.run(h._query_tasks(FakeUpdate(message), {"filter": None}))
    text = message.calls[0]["text"]
    assert "оплачено давно" not in text


def test_query_bills_shows_checkbox_for_pending(tmp_path):
    h, _, bills, *_ = _handlers(tmp_path)
    bills.create_template("Аренда", day_of_month=5, amount=100)
    message = FakeMessage()
    asyncio.run(h._query_bills(FakeUpdate(message)))
    call = message.calls[0]
    assert "Аренда" in call["text"]
    assert call["reply_markup"] is not None


def test_query_bills_caps_list_and_notes_extra(tmp_path):
    h, _, bills, *_ = _handlers(tmp_path)
    for day in range(1, 12):
        bills.create_template(f"платёж {day}", day_of_month=day, amount=10)
    message = FakeMessage()
    asyncio.run(h._query_bills(FakeUpdate(message)))
    call = message.calls[0]
    assert "…и ещё 3" in call["text"]


def test_bills_cmd_reuses_query_bills_rendering(tmp_path):
    """bills_cmd (/bills) не дублирует рендер — тот же путь, что и NL query_bills."""
    h, _, bills, *_ = _handlers(tmp_path)
    bills.create_template("Аренда", day_of_month=5, amount=100)
    message = FakeMessage()
    asyncio.run(h.bills_cmd(FakeUpdate(message), context=None))
    assert "Аренда" in message.calls[0]["text"]
    assert message.calls[0]["reply_markup"] is not None


# --- mark_task_done: паттерн callback как у mark_paid/inbox_to_task ------------

def test_mark_task_done_completes_task_and_removes_button(tmp_path):
    h, tasks, *_ = _handlers(tmp_path)
    task = tasks.create("купить молоко")
    markup = tasks_markup([task])
    query = FakeQuery(f"{TASK_DONE_PREFIX}{task['id']}", reply_markup=markup)

    asyncio.run(h.mark_task_done(FakeCallbackUpdate(query), context=None))

    assert tasks.get(task["id"])["status"] == "done"
    assert query.answers  # что-то ответили пользователю
    assert query.edited_markups[-1] is None  # кнопка убрана (была единственной)


def test_mark_task_done_is_logged_and_undoable(tmp_path):
    h, tasks, _, _, alog = _handlers(tmp_path)
    task = tasks.create("полить цветы")
    query = FakeQuery(f"{TASK_DONE_PREFIX}{task['id']}", reply_markup=tasks_markup([task]))

    asyncio.run(h.mark_task_done(FakeCallbackUpdate(query), context=None))
    rec = alog.latest_active()
    assert rec["entity_type"] == "task" and rec["action"] == "update"

    undo = h.router.resolve({"intent": "undo_last"})
    h.router.execute(undo.action)
    assert tasks.get(task["id"])["status"] != "done"


def test_mark_task_done_already_done_answers_gracefully(tmp_path):
    h, tasks, *_ = _handlers(tmp_path)
    task = tasks.create("уже готово")
    tasks.update(task["id"], status="done")
    query = FakeQuery(f"{TASK_DONE_PREFIX}{task['id']}")

    asyncio.run(h.mark_task_done(FakeCallbackUpdate(query), context=None))
    assert "уже" in (query.answers[-1] or "").lower()


def test_mark_task_done_unknown_task_answers_gracefully(tmp_path):
    h, *_ = _handlers(tmp_path)
    query = FakeQuery(f"{TASK_DONE_PREFIX}999")
    asyncio.run(h.mark_task_done(FakeCallbackUpdate(query), context=None))
    assert query.answers  # ответили, не упали
