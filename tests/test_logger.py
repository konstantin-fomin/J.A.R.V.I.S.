"""Тесты журнала действий (ActionLog) и отмены последнего действия (undo_last).

ActionLog тестируется как самостоятельное SQLite-хранилище (в стиле tasks.py),
а интеграция отмены — через IntentRouter с реальными TaskStore/BillStore на
временных базах. Календарь не дёргает сеть — подменяем FakeCal.
"""
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from bills import BillStore, current_month
from intents import IntentRouter
from logger import ActionLog
from tasks import TaskStore

TZ = ZoneInfo("Europe/Moscow")


def dt(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=TZ)


# --- ActionLog как хранилище ------------------------------------------------

@pytest.fixture
def log(tmp_path):
    return ActionLog(tmp_path / "actions.db")


def test_log_action_returns_active_record(log):
    rec = log.log_action(
        source="telegram", entity_type="task", entity_id="1", action="create",
        before_state=None, after_state={"title": "X"}, raw_message="добавь X",
    )
    assert rec["status"] == "active"
    assert rec["entity_type"] == "task"
    assert rec["action"] == "create"


def test_latest_active_none_when_empty(log):
    assert log.latest_active() is None


def test_latest_active_returns_most_recent(log):
    log.log_action(source="telegram", entity_type="task", entity_id="1",
                   action="create", before_state=None, after_state={"title": "A"})
    b = log.log_action(source="telegram", entity_type="task", entity_id="2",
                       action="create", before_state=None, after_state={"title": "B"})
    assert log.latest_active()["id"] == b["id"]


def test_mark_undone_excludes_from_latest_active(log):
    a = log.log_action(source="telegram", entity_type="task", entity_id="1",
                       action="create", before_state=None, after_state={"title": "A"})
    b = log.log_action(source="telegram", entity_type="task", entity_id="2",
                       action="create", before_state=None, after_state={"title": "B"})
    log.mark_undone(b["id"])
    assert log.latest_active()["id"] == a["id"]


def test_before_after_state_roundtrip_json(log):
    rec = log.log_action(source="telegram", entity_type="task", entity_id="1",
                         action="update", before_state={"status": "todo"},
                         after_state={"status": "done"})
    fetched = log.latest_active()
    assert fetched["before_state"] == {"status": "todo"}
    assert fetched["after_state"] == {"status": "done"}
    assert rec["before_state"] == {"status": "todo"}


# --- интеграция отмены через IntentRouter (task/bill) -----------------------

@pytest.fixture
def router(tmp_path):
    tasks = TaskStore(tmp_path / "t.db")
    bills = BillStore(tmp_path / "b.db")
    alog = ActionLog(tmp_path / "a.db")
    r = IntentRouter(tasks, bills, calendar=None, action_log=alog)
    return r, tasks, bills, alog


def test_create_then_undo_removes_task(router):
    r, tasks, _, alog = router
    r.execute({"type": "create_task", "params": {"title": "полить кактус"}})
    assert len(tasks.list()) == 1

    res = r.resolve({"intent": "undo_last", "confidence": "high"})
    assert res.kind == "execute"
    r.execute(res.action)

    assert tasks.list() == []
    assert alog.latest_active() is None  # запись помечена undone


def test_delete_then_undo_restores_task_content(router):
    r, tasks, _, _ = router
    t = tasks.create(title="купить молоко", priority="high", due_date="2026-07-01")
    r.execute({"type": "delete_task", "task_id": t["id"], "title": t["title"]})
    assert tasks.list() == []

    res = r.resolve({"intent": "undo_last"})
    r.execute(res.action)

    items = tasks.list()
    assert len(items) == 1
    assert items[0]["title"] == "купить молоко"
    assert items[0]["priority"] == "high"
    assert items[0]["due_date"] == "2026-07-01"


def test_complete_then_undo_returns_task_to_todo(router):
    r, tasks, _, _ = router
    t = tasks.create(title="доделать отчёт")
    r.execute({"type": "complete_task", "task_id": t["id"], "title": t["title"]})
    assert tasks.get(t["id"])["status"] == "done"

    res = r.resolve({"intent": "undo_last"})
    r.execute(res.action)

    assert tasks.get(t["id"])["status"] == "todo"


def test_two_undos_revert_different_actions(router):
    r, tasks, _, _ = router
    r.execute({"type": "create_task", "params": {"title": "задача A"}})
    r.execute({"type": "create_task", "params": {"title": "задача B"}})

    res1 = r.resolve({"intent": "undo_last"})
    r.execute(res1.action)
    res2 = r.resolve({"intent": "undo_last"})
    r.execute(res2.action)

    # Два undo откатили РАЗНЫЕ действия, а не одно дважды
    assert res1.action["task_id"] != res2.action["task_id"]
    assert tasks.list() == []


def test_undo_with_nothing_to_undo_is_message(router):
    r, *_ = router
    res = r.resolve({"intent": "undo_last"})
    assert res.kind == "message"


def test_mark_bill_paid_then_undo_sets_pending(router):
    r, _, bills, _ = router
    bills.create_template("аренда", day_of_month=5, amount=100)
    bills.ensure_month(current_month())
    inst = bills.list_instances(current_month())[0]

    r.execute({"type": "mark_bill_paid", "instance_id": inst["id"], "name": inst["name"]})
    assert bills.get_instance(inst["id"])["status"] == "paid"

    res = r.resolve({"intent": "undo_last"})
    r.execute(res.action)
    assert bills.get_instance(inst["id"])["status"] == "pending"


# --- календарь: отмена идёт через подтверждение Да/Нет ----------------------

class FakeCal:
    timezone = "Europe/Moscow"

    def __init__(self, events=None):
        self.events = list(events or [])

    def list_events(self, start, end):
        return list(self.events)

    def find_conflicts(self, start, end, ignore_event_id=None):
        return []

    def get_event(self, event_id):
        for e in self.events:
            if e["id"] == event_id:
                return e
        return None

    def create_event(self, title, start, end):
        ev = {"id": "new-id", "title": title, "start": start, "end": end}
        self.events.append(ev)
        return ev

    def update_event(self, event_id, **fields):
        return {"id": event_id, **fields}

    def delete_event(self, event_id):
        self.events = [e for e in self.events if e["id"] != event_id]


def _cal_router(tmp_path, cal):
    return IntentRouter(None, None, calendar=cal, action_log=ActionLog(tmp_path / "a.db"))


def test_calendar_create_then_undo_is_confirm_delete(tmp_path):
    cal = FakeCal()
    r = _cal_router(tmp_path, cal)
    r.execute({"type": "create_event", "title": "Дантист",
               "start": dt(2026, 6, 28, 10).isoformat(), "end": dt(2026, 6, 28, 11).isoformat()})

    res = r.resolve({"intent": "undo_last"})
    assert res.kind == "confirm"  # календарь — только через Да/Нет
    assert res.action["type"] == "delete_event"


def test_calendar_delete_then_undo_is_confirm_recreate(tmp_path):
    cal = FakeCal([{"id": "e1", "title": "Дантист",
                    "start": dt(2026, 6, 28, 10), "end": dt(2026, 6, 28, 11)}])
    r = _cal_router(tmp_path, cal)
    r.execute({"type": "delete_event", "event_id": "e1", "title": "Дантист"})
    assert cal.get_event("e1") is None

    res = r.resolve({"intent": "undo_last"})
    assert res.kind == "confirm"
    assert res.action["type"] == "create_event"
    assert res.action["title"] == "Дантист"

    r.execute(res.action)
    assert any(e["title"] == "Дантист" for e in cal.events)  # восстановлена
