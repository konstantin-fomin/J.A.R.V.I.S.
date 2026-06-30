"""Тесты §3-bis: bulk-создание платежей из свободного текста (create_bills_batch).

Пользователь пишет списком («запиши платежи: 40000 за дом 1 июля, 13700 пэй 2
июля, …») — парсер извлекает массив, роутер ВСЕГДА показывает предпросмотр (это
несколько финансовых записей, ошибка дороже одной задачи), а по «Да» создаёт
каждый платёж через штатный BillStore.create_template с отдельной записью в
журнале (каждая отменяема undo_last по отдельности). Сеть/LLM не дёргаем —
llm.chat инъектируем фейком.
"""
import json

import pytest

from bills import BillStore
from intents import INTENTS, RISK_LEVELS, IntentRouter, parse_intent
from logger import ActionLog
from tasks import TaskStore

TODAY = "2026-06-30"


class FakeLLM:
    """Фейковый клиент: .chat() возвращает заранее заданную строку-ответ модели."""

    def __init__(self, response: str):
        self._response = response

    def chat(self, *_):
        return self._response


@pytest.fixture
def router(tmp_path):
    tasks = TaskStore(tmp_path / "t.db")
    bills = BillStore(tmp_path / "b.db")
    alog = ActionLog(tmp_path / "a.db")
    r = IntentRouter(tasks, bills, calendar=None, action_log=alog)
    return r, bills, alog


# --- Интент зарегистрирован -------------------------------------------------

def test_create_bills_batch_is_known_intent_with_risk_level():
    assert "create_bills_batch" in INTENTS
    assert RISK_LEVELS["create_bills_batch"] in {"safe", "medium", "dangerous"}


# --- parse_intent: извлекает массив платежей --------------------------------

def test_parse_extracts_bills_array():
    payload = {
        "intent": "create_bills_batch",
        "confidence": "high",
        "bills": [
            {"name": "дом", "amount": 40000, "day_of_month": 1},
            {"name": "пэй", "amount": 13700, "day_of_month": 2},
        ],
    }
    llm = FakeLLM(json.dumps(payload))
    data = parse_intent(llm, "запиши платежи: 40000 за дом 1 июля, 13700 пэй 2 июля", TODAY)
    assert data["intent"] == "create_bills_batch"
    assert len(data["bills"]) == 2
    assert data["bills"][0]["name"] == "дом"
    assert data["bills"][1]["day_of_month"] == 2


def test_prompt_documents_create_bills_batch():
    """Замок: промпт парсера должен описывать create_bills_batch (иначе модель
    не научится его выдавать)."""
    from intents import PROMPT

    assert "create_bills_batch" in PROMPT


# --- resolve: всегда предпросмотр (confirm), даже при high confidence -------

def test_resolve_batch_returns_confirm_with_preview(router):
    r, bills, _ = router
    data = {
        "intent": "create_bills_batch",
        "confidence": "high",
        "bills": [
            {"name": "дом", "amount": 40000, "day_of_month": 1},
            {"name": "кредит", "amount": 14500, "day_of_month": 14},
        ],
    }
    res = r.resolve(data)
    assert res.kind == "confirm"  # обязательный предпросмотр, не execute
    assert res.action["type"] == "create_bills_batch"
    assert len(res.action["items"]) == 2
    # предпросмотр перечисляет распарсенные платежи
    assert "дом" in res.label and "кредит" in res.label
    assert "1" in res.label and "14" in res.label
    # пока ничего не создано — только предпросмотр
    assert bills.list_templates() == []


def test_resolve_single_payment_also_confirms(router):
    """Один платёж (не список) — работает так же: предпросмотр из одного пункта."""
    r, _, _ = router
    data = {"intent": "create_bills_batch", "confidence": "high",
            "bills": [{"name": "интернет", "amount": 800, "day_of_month": 5}]}
    res = r.resolve(data)
    assert res.kind == "confirm"
    assert len(res.action["items"]) == 1


