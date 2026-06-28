"""Тесты weekly review (§16): чистый расчёт цифр + LLM-обёртка.

compute_week_stats — чистая функция над реальными сторами (SQLite на tmp_path),
без сети/LLM. compose_summary — с фейковым LLM: проверяем, что в промпт уходят
только уже посчитанные числа, и что вызов ровно один. updated_at/timestamp у
части записей выставляем напрямую в БД — чтобы детерминированно проверить окно.
"""
import sqlite3
from datetime import date

import pytest

from bills import BillStore
from contacts import ContactStore
from logger import ActionLog
from reads import ReadStore
from tasks import TaskStore
from weekly_review import compose_summary, compute_week_stats

START = date(2026, 6, 22)
END = date(2026, 6, 28)


def _set(db_path, table, column, value, row_id):
    con = sqlite3.connect(db_path)
    con.execute(f"UPDATE {table} SET {column} = ? WHERE id = ?", (value, row_id))
    con.commit()
    con.close()


# --- ActionLog.actions_between ----------------------------------------------

def test_actions_between_filters_by_window(tmp_path):
    log = ActionLog(tmp_path / "a.db")
    a = log.log_action(source="telegram", entity_type="task", entity_id="1",
                       action="create", after_state={"title": "x"})
    b = log.log_action(source="telegram", entity_type="task", entity_id="2",
                       action="create", after_state={"title": "y"})
    _set(tmp_path / "a.db", "action_log", "timestamp", "2026-06-10T10:00:00+00:00", a["id"])
    _set(tmp_path / "a.db", "action_log", "timestamp", "2026-06-25T10:00:00+00:00", b["id"])
    rows = log.actions_between("2026-06-22", "2026-06-29")
    assert [r["id"] for r in rows] == [b["id"]]


# --- compute_week_stats ------------------------------------------------------

def _stores(tmp_path):
    return (TaskStore(tmp_path / "t.db"), BillStore(tmp_path / "b.db"),
            ContactStore(tmp_path / "c.db"), ReadStore(tmp_path / "r.db"),
            ActionLog(tmp_path / "a.db"))


def test_compute_full_scenario(tmp_path):
    tasks, bills, contacts, reads, log = _stores(tmp_path)

    # задачи: одна выполнена в окне, одна выполнена ДО окна, одна просрочена, одна нет
    t_in = tasks.create(title="сделано в окне"); tasks.update(t_in["id"], status="done")
    _set(tmp_path / "t.db", "tasks", "updated_at", "2026-06-25T09:00:00+00:00", t_in["id"])
    t_out = tasks.create(title="сделано давно"); tasks.update(t_out["id"], status="done")
    _set(tmp_path / "t.db", "tasks", "updated_at", "2026-06-10T09:00:00+00:00", t_out["id"])
    tasks.create(title="просрочено", due_date="2026-06-20")           # overdue
    tasks.create(title="ещё не скоро", due_date="2026-07-10")          # не overdue

    # платежи: 2 инстанса, один оплачен
    bills.create_template("аренда", day_of_month=10, amount=100)
    bills.create_template("свет", day_of_month=15, amount=50)
    bills.ensure_month("2026-06")
    inst = bills.list_instances("2026-06")
    bills.set_status(inst[0]["id"], "paid")

    # контакты: один с ДР в ближайшие дни, один — нет
    contacts.create(name="Мама", birthday="1965-07-01")               # +3 дня от END
    contacts.create(name="Дед", birthday="1940-12-01")                # далеко

    # очередь «почитать»: 2 непрочитанных, 1 прочитанная
    reads.create(url="u1", title="A"); reads.create(url="u2", title="B")
    reads.create(url="u3", title="C", status="read")

    # действия за неделю (timestamp = сейчас → в окне END)
    log.log_action(source="telegram", entity_type="task", entity_id="1", action="create")
    log.log_action(source="telegram", entity_type="task", entity_id="1", action="update")
    log.log_action(source="telegram", entity_type="bill", entity_id="1", action="mark_paid")

    stats = compute_week_stats(START, END, tasks=tasks, bills=bills,
                               contacts=contacts, reads=reads, log=log)

    assert stats["start"] == "2026-06-22" and stats["end"] == "2026-06-28"
    assert stats["tasks"]["completed"] == 1
    assert "сделано в окне" in stats["tasks"]["completed_titles"]
    assert stats["tasks"]["overdue"] == 1
    assert "просрочено" in stats["tasks"]["overdue_titles"]
    assert stats["bills"]["month"] == "2026-06"
    assert stats["bills"]["paid"] == 1 and stats["bills"]["pending"] == 1
    assert [b["name"] for b in stats["birthdays"]] == ["Мама"]
    assert stats["birthdays"][0]["in_days"] == 3
    assert stats["reads_unread"] == 2
    assert stats["actions"] == {"task.create": 1, "task.update": 1, "bill.mark_paid": 1}
    assert stats["actions_total"] == 3


def test_compute_no_overdue_no_birthdays(tmp_path):
    tasks, bills, contacts, reads, log = _stores(tmp_path)
    tasks.create(title="на будущее", due_date="2026-07-20")  # не просрочено
    contacts.create(name="Без ДР")                           # birthday None
    contacts.create(name="Зимний", birthday="1980-12-31")    # далеко

    stats = compute_week_stats(START, END, tasks=tasks, bills=bills,
                               contacts=contacts, reads=reads, log=log)
    assert stats["tasks"]["overdue"] == 0
    assert stats["tasks"]["completed"] == 0
    assert stats["birthdays"] == []
    assert stats["reads_unread"] == 0
    assert stats["actions_total"] == 0


# --- compose_summary ---------------------------------------------------------

class FakeLLM:
    def __init__(self):
        self.calls = []

    def chat(self, messages):
        self.calls.append(messages)
        return "Хорошая выдалась неделя!"


def test_compose_summary_passes_only_computed_numbers():
    stats = {
        "start": "2026-06-22", "end": "2026-06-28",
        "tasks": {"completed": 3, "completed_titles": ["a"], "overdue": 1, "overdue_titles": ["b"]},
        "bills": {"month": "2026-06", "paid": 2, "pending": 1, "pending_names": ["свет"]},
        "birthdays": [{"name": "Мама", "birthday": "1965-07-01", "in_days": 3}],
        "reads_unread": 5, "actions": {"task.create": 3}, "actions_total": 3,
    }
    llm = FakeLLM()
    out = compose_summary(llm, stats)

    assert out == "Хорошая выдалась неделя!"
    assert len(llm.calls) == 1                      # ровно один вызов Gemini
    prompt = llm.calls[0][0]["content"]
    # посчитанные числа уходят в промпт (через JSON)
    assert '"completed": 3' in prompt
    assert '"overdue": 1' in prompt
    assert '"reads_unread": 5' in prompt
    assert "Мама" in prompt
    # явное ограничение «только эти числа, ничего не выдумывать»
    assert "только" in prompt.lower()
