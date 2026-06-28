"""Тесты Projects (поле project у задач + intent query_by_project) и Inbox
(InboxStore + intent capture + конвертация в задачу).

Хранилища тестируются на временных SQLite-базах, роутер — напрямую через
resolve()/execute() без сети и Telegram. Корректность «склонений» — это качество
промпта Gemini (нормализация в именительный падеж до подстрочного поиска),
поэтому здесь проверяется сам контракт: регистронезависимый подстрочный матч.
"""
import sqlite3

import pytest

from bills import BillStore
from inbox import InboxStore
from intents import IntentRouter
from logger import ActionLog
from tasks import TaskStore


# --- Projects: поле project у задач + миграция ------------------------------

def test_task_create_with_project(tmp_path):
    tasks = TaskStore(tmp_path / "t.db")
    t = tasks.create(title="смета", project="ремонт")
    assert t["project"] == "ремонт"


def test_task_project_defaults_to_none(tmp_path):
    tasks = TaskStore(tmp_path / "t.db")
    assert tasks.create(title="купить молоко")["project"] is None


def test_tasks_migration_adds_project_to_old_db(tmp_path):
    db = tmp_path / "t.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE tasks (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, "
        "description TEXT, status TEXT NOT NULL DEFAULT 'todo', priority TEXT NOT NULL "
        "DEFAULT 'normal', due_date TEXT, due_time TEXT, source TEXT NOT NULL DEFAULT "
        "'telegram', created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
    )
    conn.execute("INSERT INTO tasks (title, created_at, updated_at) VALUES ('старая','x','x')")
    conn.commit()
    conn.close()

    tasks = TaskStore(db)  # должен добавить колонку project через ALTER
    old = tasks.list()[0]
    assert "project" in old and old["project"] is None


def test_bill_template_migration_adds_project_to_old_db(tmp_path):
    db = tmp_path / "b.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE bill_templates (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT "
        "NOT NULL, amount REAL, day_of_month INTEGER NOT NULL, category TEXT, active "
        "INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO bill_templates (name, day_of_month, created_at, updated_at) "
        "VALUES ('аренда', 5, 'x', 'x')"
    )
    conn.commit()
    conn.close()

    bills = BillStore(db)
    tpl = bills.list_templates()[0]
    assert "project" in tpl and tpl["project"] is None


# --- Inbox: InboxStore -------------------------------------------------------

def test_inbox_create_is_pending(tmp_path):
    inbox = InboxStore(tmp_path / "i.db")
    it = inbox.create("идея про лендинг", source="telegram")
    assert it["status"] == "pending"
    assert it["text"] == "идея про лендинг"
    assert it["source"] == "telegram"


def test_inbox_list_filters_by_status(tmp_path):
    inbox = InboxStore(tmp_path / "i.db")
    a = inbox.create("разобрать почту")
    inbox.create("позвонить врачу")
    inbox.set_status(a["id"], "processed")
    assert len(inbox.list(status="pending")) == 1
    assert len(inbox.list(status="processed")) == 1
    assert len(inbox.list()) == 2


def test_inbox_set_status_processed(tmp_path):
    inbox = InboxStore(tmp_path / "i.db")
    it = inbox.create("разобрать почту")
    inbox.set_status(it["id"], "processed")
    assert inbox.get(it["id"])["status"] == "processed"


# --- Роутер: create_task с project / query_by_project / capture -------------

@pytest.fixture
def router(tmp_path):
    tasks = TaskStore(tmp_path / "t.db")
    bills = BillStore(tmp_path / "b.db")
    inbox = InboxStore(tmp_path / "i.db")
    r = IntentRouter(tasks, bills, calendar=None,
                     action_log=ActionLog(tmp_path / "a.db"), inbox=inbox)
    return r, tasks, bills, inbox


def test_create_task_carries_project(router):
    r, *_ = router
    res = r.resolve({"intent": "create_task", "confidence": "high",
                     "title": "смета", "project": "ремонт"})
    assert res.kind == "execute"
    assert res.action["params"]["project"] == "ремонт"


def test_create_task_without_project_omits_field(router):
    r, *_ = router
    res = r.resolve({"intent": "create_task", "confidence": "high",
                     "title": "купить молоко", "project": None})
    assert "project" not in res.action["params"]


def test_query_by_project_filters_substring(router):
    r, tasks, _, _ = router
    tasks.create(title="смета", project="ремонт квартиры")
    tasks.create(title="дизайн", project="ремонт квартиры")
    tasks.create(title="купить молоко", project=None)

    res = r.resolve({"intent": "query_by_project", "confidence": "high", "project": "ремонт"})
    assert res.kind == "execute"
    reply = r.execute(res.action)
    assert "смета" in reply and "дизайн" in reply
    assert "купить молоко" not in reply


def test_query_by_project_is_case_insensitive(router):
    r, tasks, _, _ = router
    tasks.create(title="смета", project="Ремонт")
    reply = r.execute(
        r.resolve({"intent": "query_by_project", "confidence": "high", "project": "ремонт"}).action
    )
    assert "смета" in reply


def test_query_by_project_empty_when_no_match(router):
    r, tasks, _, _ = router
    tasks.create(title="смета", project="ремонт")
    reply = r.execute(
        r.resolve({"intent": "query_by_project", "confidence": "high", "project": "дача"}).action
    )
    assert "смета" not in reply


def test_capture_stores_to_inbox(router):
    r, _, _, inbox = router
    res = r.resolve({"intent": "capture", "confidence": "high", "note": "идея про лендинг"})
    assert res.kind == "execute"
    reply = r.execute(res.action)
    items = inbox.list(status="pending")
    assert len(items) == 1
    assert items[0]["text"] == "идея про лендинг"
    assert "лендинг" in reply


def test_capture_empty_note_falls_back_to_chat(router):
    r, *_ = router
    res = r.resolve({"intent": "capture", "confidence": "high", "note": ""})
    assert res.kind == "chat"


def test_capture_low_confidence_asks_confirmation(router):
    r, *_ = router
    res = r.resolve({"intent": "capture", "confidence": "low", "note": "идея"})
    assert res.kind == "confirm"


def test_inbox_item_converts_to_task_and_marks_processed(router):
    # Это путь кнопки «→ в задачу»: создаём задачу через существующий create_task
    # path и помечаем запись инбокса processed.
    r, tasks, _, inbox = router
    it = inbox.create("позвонить врачу")
    r.execute({"type": "create_task", "params": {"title": it["text"], "source": "inbox"}})
    inbox.set_status(it["id"], "processed")
    assert any(t["title"] == "позвонить врачу" for t in tasks.list())
    assert inbox.get(it["id"])["status"] == "processed"
