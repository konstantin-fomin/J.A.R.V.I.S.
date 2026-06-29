"""Тесты повторяющихся задач (§18.2).

RecurringTaskStore тестируется как самостоятельное SQLite-хранилище (в стиле
tasks.py/bills.py), генерация инстансов — поверх реального TaskStore на временной
базе, интеграция интентов — через настоящий IntentRouter. Сеть/LLM не дёргаем.
"""
from datetime import date, timedelta

import pytest

from bills import BillStore
from intents import RISK_LEVELS, IntentRouter
from logger import ActionLog
from recurring import RecurringTaskStore
from tasks import TaskStore


# --- RecurringTaskStore как хранилище ---------------------------------------

@pytest.fixture
def rec(tmp_path):
    return RecurringTaskStore(tmp_path / "rec.db")


def test_create_template_roundtrip(rec):
    t = rec.create_template("зарядка", "daily", time="08:00", project="здоровье")
    assert t["title"] == "зарядка"
    assert t["recurrence_type"] == "daily"
    assert t["time"] == "08:00"
    assert t["project"] == "здоровье"
    assert t["active"] == 1
    assert rec.get_template(t["id"]) == t


def test_list_templates_active_only(rec):
    a = rec.create_template("A", "daily")
    b = rec.create_template("B", "daily")
    rec.update_template(b["id"], active=False)
    active = rec.list_templates(active_only=True)
    assert [t["id"] for t in active] == [a["id"]]
    assert len(rec.list_templates()) == 2


def test_delete_template(rec):
    t = rec.create_template("A", "daily")
    assert rec.delete_template(t["id"]) is True
    assert rec.get_template(t["id"]) is None


# --- генерация инстансов: ensure_day по типу --------------------------------

@pytest.fixture
def tasks(tmp_path):
    return TaskStore(tmp_path / "t.db")


def test_daily_generates_every_day_and_is_idempotent(rec, tasks):
    rec.create_template("пить воду", "daily", time="10:00", project="здоровье")
    day = date(2026, 6, 29)

    created = rec.ensure_day(day, tasks)
    assert created == 1
    again = rec.ensure_day(day, tasks)
    assert again == 0  # повторный запуск в тот же день — без дублей

    items = tasks.list(due_date=day.isoformat())
    assert len(items) == 1
    inst = items[0]
    assert inst["title"] == "пить воду"
    assert inst["due_time"] == "10:00"
    assert inst["project"] == "здоровье"
    assert inst["source"] == "recurring"
    assert inst["recurring_template_id"] is not None


def test_weekly_generates_only_on_matching_weekday(rec, tasks):
    # 2026-06-29 — понедельник (weekday()==0)
    monday = date(2026, 6, 29)
    assert monday.weekday() == 0
    rec.create_template("отчёт", "weekly", day_of_week=0)

    assert rec.ensure_day(monday, tasks) == 1            # понедельник — стреляет
    assert rec.ensure_day(monday + timedelta(days=1), tasks) == 0  # вторник — нет


def test_monthly_generates_on_matching_day(rec, tasks):
    rec.create_template("платёж по абонементу", "monthly", day_of_month=15)
    assert rec.ensure_day(date(2026, 6, 15), tasks) == 1
    assert rec.ensure_day(date(2026, 6, 14), tasks) == 0


def test_monthly_clamps_to_last_day_of_short_month(rec, tasks):
    # day_of_month=31, февраль (28 дней в 2026) → стреляет 28-го, не 27-го
    rec.create_template("месячное", "monthly", day_of_month=31)
    assert rec.ensure_day(date(2026, 2, 27), tasks) == 0
    assert rec.ensure_day(date(2026, 2, 28), tasks) == 1


def test_inactive_template_does_not_generate(rec, tasks):
    t = rec.create_template("выкл", "daily")
    rec.update_template(t["id"], active=False)
    assert rec.ensure_day(date(2026, 6, 29), tasks) == 0


# --- очистка: только старые выполненные recurring-инстансы -------------------

def test_purge_removes_only_old_done_recurring(tasks):
    today = date(2026, 6, 29)
    cutoff = (today - timedelta(days=30)).isoformat()

    old_done_rec = tasks.create(title="старая recurring", due_date="2026-05-01",
                                source="recurring", recurring_template_id=1)
    tasks.update(old_done_rec["id"], status="done")
    recent_done_rec = tasks.create(title="свежая recurring", due_date=today.isoformat(),
                                   source="recurring", recurring_template_id=1)
    tasks.update(recent_done_rec["id"], status="done")
    tasks.create(title="невыполненная recurring", due_date="2026-05-01",
                 source="recurring", recurring_template_id=1)
    old_done_normal = tasks.create(title="старая обычная", due_date="2026-05-01")
    tasks.update(old_done_normal["id"], status="done")

    removed = tasks.purge_recurring_done(cutoff)
    assert removed == 1

    remaining = {t["title"] for t in tasks.list()}
    assert "старая recurring" not in remaining       # удалена
    assert "свежая recurring" in remaining           # не старая
    assert "невыполненная recurring" in remaining    # не done
    assert "старая обычная" in remaining             # не recurring — не трогаем


# --- интенты через IntentRouter ---------------------------------------------

@pytest.fixture
def router(tmp_path):
    tasks = TaskStore(tmp_path / "t.db")
    bills = BillStore(tmp_path / "b.db")
    recurring = RecurringTaskStore(tmp_path / "rec.db")
    alog = ActionLog(tmp_path / "a.db")
    r = IntentRouter(tasks, bills, calendar=None, action_log=alog, recurring=recurring)
    return r, tasks, recurring


def test_recurring_intents_risk_levels():
    assert RISK_LEVELS["create_recurring_task"] == "medium"
    assert RISK_LEVELS["query_recurring_tasks"] == "safe"
    assert RISK_LEVELS["delete_recurring_template"] == "dangerous"


def test_create_recurring_task_executes_and_persists(router):
    r, _, recurring = router
    res = r.resolve({"intent": "create_recurring_task", "confidence": "high",
                     "title": "зарядка", "recurrence_type": "daily", "time": "08:00"})
    assert res.kind == "execute"
    r.execute(res.action)
    templates = recurring.list_templates()
    assert len(templates) == 1
    assert templates[0]["title"] == "зарядка"
    assert templates[0]["recurrence_type"] == "daily"


def test_create_recurring_weekly_with_day_of_week(router):
    r, _, recurring = router
    res = r.resolve({"intent": "create_recurring_task", "confidence": "high",
                     "title": "созвон", "recurrence_type": "weekly", "day_of_week": 2})
    r.execute(res.action)
    assert recurring.list_templates()[0]["day_of_week"] == 2


def test_query_recurring_tasks_is_safe(router):
    r, _, recurring = router
    recurring.create_template("зарядка", "daily")
    res = r.resolve({"intent": "query_recurring_tasks", "confidence": "low"})
    assert res.kind == "execute"  # safe → сразу даже при low
    out = r.execute(res.action)
    assert "зарядка" in out


def test_delete_recurring_template_confirms_then_deletes(router):
    r, _, recurring = router
    recurring.create_template("зарядка", "daily")
    res = r.resolve({"intent": "delete_recurring_template", "confidence": "high",
                     "title_hint": "зарядка"})
    assert res.kind == "confirm"  # dangerous → всегда Да/Нет
    r.execute(res.action)
    assert recurring.list_templates() == []


def test_delete_recurring_template_not_found_is_message(router):
    r, _, _ = router
    res = r.resolve({"intent": "delete_recurring_template", "confidence": "high",
                     "title_hint": "несуществующее"})
    assert res.kind == "message"
