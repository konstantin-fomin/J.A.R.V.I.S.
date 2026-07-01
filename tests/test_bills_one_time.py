"""Тесты разовых платежей (one_time_bills) — отдельная сущность от bill_template/
bill_instance (§3-bis-2): у BillStore нет способа выразить «плати один раз
14 июля», только «плати N-го числа каждый месяц» (day_of_month без года/месяца).
one_time_bills — прямая запись с конкретной due_date, без template_id.

Единый API (list_month/get_bill/set_bill_status/due_on) объединяет обе сущности
для вызывающего кода (дашборд/бот не должны знать о разнице) через составной
id: "r<id>" — regular (bill_instances), "o<id>" — one_time (one_time_bills).
"""
import asyncio
from types import SimpleNamespace

import pytest

import config
from bills import BillStore
from bot.handlers import BILL_PAID_PREFIX, Handlers
from inbox import InboxStore
from logger import ActionLog
from tasks import TaskStore


def test_create_one_time_stores_fields(tmp_path):
    bills = BillStore(tmp_path / "b.db")
    b = bills.create_one_time(name="кредит", due_date="2026-07-14", amount=14500, category="долги")
    assert b["name"] == "кредит"
    assert b["due_date"] == "2026-07-14"
    assert b["amount"] == 14500
    assert b["category"] == "долги"
    assert b["status"] == "pending"
    assert b["paid_at"] is None


def test_create_one_time_amount_and_category_optional(tmp_path):
    bills = BillStore(tmp_path / "b.db")
    b = bills.create_one_time(name="разное", due_date="2026-07-01")
    assert b["amount"] is None
    assert b["category"] is None


def test_get_one_time_unknown_id_is_none(tmp_path):
    bills = BillStore(tmp_path / "b.db")
    assert bills.get_one_time(999) is None


def test_list_one_time_filters_by_month(tmp_path):
    bills = BillStore(tmp_path / "b.db")
    bills.create_one_time(name="июльский", due_date="2026-07-14")
    bills.create_one_time(name="августовский", due_date="2026-08-01")
    names = [b["name"] for b in bills.list_one_time("2026-07")]
    assert names == ["июльский"]


def test_list_one_time_without_month_returns_all(tmp_path):
    bills = BillStore(tmp_path / "b.db")
    bills.create_one_time(name="a", due_date="2026-07-14")
    bills.create_one_time(name="b", due_date="2026-08-01")
    assert len(bills.list_one_time()) == 2


def test_set_one_time_status_marks_paid_with_timestamp(tmp_path):
    bills = BillStore(tmp_path / "b.db")
    b = bills.create_one_time(name="кредит", due_date="2026-07-14", amount=14500)
    updated = bills.set_one_time_status(b["id"], "paid")
    assert updated["status"] == "paid"
    assert updated["paid_at"] is not None


def test_delete_one_time_removes_row(tmp_path):
    bills = BillStore(tmp_path / "b.db")
    b = bills.create_one_time(name="кредит", due_date="2026-07-14")
    bills.delete_one_time(b["id"])
    assert bills.get_one_time(b["id"]) is None


# --- list_month: единый список регулярных + разовых -----------------------------

def test_list_month_combines_regular_and_one_time(tmp_path):
    bills = BillStore(tmp_path / "b.db")
    bills.create_template(name="аренда", day_of_month=5, amount=30000)
    bills.ensure_month("2026-07")
    bills.create_one_time(name="кредит", due_date="2026-07-14", amount=14500)

    combined = bills.list_month("2026-07")
    names = sorted(b["name"] for b in combined)
    assert names == ["аренда", "кредит"]


def test_list_month_items_have_composite_id_and_kind(tmp_path):
    bills = BillStore(tmp_path / "b.db")
    bills.create_template(name="аренда", day_of_month=5, amount=30000)
    bills.ensure_month("2026-07")
    bills.create_one_time(name="кредит", due_date="2026-07-14", amount=14500)

    combined = bills.list_month("2026-07")
    by_name = {b["name"]: b for b in combined}
    assert by_name["аренда"]["kind"] == "regular"
    assert by_name["аренда"]["id"].startswith("r")
    assert by_name["кредит"]["kind"] == "one_time"
    assert by_name["кредит"]["id"].startswith("o")


