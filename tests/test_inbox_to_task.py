"""Тесты конвертации inbox-записи в задачу — два входа в одну общую операцию.

Общая функция convert_inbox_item_to_task: task с дословным текстом записи (без
LLM) + inbox status=processed. Два входа: REST-эндпоинт дашборда
(POST /api/inbox/{id}/to-task, напрямую через сторы, мимо ActionLog — §10) и
NL-intent inbox_to_task бота (через IntentRouter, логируется как task/create,
отменяемо undo_last). Сеть/LLM не дёргаем.
"""
import pytest
from fastapi.testclient import TestClient

from bills import BillStore
from inbox import InboxStore, convert_inbox_item_to_task
from intents import INTENTS, RISK_LEVELS, IntentRouter
from logger import ActionLog
from tasks import TaskStore
from web.server import create_app


# --- Общая функция конвертации ----------------------------------------------

def test_convert_creates_task_with_verbatim_text_and_marks_processed(tmp_path):
    tasks = TaskStore(tmp_path / "t.db")
    inbox = InboxStore(tmp_path / "i.db")
    item = inbox.create("позвонить врачу про анализы")

    task = convert_inbox_item_to_task(tasks, inbox, item)

    assert task["title"] == "позвонить врачу про анализы"  # дословно, без причёсывания
    assert task["source"] == "inbox"
    assert inbox.get(item["id"])["status"] == "processed"
    assert any(t["title"] == "позвонить врачу про анализы" for t in tasks.list())


# --- REST-эндпоинт дашборда -------------------------------------------------

def _client(tmp_path):
    tasks = TaskStore(tmp_path / "t.db")
    bills = BillStore(tmp_path / "b.db")
    inbox = InboxStore(tmp_path / "i.db")
    app = create_app(None, None, None, tasks, bills, None, inbox)  # type: ignore[arg-type]
    return TestClient(app), tasks, inbox


def test_api_inbox_to_task_creates_task_and_removes_from_pending(tmp_path):
    client, tasks, inbox = _client(tmp_path)
    item = inbox.create("купить подарок маме")

    r = client.post(f"/api/inbox/{item['id']}/to-task")
    assert r.status_code == 200
    task = r.json()["task"]
    assert task["title"] == "купить подарок маме"

    # задача появилась, запись ушла из pending (стала processed)
    assert any(t["title"] == "купить подарок маме" for t in tasks.list())
    assert inbox.list("pending") == []
    assert inbox.get(item["id"])["status"] == "processed"


def test_api_inbox_to_task_missing_item_is_404(tmp_path):
    client, _, _ = _client(tmp_path)
    assert client.post("/api/inbox/999/to-task").status_code == 404


def test_api_inbox_to_task_no_inbox_is_503(tmp_path):
    tasks = TaskStore(tmp_path / "t.db")
    bills = BillStore(tmp_path / "b.db")
    app = create_app(None, None, None, tasks, bills, None, None)  # type: ignore[arg-type]
    assert TestClient(app).post("/api/inbox/1/to-task").status_code == 503


# --- NL-intent inbox_to_task ------------------------------------------------

@pytest.fixture
def router(tmp_path):
    tasks = TaskStore(tmp_path / "t.db")
    bills = BillStore(tmp_path / "b.db")
    inbox = InboxStore(tmp_path / "i.db")
    alog = ActionLog(tmp_path / "a.db")
    r = IntentRouter(tasks, bills, calendar=None, action_log=alog, inbox=inbox)
    return r, tasks, inbox, alog


def test_inbox_to_task_is_known_safe_intent():
    assert "inbox_to_task" in INTENTS
    assert RISK_LEVELS["inbox_to_task"] == "safe"


def test_prompt_documents_inbox_to_task():
    from intents import PROMPT
    assert "inbox_to_task" in PROMPT


def test_resolve_inbox_to_task_finds_by_fuzzy_hint_and_executes(router):
    r, _, inbox, _ = router
    inbox.create("забронировать билеты в Питер")
    res = r.resolve({"intent": "inbox_to_task", "confidence": "high", "title_hint": "билеты"})
    assert res.kind == "execute"  # safe → сразу, без подтверждения
    assert res.action["type"] == "inbox_to_task"


def test_resolve_inbox_to_task_no_match_is_honest_message(router):
    r, _, inbox, _ = router
    inbox.create("совсем другое")
    res = r.resolve({"intent": "inbox_to_task", "confidence": "high", "title_hint": "пицца"})
    assert res.kind == "message"
    assert "инбоксе" in res.text.lower()


def test_resolve_inbox_to_task_already_processed_not_matched(router):
    """Уже разобранные записи не предлагаются к конвертации повторно."""
    r, _, inbox, _ = router
    it = inbox.create("старое дело")
    inbox.set_status(it["id"], "processed")
    res = r.resolve({"intent": "inbox_to_task", "confidence": "high", "title_hint": "старое"})
    assert res.kind == "message"


def test_execute_inbox_to_task_creates_task_marks_processed_and_logs(router):
    r, tasks, inbox, alog = router
    it = inbox.create("записаться к стоматологу")
    res = r.resolve({"intent": "inbox_to_task", "confidence": "high", "title_hint": "стоматолог"})
    r.execute(res.action)

    assert any(t["title"] == "записаться к стоматологу" for t in tasks.list())
    assert inbox.get(it["id"])["status"] == "processed"
    # залогировано как обычное создание задачи (entity_type=task, action=create)
    rec = alog.latest_active()
    assert rec["entity_type"] == "task" and rec["action"] == "create"


def test_inbox_to_task_is_undoable(router):
    r, tasks, inbox, _ = router
    it = inbox.create("полить цветы")
    r.execute(r.resolve({"intent": "inbox_to_task", "confidence": "high",
                         "title_hint": "цветы"}).action)
    assert any(t["title"] == "полить цветы" for t in tasks.list())

    undo = r.resolve({"intent": "undo_last"})
    r.execute(undo.action)
    assert not any(t["title"] == "полить цветы" for t in tasks.list())  # задача снята
