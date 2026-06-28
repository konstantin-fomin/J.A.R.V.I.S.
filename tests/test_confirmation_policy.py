"""Тесты §17: декларативная таблица рисков подтверждений и правка последнего
действия (edit_last).

Подтверждения проверяем как регрессию (поведение не должно измениться после
рефактора под RISK_LEVELS), а edit_last — через реальный IntentRouter с
TaskStore/ContactStore/ActionLog на временных базах. Сеть/LLM не дёргаем.
"""
import pytest

from bills import BillStore
from contacts import ContactStore
from intents import INTENTS, RISK_LEVELS, IntentRouter
from logger import ActionLog
from tasks import TaskStore


@pytest.fixture
def router(tmp_path):
    tasks = TaskStore(tmp_path / "t.db")
    bills = BillStore(tmp_path / "b.db")
    contacts = ContactStore(tmp_path / "c.db")
    alog = ActionLog(tmp_path / "a.db")
    r = IntentRouter(tasks, bills, calendar=None, action_log=alog, contacts=contacts)
    return r, tasks, contacts, alog


# --- Таблица рисков ---------------------------------------------------------

def test_risk_levels_cover_all_mutating_and_query_intents():
    """Каждый интент (кроме служебных none/undo_last/edit_last) имеет уровень риска."""
    special = {"none", "undo_last", "edit_last"}
    for intent in INTENTS - special:
        assert intent in RISK_LEVELS, f"нет уровня риска для {intent}"
        assert RISK_LEVELS[intent] in {"safe", "medium", "dangerous"}


def test_delete_task_always_confirms_even_high_confidence(router):
    r, tasks, _, _ = router
    tasks.create(title="старая задача")
    res = r.resolve({"intent": "delete_task", "confidence": "high", "title_hint": "старая"})
    assert res.kind == "confirm"  # dangerous → всегда Да/Нет


def test_create_task_executes_on_high_confirms_on_low(router):
    r, *_ = router
    high = r.resolve({"intent": "create_task", "confidence": "high", "title": "купить хлеб"})
    low = r.resolve({"intent": "create_task", "confidence": "low", "title": "купить хлеб"})
    assert high.kind == "execute"   # medium + high → сразу
    assert low.kind == "confirm"    # medium + low → переспросить


def test_query_by_project_is_safe_executes_immediately(router):
    r, *_ = router
    res = r.resolve({"intent": "query_by_project", "confidence": "low", "project": "ремонт"})
    assert res.kind == "execute"  # safe → всегда сразу, даже при low


def test_query_tasks_confirms_on_low_confidence(router):
    """Регрессия: query_tasks исторически идёт через auto-or-confirm (medium)."""
    r, *_ = router
    res = r.resolve({"intent": "query_tasks", "confidence": "low", "filter": "all"})
    assert res.kind == "confirm"


# --- edit_last --------------------------------------------------------------

def test_edit_last_changes_task_priority(router):
    r, tasks, _, _ = router
    r.execute({"type": "create_task", "params": {"title": "доделать отчёт"}})
    # последняя запись журнала — про только что созданную задачу
    last_id = int(r.log.latest_active()["entity_id"])

    res = r.resolve({"intent": "edit_last", "field": "priority", "value": "high"})
    assert res.kind == "execute"
    r.execute(res.action)

    assert tasks.get(last_id)["priority"] == "high"


def test_edit_last_renames_task(router):
    r, tasks, _, _ = router
    r.execute({"type": "create_task", "params": {"title": "старое имя"}})
    last_id = int(r.log.latest_active()["entity_id"])

    res = r.resolve({"intent": "edit_last", "field": "title", "value": "новое имя"})
    r.execute(res.action)

    assert tasks.get(last_id)["title"] == "новое имя"


def test_edit_last_sets_task_due_date(router):
    r, tasks, _, _ = router
    r.execute({"type": "create_task", "params": {"title": "позвонить"}})
    last_id = int(r.log.latest_active()["entity_id"])

    res = r.resolve({"intent": "edit_last", "field": "due_date", "value": "2026-07-03"})
    r.execute(res.action)

    assert tasks.get(last_id)["due_date"] == "2026-07-03"


def test_edit_last_appends_to_contact_notes(router):
    r, _, contacts, _ = router
    r.execute({"type": "create_contact", "params": {"name": "Маша", "notes": "коллега"}})
    cid = int(r.log.latest_active()["entity_id"])

    res = r.resolve({"intent": "edit_last", "field": "note", "value": "любит чай"})
    r.execute(res.action)

    notes = contacts.get(cid)["notes"]
    assert "коллега" in notes and "любит чай" in notes  # дописал, не затёр


def test_edit_last_inapplicable_field_is_honest_message_not_noop(router):
    """Приоритет к контакту не применим → честный ответ, ничего не меняем."""
    r, _, contacts, _ = router
    r.execute({"type": "create_contact", "params": {"name": "Петя", "notes": "сосед"}})
    cid = int(r.log.latest_active()["entity_id"])

    res = r.resolve({"intent": "edit_last", "field": "priority", "value": "high"})
    assert res.kind == "message"
    assert res.action is None
    assert contacts.get(cid)["notes"] == "сосед"  # no-op данных нет


def test_edit_last_with_nothing_to_edit_is_message(router):
    r, *_ = router
    res = r.resolve({"intent": "edit_last", "field": "title", "value": "x"})
    assert res.kind == "message"


def test_edit_last_is_undoable(router):
    r, tasks, _, _ = router
    r.execute({"type": "create_task", "params": {"title": "задача", "priority": "normal"}})
    last_id = int(r.log.latest_active()["entity_id"])

    r.execute(r.resolve({"intent": "edit_last", "field": "priority", "value": "high"}).action)
    assert tasks.get(last_id)["priority"] == "high"

    undo = r.resolve({"intent": "undo_last"})
    r.execute(undo.action)
    assert tasks.get(last_id)["priority"] == "normal"  # вернулось как было