def test_list_month_sorted_by_due_date(tmp_path):
    bills = BillStore(tmp_path / "b.db")
    bills.create_template(name="аренда", day_of_month=20, amount=30000)
    bills.ensure_month("2026-07")
    bills.create_one_time(name="кредит", due_date="2026-07-05", amount=14500)

    combined = bills.list_month("2026-07")
    assert [b["name"] for b in combined] == ["кредит", "аренда"]


def test_list_month_excludes_other_months(tmp_path):
    bills = BillStore(tmp_path / "b.db")
    bills.create_one_time(name="июльский", due_date="2026-07-14")
    bills.create_one_time(name="августовский", due_date="2026-08-01")
    combined = bills.list_month("2026-07")
    assert [b["name"] for b in combined] == ["июльский"]


# --- get_bill / set_bill_status: единый доступ по составному id -----------------

def test_get_bill_regular(tmp_path):
    bills = BillStore(tmp_path / "b.db")
    bills.create_template(name="аренда", day_of_month=5, amount=30000)
    bills.ensure_month("2026-07")
    instance_id = bills.list_instances("2026-07")[0]["id"]
    bill = bills.get_bill(f"r{instance_id}")
    assert bill["name"] == "аренда"
    assert bill["kind"] == "regular"


def test_get_bill_one_time(tmp_path):
    bills = BillStore(tmp_path / "b.db")
    b = bills.create_one_time(name="кредит", due_date="2026-07-14", amount=14500)
    bill = bills.get_bill(f"o{b['id']}")
    assert bill["name"] == "кредит"
    assert bill["kind"] == "one_time"


def test_get_bill_unknown_composite_id_is_none(tmp_path):
    bills = BillStore(tmp_path / "b.db")
    assert bills.get_bill("r999") is None
    assert bills.get_bill("o999") is None


def test_get_bill_malformed_id_raises_value_error(tmp_path):
    bills = BillStore(tmp_path / "b.db")
    with pytest.raises(ValueError):
        bills.get_bill("x5")


def test_set_bill_status_regular(tmp_path):
    bills = BillStore(tmp_path / "b.db")
    bills.create_template(name="аренда", day_of_month=5, amount=30000)
    bills.ensure_month("2026-07")
    instance_id = bills.list_instances("2026-07")[0]["id"]
    updated = bills.set_bill_status(f"r{instance_id}", "paid")
    assert updated["status"] == "paid"
    assert updated["kind"] == "regular"


def test_set_bill_status_one_time(tmp_path):
    bills = BillStore(tmp_path / "b.db")
    b = bills.create_one_time(name="кредит", due_date="2026-07-14", amount=14500)
    updated = bills.set_bill_status(f"o{b['id']}", "paid")
    assert updated["status"] == "paid"
    assert updated["kind"] == "one_time"


# --- due_on: комбинированный запрос по конкретной дате (напоминания/§today) -----

def test_due_on_combines_regular_and_one_time(tmp_path):
    bills = BillStore(tmp_path / "b.db")
    bills.create_template(name="аренда", day_of_month=14, amount=30000)
    bills.ensure_month("2026-07")
    bills.create_one_time(name="кредит", due_date="2026-07-14", amount=14500)

    due = bills.due_on("2026-07-14", status="pending")
    names = sorted(b["name"] for b in due)
    assert names == ["аренда", "кредит"]


def test_due_on_filters_by_status(tmp_path):
    bills = BillStore(tmp_path / "b.db")
    b = bills.create_one_time(name="кредит", due_date="2026-07-14")
    bills.set_one_time_status(b["id"], "paid")
    due = bills.due_on("2026-07-14", status="pending")
    assert due == []


# --- Handlers.mark_paid: работает одинаково для regular и one_time (§3-bis-2) ---
# mark_paid раньше писал в BillStore напрямую (не журналировалось/не отменялось) —
# приведено к тому же паттерну, что mark_task_done: через IntentRouter.execute.

@pytest.fixture(autouse=True)
def _no_user_restriction(monkeypatch):
    monkeypatch.setattr(config, "ALLOWED_USER_ID", None)


class FakeQuery:
    def __init__(self, data, reply_markup=None):
        self.data = data
        self.message = SimpleNamespace(reply_markup=reply_markup)
        self.answers: list[str | None] = []
        self.edited_markups: list = []

    async def answer(self, text=None):
        self.answers.append(text)

    async def edit_message_reply_markup(self, reply_markup=None):
        self.edited_markups.append(reply_markup)
        self.message.reply_markup = reply_markup


