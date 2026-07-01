"""Тесты reply-клавиатуры (эргономика бота, п.3): под полем ввода — три кнопки
«📋 Задачи» / «💰 Платежи» / «📥 Инбокс». Нажатие шлёт обычное текстовое
сообщение с этим текстом — handle_text перехватывает его ДО intent-парсинга
(LLM не дёргаем) и рендерит тот же список, что /bills, NL query_tasks и /inbox.
Клавиатура вешается на ответ /start (постоянная, resize_keyboard).
"""
import asyncio

import pytest
from telegram import ReplyKeyboardMarkup

import config
from bills import BillStore
from bot.handlers import (
    BTN_BILLS,
    BTN_INBOX,
    BTN_TASKS,
    Handlers,
    main_reply_keyboard,
)
from inbox import InboxStore
from logger import ActionLog
from tasks import TaskStore


@pytest.fixture(autouse=True)
def _no_user_restriction(monkeypatch):
    monkeypatch.setattr(config, "ALLOWED_USER_ID", None)


class _BoomLLM:
    """LLM, который падает при любом обращении — кнопки не должны его трогать."""

    def chat(self, *args, **kwargs):
        raise AssertionError("LLM не должен вызываться для кнопок клавиатуры")


class FakeMessage:
    def __init__(self, text=""):
        self.text = text
        self.forward_origin = None
        self.calls: list[dict] = []

    async def reply_text(self, text, parse_mode=None, **kwargs):
        self.calls.append({"text": text, "parse_mode": parse_mode, **kwargs})

    class _Chat:
        async def send_action(self, *args, **kwargs):
            pass

    chat = _Chat()


class FakeUpdate:
    def __init__(self, message):
        self.message = message
        self.effective_user = None


def _handlers(tmp_path):
    tasks = TaskStore(tmp_path / "t.db")
    bills = BillStore(tmp_path / "b.db")
    inbox = InboxStore(tmp_path / "i.db")
    alog = ActionLog(tmp_path / "a.db")
    h = Handlers(memory=None, llm=_BoomLLM(), facts=None, bills=bills, tasks=tasks,
                calendar=None, action_log=alog, inbox=inbox)  # type: ignore[arg-type]
    return h, tasks, bills, inbox, alog


# --- main_reply_keyboard: чистая функция ---------------------------------------

def test_main_reply_keyboard_has_three_buttons():
    markup = main_reply_keyboard()
    assert isinstance(markup, ReplyKeyboardMarkup)
    labels = [b.text for row in markup.keyboard for b in row]
    assert labels == [BTN_TASKS, BTN_BILLS, BTN_INBOX]


# --- /start вешает клавиатуру ---------------------------------------------------

def test_start_attaches_main_keyboard(tmp_path):
    h, *_ = _handlers(tmp_path)
    message = FakeMessage()
    asyncio.run(h.start(FakeUpdate(message), context=None))
    assert isinstance(message.calls[0]["reply_markup"], ReplyKeyboardMarkup)


# --- handle_text: перехват кнопок -----------------------------------------------

def test_tasks_button_shows_task_list(tmp_path):
    h, tasks, *_ = _handlers(tmp_path)
    tasks.create("купить молоко")
    message = FakeMessage(BTN_TASKS)
    asyncio.run(h.handle_text(FakeUpdate(message), context=None))
    assert "купить молоко" in message.calls[0]["text"]
    assert message.calls[0]["reply_markup"] is not None


def test_bills_button_shows_bills_list(tmp_path):
    h, _, bills, *_ = _handlers(tmp_path)
    bills.create_template("Аренда", day_of_month=5, amount=100)
    message = FakeMessage(BTN_BILLS)
    asyncio.run(h.handle_text(FakeUpdate(message), context=None))
    assert "Аренда" in message.calls[0]["text"]


def test_inbox_button_shows_inbox_list(tmp_path):
    h, _, _, inbox, _ = _handlers(tmp_path)
    inbox.create("мысль про отпуск")
    message = FakeMessage(BTN_INBOX)
    asyncio.run(h.handle_text(FakeUpdate(message), context=None))
    assert "мысль про отпуск" in message.calls[0]["text"]


def test_button_labels_never_reach_llm_or_diary(tmp_path):
    """Регрессия: тексты кнопок не должны попадать ни в 📓-режим, ни в LLM-чат —
    _BoomLLM упадёт, если до него дойдёт очередь."""
    h, *_ = _handlers(tmp_path)
    for label in (BTN_TASKS, BTN_BILLS, BTN_INBOX):
        message = FakeMessage(label)
        asyncio.run(h.handle_text(FakeUpdate(message), context=None))
