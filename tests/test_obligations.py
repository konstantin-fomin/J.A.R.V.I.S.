"""Тесты обязательств (§19.1): ObligationStore как самостоятельная SQLite-база
(стиль tasks.py/contacts.py) + интеграция intent→действие/undo через реальный
IntentRouter + живой сценарий job follow_up_obligations в тихие часы.

Сеть/Telegram/LLM не дёргаем; время job'а управляем окном тихих часов в config.
"""
import asyncio
from datetime import date, datetime, timedelta

import pytest

from bills import BillStore
from contacts import ContactStore
from intents import RISK_LEVELS, IntentRouter
from logger import ActionLog
from obligations import ObligationStore
from tasks import TaskStore


# --- ObligationStore: хранилище ---------------------------------------------

def test_create_defaults_open_and_today_since(tmp_path):
    obl = ObligationStore(tmp_path / "o.db")
    o = obl.create(title="отчёт по проекту", person="Петя", direction="waiting_on")
    assert o["status"] == "open"
    assert o["direction"] == "waiting_on"
    assert o["person"] == "Петя"
    assert o["since_date"] == date.today().isoformat()  # по умолчанию сегодня
    assert o["follow_up_date"] is None
    assert o["related_project"] is None


def test_create_keeps_explicit_fields(tmp_path):
    obl = ObligationStore(tmp_path / "o.db")
    o = obl.create(title="вернуть долг", person="Маша", direction="i_owe",
                   since_date="2026-06-01", follow_up_date="2026-07-01",
                   related_project="ремонт")
    assert o["since_date"] == "2026-06-01"
    assert o["follow_up_date"] == "2026-07-01"
    assert o["related_project"] == "ремонт"
    assert o["direction"] == "i_owe"


def test_list_filters_by_direction_and_person_and_status(tmp_path):
    obl = ObligationStore(tmp_path / "o.db")
    obl.create(title="отчёт", person="Петя", direction="waiting_on")
    obl.create(title="долг", person="Маша", direction="i_owe")
    done = obl.create(title="старое", person="Петя", direction="waiting_on")
    obl.update(done["id"], status="done")

    assert len(obl.list()) == 3
    assert {o["title"] for o in obl.list(direction="waiting_on")} == {"отчёт", "старое"}
    assert {o["title"] for o in obl.list(person="петя")} == {"отчёт", "старое"}  # подстрока, регистр
    assert {o["title"] for o in obl.list(status="open")} == {"отчёт", "долг"}
    assert {o["title"] for o in obl.list(direction="waiting_on", status="open")} == {"отчёт"}


def test_find_substring_open_only(tmp_path):
    obl = ObligationStore(tmp_path / "o.db")
    obl.create(title="отчёт по лендингу", person="Петя", direction="waiting_on")
    closed = obl.create(title="отчёт по смете", person="Петя", direction="waiting_on")
    obl.update(closed["id"], status="done")
    found = obl.find("отчёт", status="open")
    assert len(found) == 1
    assert found[0]["title"] == "отчёт по лендингу"


def test_due_followups_returns_open_due_today_or_earlier(tmp_path):
    obl = ObligationStore(tmp_path / "o.db")
    today = date.today()
    due = obl.create(title="напомнить Пете", person="Петя", direction="waiting_on",
                     follow_up_date=today.isoformat())
    obl.create(title="ещё рано", person="Маша", direction="waiting_on",
               follow_up_date=(today + timedelta(days=5)).isoformat())
    obl.create(title="без даты", person="Ваня", direction="i_owe")
    closed = obl.create(title="закрытое", person="Петя", direction="waiting_on",
                        follow_up_date=today.isoformat())
    obl.update(closed["id"], status="done")

    out = obl.due_followups(today)
    assert [o["id"] for o in out] == [due["id"]]