class FakeCallbackUpdate:
    def __init__(self, query):
        self.callback_query = query
        self.effective_user = None


def _handlers(tmp_path):
    tasks = TaskStore(tmp_path / "t.db")
    bills = BillStore(tmp_path / "b.db")
    inbox = InboxStore(tmp_path / "i.db")
    alog = ActionLog(tmp_path / "a.db")
    h = Handlers(memory=None, llm=None, facts=None, bills=bills, tasks=tasks,
                calendar=None, action_log=alog, inbox=inbox)  # type: ignore[arg-type]
    return h, bills, alog


def test_mark_paid_regular_bill_marks_paid_and_removes_button(tmp_path):
    h, bills, _ = _handlers(tmp_path)
    bills.create_template(name="аренда", day_of_month=5, amount=30000)
    bills.ensure_month("2026-07")
    bill_id = f"r{bills.list_instances('2026-07')[0]['id']}"
    query = FakeQuery(f"{BILL_PAID_PREFIX}{bill_id}")

    asyncio.run(h.mark_paid(FakeCallbackUpdate(query), context=None))

    assert bills.get_bill(bill_id)["status"] == "paid"
    assert query.answers
    assert query.edited_markups[-1] is None


def test_mark_paid_one_time_bill_marks_paid(tmp_path):
    h, bills, _ = _handlers(tmp_path)
    b = bills.create_one_time(name="кредит", due_date="2026-07-14", amount=14500)
    bill_id = f"o{b['id']}"
    query = FakeQuery(f"{BILL_PAID_PREFIX}{bill_id}")

    asyncio.run(h.mark_paid(FakeCallbackUpdate(query), context=None))

    assert bills.get_bill(bill_id)["status"] == "paid"


def test_mark_paid_is_logged_and_undoable_for_one_time(tmp_path):
    h, bills, alog = _handlers(tmp_path)
    b = bills.create_one_time(name="кредит", due_date="2026-07-14", amount=14500)
    bill_id = f"o{b['id']}"
    query = FakeQuery(f"{BILL_PAID_PREFIX}{bill_id}")

    asyncio.run(h.mark_paid(FakeCallbackUpdate(query), context=None))
    rec = alog.latest_active()
    assert rec["entity_type"] == "bill" and rec["action"] == "mark_paid"

    undo = h.router.resolve({"intent": "undo_last"})
    h.router.execute(undo.action)
    assert bills.get_bill(bill_id)["status"] == "pending"


def test_mark_paid_is_logged_and_undoable_for_regular(tmp_path):
    h, bills, alog = _handlers(tmp_path)
    bills.create_template(name="аренда", day_of_month=5, amount=30000)
    bills.ensure_month("2026-07")
    bill_id = f"r{bills.list_instances('2026-07')[0]['id']}"
    query = FakeQuery(f"{BILL_PAID_PREFIX}{bill_id}")

    asyncio.run(h.mark_paid(FakeCallbackUpdate(query), context=None))
    rec = alog.latest_active()
    assert rec["entity_type"] == "bill" and rec["action"] == "mark_paid"

    undo = h.router.resolve({"intent": "undo_last"})
    h.router.execute(undo.action)
    assert bills.get_bill(bill_id)["status"] == "pending"


def test_mark_paid_already_paid_answers_gracefully(tmp_path):
    h, bills, _ = _handlers(tmp_path)
    b = bills.create_one_time(name="кредит", due_date="2026-07-14")
    bills.set_one_time_status(b["id"], "paid")
    query = FakeQuery(f"{BILL_PAID_PREFIX}o{b['id']}")

    asyncio.run(h.mark_paid(FakeCallbackUpdate(query), context=None))
    assert "уже" in (query.answers[-1] or "").lower()


def test_mark_paid_unknown_bill_answers_gracefully(tmp_path):
    h, *_ = _handlers(tmp_path)
    query = FakeQuery(f"{BILL_PAID_PREFIX}o999")
    asyncio.run(h.mark_paid(FakeCallbackUpdate(query), context=None))
    assert query.answers  # ответили, не упали


def test_mark_paid_malformed_id_answers_gracefully(tmp_path):
    h, *_ = _handlers(tmp_path)
    query = FakeQuery(f"{BILL_PAID_PREFIX}xyz")
    asyncio.run(h.mark_paid(FakeCallbackUpdate(query), context=None))
    assert query.answers  # ответили, не упали
