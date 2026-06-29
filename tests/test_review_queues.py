"""Тесты очередей разбора инбокса (§19.2): статусы someday/needs_decision/
maybe_later, журналируемый capture и intent inbox_reclassify (переклассификация
последней записи через тот же путь, что edit_last/snooze) с откатом.

Сеть/Telegram/LLM не дёргаем — работаем напрямую через InboxStore/IntentRouter.
"""
import pytest

from bills import BillStore
from inbox import InboxStore
from intents import RISK_LEVELS, IntentRouter
from logger import ActionLog
from tasks import TaskStore


# --- InboxStore: статусы разбора --------------------------------------------

def test_delete_removes_item(tmp_path):
    inbox = InboxStore(tmp_path / "i.db")
    item = inbox.create("мысль")
    assert inbox.delete(item["id"]) is True
    assert inbox.get(item["id"]) is None


def test_review_status_is_stored_and_listable(tmp_path):
    inbox = InboxStore(tmp_path / "i.db")
    item = inbox.create("обдумать переезд")
    inbox.set_status(item["id"], "someday")
    assert inbox.get(item["id"])["status"] == "someday"
    assert [i["id"] for i in inbox.list(status="someday")] == [item["id"]]
    assert inbox.list(status="pending") == []


# --- Интеграция через IntentRouter ------------------------------------------

@pytest.fixture
def router(tmp_path):
    tasks = TaskStore(tmp_path / "t.db")
    bills = BillStore(tmp_path / "b.db")
    inbox = InboxStore(tmp_path / "i.db")
    alog = ActionLog(tmp_path / "a.db")
    r = IntentRouter(tasks, bills, calendar=None, action_log=alog, inbox=inbox)
    return r, inbox


def test_risk_level_registered():
    assert RISK_LEVELS["inbox_reclassify"] == "medium"


def test_capture_is_logged_and_undoable(router):
    r, inbox = router
    r.execute(r.resolve({"intent": "capture", "confidence": "high", "note": "идея"}).action)
    assert len(inbox.list()) == 1
    r.execute(r.resolve({"intent": "undo_last"}).action)
    assert inbox.list() == []  # захват откатился — записи нет


def test_reclassify_changes_last_inbox_status(router):
    r, inbox = router
    r.execute(r.resolve({"intent": "capture", "confidence": "high", "note": "обдумать"}).action)
    item_id = inbox.list()[0]["id"]
    res = r.resolve({"intent": "inbox_reclassify", "confidence": "high", "status": "someday"})
    assert res.kind == "execute"
    r.execute(res.action)
    assert inbox.get(item_id)["status"] == "someday"


def test_reclassify_is_undoable(router):
    r, inbox = router
    r.execute(r.resolve({"intent": "capture", "confidence": "high", "note": "обдумать"}).action)
    item_id = inbox.list()[0]["id"]
    r.execute(r.resolve({"intent": "inbox_reclassify", "confidence": "high",
                         "status": "needs_decision"}).action)
    assert inbox.get(item_id)["status"] == "needs_decision"
    r.execute(r.resolve({"intent": "undo_last"}).action)
    assert inbox.get(item_id)["status"] == "pending"  # вернулся прежний статус


def test_reclassify_unknown_status_is_message(router):
    r, _ = router
    r.execute(r.resolve({"intent": "capture", "confidence": "high", "note": "x"}).action)
    res = r.resolve({"intent": "inbox_reclassify", "confidence": "high", "status": "колбаса"})
    assert res.kind == "message"


def test_reclassify_when_last_action_not_inbox_is_message(router):
    r, _ = router
    # последнее действие — создание задачи, не инбокс
    r.execute(r.resolve({"intent": "create_task", "confidence": "high", "title": "дело"}).action)
    res = r.resolve({"intent": "inbox_reclassify", "confidence": "high", "status": "someday"})
    assert res.kind == "message"


def test_reclassify_low_confidence_confirms(router):
    r, inbox = router
    r.execute(r.resolve({"intent": "capture", "confidence": "high", "note": "обдумать"}).action)
    res = r.resolve({"intent": "inbox_reclassify", "confidence": "low", "status": "someday"})
    assert res.kind == "confirm"  # medium + low → подтверждение