def test_update_and_delete(tmp_path):
    obl = ObligationStore(tmp_path / "o.db")
    o = obl.create(title="x", person="P", direction="i_owe")
    obl.update(o["id"], status="done")
    assert obl.get(o["id"])["status"] == "done"
    assert obl.delete(o["id"]) is True
    assert obl.get(o["id"]) is None


# --- Интеграция через IntentRouter ------------------------------------------

@pytest.fixture
def router(tmp_path):
    tasks = TaskStore(tmp_path / "t.db")
    bills = BillStore(tmp_path / "b.db")
    contacts = ContactStore(tmp_path / "c.db")
    obl = ObligationStore(tmp_path / "o.db")
    alog = ActionLog(tmp_path / "a.db")
    r = IntentRouter(tasks, bills, calendar=None, action_log=alog,
                     contacts=contacts, obligations=obl)
    return r, obl


def test_risk_levels_registered():
    assert RISK_LEVELS["create_obligation"] == "medium"
    assert RISK_LEVELS["query_obligations"] == "safe"
    assert RISK_LEVELS["complete_obligation"] == "medium"
    assert RISK_LEVELS["delete_obligation"] == "dangerous"


def test_create_obligation_executes_and_stores(router):
    r, obl = router
    res = r.resolve({"intent": "create_obligation", "confidence": "high",
                     "title": "отчёт", "person": "Петя", "direction": "waiting_on"})
    assert res.kind == "execute"
    reply = r.execute(res.action)
    assert "Петя" in reply or "отчёт" in reply
    items = obl.list()
    assert len(items) == 1 and items[0]["status"] == "open"


def test_create_obligation_low_confidence_confirms(router):
    r, _ = router
    res = r.resolve({"intent": "create_obligation", "confidence": "low",
                     "title": "долг", "person": "Маша", "direction": "i_owe"})
    assert res.kind == "confirm"


def test_query_obligations_filters_by_direction(router):
    r, obl = router
    obl.create(title="отчёт", person="Петя", direction="waiting_on")
    obl.create(title="долг", person="Маша", direction="i_owe")
    res = r.resolve({"intent": "query_obligations", "confidence": "high",
                     "direction": "waiting_on"})
    assert res.kind == "execute"
    reply = r.execute(res.action)
    assert "отчёт" in reply and "долг" not in reply


def test_complete_obligation_closes_match(router):
    r, obl = router
    o = obl.create(title="отчёт по смете", person="Петя", direction="waiting_on")
    res = r.resolve({"intent": "complete_obligation", "confidence": "high",
                     "title_hint": "отчёт"})
    assert res.kind == "execute"
    r.execute(res.action)
    assert obl.get(o["id"])["status"] == "done"


def test_complete_obligation_no_match_is_message(router):
    r, _ = router
    res = r.resolve({"intent": "complete_obligation", "confidence": "high",
                     "title_hint": "ничего"})
    assert res.kind == "message"


def test_delete_obligation_is_dangerous_confirm(router):
    r, obl = router
    obl.create(title="долг", person="Маша", direction="i_owe")
    res = r.resolve({"intent": "delete_obligation", "confidence": "high",
                     "title_hint": "долг"})
    assert res.kind == "confirm"  # dangerous → всегда подтверждение


def test_create_obligation_is_undoable(router):
    r, obl = router
    r.execute(r.resolve({"intent": "create_obligation", "confidence": "high",
                         "title": "отчёт", "person": "Петя",
                         "direction": "waiting_on"}).action)
    assert len(obl.list()) == 1
    r.execute(r.resolve({"intent": "undo_last"}).action)
    assert obl.list() == []  # создание откатилось


def test_complete_obligation_is_undoable(router):
    r, obl = router
    o = obl.create(title="отчёт", person="Петя", direction="waiting_on")
    r.execute(r.resolve({"intent": "complete_obligation", "confidence": "high",
                         "title_hint": "отчёт"}).action)
    assert obl.get(o["id"])["status"] == "done"
    r.execute(r.resolve({"intent": "undo_last"}).action)
    assert obl.get(o["id"])["status"] == "open"  # вернулся прежний статус


