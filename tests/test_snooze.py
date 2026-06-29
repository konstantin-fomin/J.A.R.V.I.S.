"""Тесты snooze/defer (§18.1): нормализация относительного offset в конкретные
due_date/due_time и интеграция через IntentRouter (правка последней задачи тем же
edit_last-путём, отменяемо). Сеть/LLM не дёргаем; now инъектируем в нормализатор.
"""
from datetime import date, datetime, timedelta

import pytest

from bills import BillStore
from contacts import ContactStore
from intents import RISK_LEVELS, IntentRouter, normalize_snooze_offset
from logger import ActionLog
from tasks import TaskStore


# --- нормализация offset → due_date/due_time --------------------------------

NOW = datetime(2026, 6, 29, 14, 0)  # понедельник, день


def test_named_evening_is_today_19():
    assert normalize_snooze_offset("evening", NOW) == {"due_date": "2026-06-29", "due_time": "19:00"}


def test_named_morning_is_tomorrow_09():
    assert normalize_snooze_offset("morning", NOW) == {"due_date": "2026-06-30", "due_time": "09:00"}


def test_named_tomorrow_shifts_date_only():
    # «перенеси на завтра» / «не сегодня» — дату на завтра, время не трогаем
    assert normalize_snooze_offset("tomorrow", NOW) == {"due_date": "2026-06-30"}


def test_named_next_week_is_plus_7_days_date_only():
    assert normalize_snooze_offset("next_week", NOW) == {"due_date": "2026-07-06"}


def test_duration_hours_sets_date_and_time():
    assert normalize_snooze_offset("2h", NOW) == {"due_date": "2026-06-29", "due_time": "16:00"}


def test_duration_minutes_sets_date_and_time():
    assert normalize_snooze_offset("30m", NOW) == {"due_date": "2026-06-29", "due_time": "14:30"}


def test_duration_hours_can_roll_over_midnight():
    late = datetime(2026, 6, 29, 23, 30)
    assert normalize_snooze_offset("2h", late) == {"due_date": "2026-06-30", "due_time": "01:30"}


def test_duration_days_shifts_date_only():
    assert normalize_snooze_offset("3d", NOW) == {"due_date": "2026-07-02"}


def test_plus_prefix_accepted():
    assert normalize_snooze_offset("+2h", NOW) == {"due_date": "2026-06-29", "due_time": "16:00"}


def test_unknown_offset_returns_none():
    assert normalize_snooze_offset("когда-нибудь", NOW) is None
    assert normalize_snooze_offset("", NOW) is None


# --- интеграция через IntentRouter ------------------------------------------

@pytest.fixture
def router(tmp_path):
    tasks = TaskStore(tmp_path / "t.db")
    bills = BillStore(tmp_path / "b.db")
    contacts = ContactStore(tmp_path / "c.db")
    alog = ActionLog(tmp_path / "a.db")
    r = IntentRouter(tasks, bills, calendar=None, action_log=alog, contacts=contacts)
    return r, tasks, contacts


def test_snooze_risk_level_is_medium():
    assert RISK_LEVELS["snooze"] == "medium"


def test_snooze_evening_sets_today_19_on_last_task(router):
    r, tasks, _ = router
    r.execute({"type": "create_task", "params": {"title": "позвонить врачу"}})
    last_id = int(r.log.latest_active()["entity_id"])

    res = r.resolve({"intent": "snooze", "confidence": "high", "offset": "evening"})
    assert res.kind == "execute"  # medium + high → сразу
    r.execute(res.action)

    t = tasks.get(last_id)
    assert t["due_date"] == date.today().isoformat()
    assert t["due_time"] == "19:00"


def test_snooze_tomorrow_preserves_existing_time(router):
    r, tasks, _ = router
    r.execute({"type": "create_task",
               "params": {"title": "встреча", "due_date": date.today().isoformat(),
                          "due_time": "10:00"}})
    last_id = int(r.log.latest_active()["entity_id"])

    r.execute(r.resolve({"intent": "snooze", "confidence": "high", "offset": "tomorrow"}).action)

    t = tasks.get(last_id)
    assert t["due_date"] == (date.today() + timedelta(days=1)).isoformat()
    assert t["due_time"] == "10:00"  # время сохранили


def test_snooze_low_confidence_confirms(router):
    r, tasks, _ = router
    r.execute({"type": "create_task", "params": {"title": "задача"}})
    res = r.resolve({"intent": "snooze", "confidence": "low", "offset": "tomorrow"})
    assert res.kind == "confirm"  # medium + low → переспросить


def test_snooze_unknown_offset_is_message(router):
    r, tasks, _ = router
    r.execute({"type": "create_task", "params": {"title": "задача"}})
    res = r.resolve({"intent": "snooze", "confidence": "high", "offset": "ягодки"})
    assert res.kind == "message"


def test_snooze_on_non_task_is_message(router):
    r, _, contacts = router
    r.execute({"type": "create_contact", "params": {"name": "Маша"}})
    res = r.resolve({"intent": "snooze", "confidence": "high", "offset": "tomorrow"})
    assert res.kind == "message"  # контакт нельзя отложить


def test_snooze_with_empty_log_is_message(router):
    r, *_ = router
    res = r.resolve({"intent": "snooze", "confidence": "high", "offset": "tomorrow"})
    assert res.kind == "message"


def test_snooze_is_undoable(router):
    r, tasks, _ = router
    r.execute({"type": "create_task",
               "params": {"title": "задача", "due_date": "2026-06-29", "due_time": "10:00"}})
    last_id = int(r.log.latest_active()["entity_id"])

    r.execute(r.resolve({"intent": "snooze", "confidence": "high", "offset": "tomorrow"}).action)
    assert tasks.get(last_id)["due_date"] != "2026-06-29"

    r.execute(r.resolve({"intent": "undo_last"}).action)
    restored = tasks.get(last_id)
    assert restored["due_date"] == "2026-06-29"
    assert restored["due_time"] == "10:00"
