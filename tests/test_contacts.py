"""Тесты CRM-контактов (§14): хранилище, intents, undo и логика ДР.

ContactStore — самостоятельное SQLite-хранилище (как tasks.py), интеграция
intent→действие и отмена — через реальный IntentRouter с ActionLog на временных
базах. Сеть/Telegram/LLM не дёргаем: intent-объекты задаём руками.
"""
from datetime import date, timedelta

import pytest

from contacts import ContactStore, days_until_birthday
from intents import IntentRouter
from logger import ActionLog


# --- ContactStore как хранилище ---------------------------------------------

def test_create_and_find_by_substring(tmp_path):
    store = ContactStore(tmp_path / "c.db")
    store.create(name="Мама", birthday="1965-04-12")
    found = store.find("мам")  # подстрока, регистр не важен
    assert len(found) == 1
    assert found[0]["name"] == "Мама"
    assert found[0]["birthday"] == "1965-04-12"


def test_update_changes_fields(tmp_path):
    store = ContactStore(tmp_path / "c.db")
    c = store.create(name="Аня", notes="старое")
    store.update(c["id"], last_contact_date="2026-06-28", notes="старое\nновое")
    got = store.get(c["id"])
    assert got["last_contact_date"] == "2026-06-28"
    assert got["notes"] == "старое\nновое"


def test_delete_removes(tmp_path):
    store = ContactStore(tmp_path / "c.db")
    c = store.create(name="Вася")
    assert store.delete(c["id"]) is True
    assert store.list() == []


# --- логика дней рождения ----------------------------------------------------

def test_days_until_birthday_today_is_zero():
    assert days_until_birthday(date(1990, 6, 28), date(2026, 6, 28)) == 0


def test_days_until_birthday_future_within_year():
    assert days_until_birthday(date(1990, 7, 1), date(2026, 6, 28)) == 3


def test_days_until_birthday_wraps_to_next_year():
    # ДР 1 января, сегодня 30 декабря → 2 дня (через год)
    assert days_until_birthday(date(1990, 1, 1), date(2026, 12, 30)) == 2


def test_days_until_birthday_feb29_in_non_leap_year():
    # 2026 невисокосный → 29 февраля отмечаем 1 марта
    assert days_until_birthday(date(2000, 2, 29), date(2026, 2, 27)) == 2


def test_upcoming_birthdays_includes_n_excludes_n_plus_1(tmp_path):
    store = ContactStore(tmp_path / "c.db")
    store.create(name="Через3", birthday="1990-07-01")   # от 28.06 → 3 дня
    store.create(name="Через4", birthday="1990-07-02")   # 4 дня
    store.create(name="БезДР")                            # birthday None
    up = store.upcoming_birthdays(within_days=3, today=date(2026, 6, 28))
    assert {c["name"] for c in up} == {"Через3"}


# --- интеграция через IntentRouter ------------------------------------------

@pytest.fixture
def router(tmp_path):
    contacts = ContactStore(tmp_path / "c.db")
    alog = ActionLog(tmp_path / "a.db")
    r = IntentRouter(None, None, calendar=None, action_log=alog, inbox=None, contacts=contacts)
    return r, contacts, alog


def test_create_contact_then_query_finds(router):
    r, contacts, _ = router
    res = r.resolve({"intent": "create_contact", "confidence": "high",
                     "name": "Мама", "birthday": "1965-04-12"})
    assert res.kind == "execute"
    reply = r.execute(res.action)
    assert "Мама" in reply
    found = contacts.find("мама")
    assert len(found) == 1 and found[0]["birthday"] == "1965-04-12"


def test_create_contact_logged_as_contact(router):
    r, _, alog = router
    r.execute(r.resolve({"intent": "create_contact", "confidence": "high", "name": "Петя"}).action)
    rec = alog.latest_active()
    assert rec["entity_type"] == "contact" and rec["action"] == "create"


def test_update_contact_sets_last_contact_today_and_appends_note(router):
    r, contacts, _ = router
    c = contacts.create(name="Аня", notes="старое")
    res = r.resolve({"intent": "update_contact", "confidence": "high",
                     "name_hint": "аня", "note": "вышла на новую работу"})
    assert res.kind == "execute"
    r.execute(res.action)
    got = contacts.get(c["id"])
    assert got["last_contact_date"] == date.today().isoformat()
    assert got["notes"] == "старое\nвышла на новую работу"


def test_update_contact_without_note_only_touches_date(router):
    r, contacts, _ = router
    c = contacts.create(name="Боб", notes="без изменений")
    r.execute(r.resolve({"intent": "update_contact", "confidence": "high", "name_hint": "боб"}).action)
    got = contacts.get(c["id"])
    assert got["last_contact_date"] == date.today().isoformat()
    assert got["notes"] == "без изменений"


def test_update_contact_not_found_is_message(router):
    r, *_ = router
    res = r.resolve({"intent": "update_contact", "confidence": "high", "name_hint": "никого"})
    assert res.kind == "message"


def test_delete_contact_requires_confirmation(router):
    r, contacts, _ = router
    contacts.create(name="Дядя Вася")
    res = r.resolve({"intent": "delete_contact", "confidence": "high", "name_hint": "вася"})
    assert res.kind == "confirm"
    assert "Вася" in res.label


def test_query_contacts_by_name(router):
    r, contacts, _ = router
    contacts.create(name="Иван Петров")
    contacts.create(name="Мария")
    res = r.resolve({"intent": "query_contacts", "confidence": "high",
                     "filter": "by_name", "name": "петров"})
    assert res.kind == "execute"
    reply = r.execute(res.action)
    assert "Иван Петров" in reply and "Мария" not in reply


def test_query_contacts_upcoming_birthdays(router):
    r, contacts, _ = router
    soon = (date.today() + timedelta(days=1)).replace(year=1990).isoformat()
    contacts.create(name="Скоро", birthday=soon)
    res = r.resolve({"intent": "query_contacts", "confidence": "high", "filter": "upcoming_birthdays"})
    reply = r.execute(res.action)
    assert "Скоро" in reply


# --- undo ---------------------------------------------------------------------

def test_create_contact_then_undo_removes(router):
    r, contacts, _ = router
    r.execute(r.resolve({"intent": "create_contact", "confidence": "high", "name": "Тест"}).action)
    assert len(contacts.list()) == 1
    r.execute(r.resolve({"intent": "undo_last"}).action)
    assert contacts.list() == []


def test_update_contact_then_undo_restores_previous(router):
    r, contacts, _ = router
    c = contacts.create(name="Олег", last_contact_date="2026-01-01", notes="старая заметка")
    r.execute(r.resolve({"intent": "update_contact", "confidence": "high",
                         "name_hint": "олег", "note": "новая"}).action)
    r.execute(r.resolve({"intent": "undo_last"}).action)
    restored = contacts.get(c["id"])
    assert restored["last_contact_date"] == "2026-01-01"
    assert restored["notes"] == "старая заметка"


def test_delete_contact_then_undo_restores(router):
    r, contacts, _ = router
    c = contacts.create(name="Бабушка", birthday="1940-03-03", notes="печёт пироги")
    r.execute({"type": "delete_contact", "contact_id": c["id"], "name": c["name"]})
    assert contacts.list() == []
    r.execute(r.resolve({"intent": "undo_last"}).action)
    items = contacts.list()
    assert len(items) == 1
    assert items[0]["name"] == "Бабушка"
    assert items[0]["birthday"] == "1940-03-03"
    assert items[0]["notes"] == "печёт пироги"