def test_delete_obligation_is_undoable(router):
    r, obl = router
    o = obl.create(title="долг", person="Маша", direction="i_owe")
    res = r.resolve({"intent": "delete_obligation", "confidence": "high",
                     "title_hint": "долг"})
    r.execute(res.action)
    assert obl.list() == []
    r.execute(r.resolve({"intent": "undo_last"}).action)
    restored = obl.list()
    assert len(restored) == 1 and restored[0]["title"] == "долг"


# --- Живой сценарий: follow_up_obligations и тихие часы ----------------------

class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, **kwargs):
        self.sent.append(kwargs)


class FakeJob:
    def __init__(self, data, name):
        self.data = data
        self.name = name


class FakeJobQueue:
    def __init__(self):
        self.scheduled = []

    def run_once(self, callback, when, data=None, name=None):
        self.scheduled.append({"callback": callback, "when": when, "data": data, "name": name})


class FakeContext:
    def __init__(self, data, name, bot, job_queue):
        self.job = FakeJob(data, name)
        self.bot = bot
        self.job_queue = job_queue


def _quiet_window_around_now(monkeypatch):
    import config
    now = datetime.now()
    monkeypatch.setattr(config, "QUIET_HOURS_START", (now - timedelta(hours=1)).strftime("%H:%M"))
    monkeypatch.setattr(config, "QUIET_HOURS_END", (now + timedelta(hours=1)).strftime("%H:%M"))


def _open_window_now(monkeypatch):
    import config
    now = datetime.now()
    monkeypatch.setattr(config, "QUIET_HOURS_START", (now + timedelta(hours=2)).strftime("%H:%M"))
    monkeypatch.setattr(config, "QUIET_HOURS_END", (now + timedelta(hours=3)).strftime("%H:%M"))


def test_follow_up_defers_in_quiet_hours_and_delivers_after(tmp_path, monkeypatch):
    import config
    from bot import telegram_bot

    obl = ObligationStore(tmp_path / "o.db")
    obl.create(title="отчёт", person="Петя", direction="waiting_on",
               follow_up_date=date.today().isoformat())

    monkeypatch.setattr(config, "ALLOWED_USER_ID", 42)
    bot = FakeBot()
    jq = FakeJobQueue()
    ctx = FakeContext(data=obl, name="follow_up_obligations", bot=bot, job_queue=jq)

    _quiet_window_around_now(monkeypatch)
    asyncio.run(telegram_bot.follow_up_obligations(ctx))
    assert bot.sent == []
    assert len(jq.scheduled) == 1
    deferred = jq.scheduled[0]
    assert deferred["callback"] is telegram_bot.follow_up_obligations

    _open_window_now(monkeypatch)
    ctx2 = FakeContext(data=deferred["data"], name="follow_up_obligations_deferred",
                       bot=bot, job_queue=jq)
    asyncio.run(deferred["callback"](ctx2))
    assert len(bot.sent) == 1
    assert bot.sent[0]["chat_id"] == 42
    assert "Петя" in bot.sent[0]["text"]


def test_follow_up_silent_when_nothing_due(tmp_path, monkeypatch):
    import config
    from bot import telegram_bot

    obl = ObligationStore(tmp_path / "o.db")
    obl.create(title="рано", person="Маша", direction="waiting_on",
               follow_up_date=(date.today() + timedelta(days=10)).isoformat())

    monkeypatch.setattr(config, "ALLOWED_USER_ID", 42)
    monkeypatch.setattr(config, "QUIET_HOURS_START", "09:00")
    monkeypatch.setattr(config, "QUIET_HOURS_END", "09:00")  # тихих часов нет
    bot = FakeBot()
    ctx = FakeContext(data=obl, name="follow_up_obligations", bot=bot, job_queue=FakeJobQueue())
    asyncio.run(telegram_bot.follow_up_obligations(ctx))
    assert bot.sent == []
