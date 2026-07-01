"""Тесты команды /today: снимок дня одним сообщением.

build_today_snapshot — чистая функция форматирования (те же сигналы, что в
build_reminder_text/dashboard now-strip: ближайшая встреча, задачи на сегодня,
платёж в ближайшие 3 дня) + счётчик инбокса. Handlers.today_cmd — интеграционный
тест со стором задач/платежей/инбокса на tmp_path (сеть/календарь/LLM не дёргаем,
calendar=None — модуль его и так не трогает без настроенного календаря).
"""
import asyncio
from datetime import date, timedelta

import pytest

import config
from bot.handlers import Handlers, build_today_snapshot
from bills import BillStore
from inbox import InboxStore
from tasks import TaskStore


@pytest.fixture(autouse=True)
def _no_user_restriction(monkeypatch):
    # today_cmd проходит через _allowed(update) — в проде ограничено ALLOWED_USER_ID,
    # в тестах фейковый Update без effective_user, поэтому снимаем ограничение.
    monkeypatch.setattr(config, "ALLOWED_USER_ID", None)


# --- build_today_snapshot: чистое форматирование -----------------------------

def test_snapshot_all_empty_is_honest_calm_message():
    text = build_today_snapshot(next_meeting=None, tasks_today=0, tasks_done=0,
                                 soon_bill=None, inbox_pending=0)
    assert "Пока ничего срочного" in text
    assert "Инбокс: 0" in text


def test_snapshot_includes_next_meeting():
    text = build_today_snapshot(
        next_meeting={"time": "14:00", "title": "Встреча с Петром"},
        tasks_today=0, tasks_done=0, soon_bill=None, inbox_pending=0,
    )
    assert "14:00" in text and "Встреча с Петром" in text


def test_snapshot_shows_remaining_tasks_count():
    text = build_today_snapshot(next_meeting=None, tasks_today=5, tasks_done=2,
                                 soon_bill=None, inbox_pending=0)
    assert "3" in text


def test_snapshot_all_today_tasks_done_says_so():
    text = build_today_snapshot(next_meeting=None, tasks_today=3, tasks_done=3,
                                 soon_bill=None, inbox_pending=0)
    assert "выполнены" in text.lower()
    assert "Пока ничего срочного" not in text


def test_snapshot_includes_soon_bill():
    text = build_today_snapshot(next_meeting=None, tasks_today=0, tasks_done=0,
                                 soon_bill={"name": "Аренда", "when": "через 2 дня"},
                                 inbox_pending=0)
    assert "Аренда" in text and "через 2 дня" in text


def test_snapshot_always_shows_inbox_counter_even_when_urgent_stuff_present():
    text = build_today_snapshot(
        next_meeting={"time": "09:00", "title": "Дейлик"},
        tasks_today=1, tasks_done=0, soon_bill=None, inbox_pending=4,
    )
    assert "Инбокс: 4" in text


# --- Handlers.today_cmd: интеграция со сторами --------------------------------

class FakeMessage:
    def __init__(self):
        self.calls: list[dict] = []

    async def reply_text(self, text, parse_mode=None, **kwargs):
        self.calls.append({"text": text, "parse_mode": parse_mode, **kwargs})


class FakeUpdate:
    def __init__(self, message):
        self.message = message
        self.effective_user = None


def _handlers(tmp_path):
    tasks = TaskStore(tmp_path / "t.db")
    bills = BillStore(tmp_path / "b.db")
    inbox = InboxStore(tmp_path / "i.db")
    h = Handlers(memory=None, llm=None, facts=None, bills=bills, tasks=tasks,
                calendar=None, inbox=inbox)  # type: ignore[arg-type]
    return h, tasks, bills, inbox


def test_today_cmd_reports_honest_calm_when_nothing_going_on(tmp_path):
    h, *_ = _handlers(tmp_path)
    message = FakeMessage()
    asyncio.run(h.today_cmd(FakeUpdate(message), context=None))
    assert "Пока ничего срочного" in message.calls[0]["text"]


def test_today_cmd_counts_todays_open_tasks(tmp_path):
    h, tasks, _, _ = _handlers(tmp_path)
    today = date.today().isoformat()
    tasks.create("задача 1", due_date=today)
    done = tasks.create("задача 2", due_date=today)
    tasks.update(done["id"], status="done")
    tasks.create("без даты — тоже считается")

    message = FakeMessage()
    asyncio.run(h.today_cmd(FakeUpdate(message), context=None))
    text = message.calls[0]["text"]
    assert "2" in text  # 3 задачи на сегодня, 1 выполнена → осталось 2


def test_today_cmd_reports_bill_due_within_three_days(tmp_path):
    h, _, bills, _ = _handlers(tmp_path)
    due_in_2 = date.today() + timedelta(days=2)
    bills.create_template("Аренда", day_of_month=due_in_2.day, amount=100)

    message = FakeMessage()
    asyncio.run(h.today_cmd(FakeUpdate(message), context=None))
    text = message.calls[0]["text"]
    assert "Аренда" in text


def test_today_cmd_ignores_bill_due_in_more_than_three_days(tmp_path):
    h, _, bills, _ = _handlers(tmp_path)
    due_in_10 = date.today() + timedelta(days=10)
    bills.create_template("Интернет", day_of_month=due_in_10.day, amount=50)

    message = FakeMessage()
    asyncio.run(h.today_cmd(FakeUpdate(message), context=None))
    text = message.calls[0]["text"]
    assert "Интернет" not in text


def test_today_cmd_reports_inbox_pending_count(tmp_path):
    h, _, _, inbox = _handlers(tmp_path)
    inbox.create("мысль 1")
    inbox.create("мысль 2")

    message = FakeMessage()
    asyncio.run(h.today_cmd(FakeUpdate(message), context=None))
    assert "Инбокс: 2" in message.calls[0]["text"]