def test_resolve_batch_empty_is_honest_message_not_silent(router):
    """Похоже на список платежей, но распарсить нечего → честный отказ-сообщение,
    не молчаливый no-op и не chat."""
    r, bills, _ = router
    res = r.resolve({"intent": "create_bills_batch", "confidence": "high", "bills": []})
    assert res.kind == "message"
    assert "по одному" in res.text.lower() or "проще" in res.text.lower()
    assert bills.list_templates() == []


def test_resolve_batch_filters_invalid_items(router):
    """Кривые элементы (без имени, без дня, день вне 1-31) отсеиваются."""
    r, _, _ = router
    data = {
        "intent": "create_bills_batch",
        "confidence": "high",
        "bills": [
            {"name": "дом", "amount": 40000, "day_of_month": 1},   # ок
            {"name": "", "amount": 100, "day_of_month": 5},        # нет имени
            {"name": "мусор", "amount": 100, "day_of_month": 99},  # день вне диапазона
            {"name": "вода", "amount": None, "day_of_month": 10},  # сумма null — ок, имя есть
        ],
    }
    res = r.resolve(data)
    assert res.kind == "confirm"
    names = [it["name"] for it in res.action["items"]]
    assert names == ["дом", "вода"]


# --- execute: создаёт все шаблоны через BillStore.create_template -----------

def test_execute_batch_creates_all_templates(router):
    r, bills, _ = router
    items = [
        {"name": "дом", "amount": 40000, "day_of_month": 1},
        {"name": "пэй", "amount": 13700, "day_of_month": 2},
        {"name": "кредит", "amount": 14500, "day_of_month": 14},
        {"name": "кредитка", "amount": 75600, "day_of_month": 5},
        {"name": "машина", "amount": 65000, "day_of_month": 15},
    ]
    reply = r.execute({"type": "create_bills_batch", "items": items})
    templates = bills.list_templates()
    assert len(templates) == 5
    by_name = {t["name"]: t for t in templates}
    assert by_name["дом"]["amount"] == 40000 and by_name["дом"]["day_of_month"] == 1
    assert by_name["кредит"]["day_of_month"] == 14
    assert by_name["машина"]["amount"] == 65000
    assert "дом" in reply and "машина" in reply


def test_execute_batch_logs_each_template_separately(router):
    r, bills, alog = router
    items = [
        {"name": "дом", "amount": 40000, "day_of_month": 1},
        {"name": "пэй", "amount": 13700, "day_of_month": 2},
    ]
    r.execute({"type": "create_bills_batch", "items": items})
    recs = alog.actions_between("0000", "9999")
    bill_recs = [x for x in recs if x["entity_type"] == "bill_template"]
    assert len(bill_recs) == 2
    assert all(x["action"] == "create" for x in bill_recs)


def test_batch_template_is_individually_undoable(router):
    """Пакет даёт N отдельных записей в логе — undo_last снимает ровно последнюю."""
    r, bills, _ = router
    items = [
        {"name": "дом", "amount": 40000, "day_of_month": 1},
        {"name": "пэй", "amount": 13700, "day_of_month": 2},
    ]
    r.execute({"type": "create_bills_batch", "items": items})
    assert len(bills.list_templates()) == 2

    undo = r.resolve({"intent": "undo_last"})
    r.execute(undo.action)
    remaining = [t["name"] for t in bills.list_templates()]
    assert remaining == ["дом"]  # снят только последний («пэй»)


# --- BillStore.delete_template (нужен для undo) ------------------------------

def test_delete_template_removes_template_and_instances(tmp_path):
    bills = BillStore(tmp_path / "b.db")
    t = bills.create_template(name="дом", day_of_month=1, amount=40000)
    bills.ensure_month("2026-07")
    assert bills.list_instances("2026-07")  # инстанс создан

    bills.delete_template(t["id"])
    assert bills.get_template(t["id"]) is None
    assert bills.list_instances("2026-07") == []  # инстансы тоже убраны
