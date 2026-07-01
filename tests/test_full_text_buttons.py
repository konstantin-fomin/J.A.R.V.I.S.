"""Тесты правила «никогда не обрезаем текст записи многоточием» — единая логика
для трёх мест (инбокс/задачи/платежи), см. render_actionable_list:

- Если полная подпись кнопки умещается в лимит Telegram (BUTTON_TEXT_LIMIT,
  1-64 символа согласно Bot API) — запись показываем ТОЛЬКО кнопкой с полным
  текстом, без дублирования отдельной строкой.
- Если не умещается — полный текст идёт отдельной строкой (без обрезки), а
  рядом — компактная кнопка-действие без повтора содержимого.

Сеть/LLM не дёргаем.
"""
import asyncio

import pytest

import config
from bills import BillStore
from bot.handlers import (
    BUTTON_TEXT_LIMIT,
    Handlers,
    render_actionable_list,
)
from inbox import InboxStore
from logger import ActionLog
from tasks import TaskStore


@pytest.fixture(autouse=True)
def _no_user_restriction(monkeypatch):
    monkeypatch.setattr(config, "ALLOWED_USER_ID", None)


# --- render_actionable_list: чистая функция -------------------------------------

def _item(id_, text, actionable=True):
    return {"id": id_, "text": text, "actionable": actionable}


def _render(items):
    return render_actionable_list(
        items,
        is_actionable=lambda it: it["actionable"],
        button_label=lambda it: f"» {it['text']}",
        text_line=lambda it: f"• {it['text']}",
        short_action="→ действие",
        callback_data=lambda it: f"x:{it['id']}",
    )


def test_short_label_becomes_button_only_no_text_duplication():
    items = [_item(1, "купить молоко")]
    lines, markup = _render(items)
    assert lines == []
    labels = [b.text for row in markup.inline_keyboard for b in row]
    assert labels == ["» купить молоко"]


def test_long_label_keeps_full_text_as_line_plus_short_button():
    long_text = "x" * 100
    items = [_item(1, long_text)]
    lines, markup = _render(items)
    assert lines == [f"• {long_text}"]
    assert long_text in lines[0]
    labels = [b.text for row in markup.inline_keyboard for b in row]
    assert labels == ["→ действие"]
    # компактная кнопка не должна содержать длинный текст записи
    assert long_text not in labels[0]


def test_no_ellipsis_anywhere_for_long_text():
    long_text = "y" * 200
    items = [_item(1, long_text)]
    lines, markup = _render(items)
    assert "…" not in lines[0]
    assert lines[0] == f"• {long_text}"


def test_non_actionable_item_always_shown_as_text_no_button():
    items = [_item(1, "готово", actionable=False)]
    lines, markup = _render(items)
    assert lines == ["• готово"]
    assert markup is None


def test_boundary_exact_limit_becomes_button():
    """Ровно BUTTON_TEXT_LIMIT символов — ещё влезает, идёт кнопкой."""
    text = "z" * (BUTTON_TEXT_LIMIT - len("» "))
    items = [_item(1, text)]
    lines, markup = _render(items)
    assert lines == []
    assert markup is not None


def test_boundary_one_over_limit_falls_back_to_text():
    text = "z" * (BUTTON_TEXT_LIMIT - len("» ") + 1)
    items = [_item(1, text)]
    lines, markup = _render(items)
    assert lines == [f"• {text}"]


# --- Handlers: интеграция для задач/платежей/инбокса ----------------------------

class FakeMessage:
    def __init__(self):
        self.calls: list[dict] = []

    async def reply_text(self, text, parse_mode=None, **kwargs):
        self.calls.append({"text": text, "parse_mode": parse_mode, **kwargs})


class FakeUpdate:
    def __init__(self, message):
        self.message = message
        self.effective_user = None


def _handlers(tmp_path):
    tasks = TaskStore(tmp_path / "t.db")
    bills = BillStore(tmp_path / "b.db")
    inbox = InboxStore(tmp_path / "i.db")
    alog = ActionLog(tmp_path / "a.db")
    h = Handlers(memory=None, llm=None, facts=None, bills=bills, tasks=tasks,
                calendar=None, action_log=alog, inbox=inbox)  # type: ignore[arg-type]
    return h, tasks, bills, inbox, alog


LONG_TITLE = "Очень длинное название задачи, которое совершенно точно не влезет в лимит кнопки Telegram в 64 символа"


def test_query_tasks_short_title_is_button_only(tmp_path):
    h, tasks, *_ = _handlers(tmp_path)
    tasks.create("купить молоко")
    message = FakeMessage()
    asyncio.run(h._query_tasks(FakeUpdate(message), {"filter": None}))
    call = message.calls[0]
    assert "купить молоко" not in call["text"]  # не задублировано текстом
    labels = [b.text for row in call["reply_markup"].inline_keyboard for b in row]
    assert labels == ["☑️ купить молоко"]


def test_query_tasks_long_title_shown_full_with_short_button(tmp_path):
    h, tasks, *_ = _handlers(tmp_path)
    tasks.create(LONG_TITLE)
    message = FakeMessage()
    asyncio.run(h._query_tasks(FakeUpdate(message), {"filter": None}))
    call = message.calls[0]
    assert LONG_TITLE in call["text"]
    assert "…" not in call["text"]
    labels = [b.text for row in call["reply_markup"].inline_keyboard for b in row]
    assert labels == ["✅ отметить"]
    assert LONG_TITLE not in labels[0]


def test_query_bills_short_name_is_button_only(tmp_path):
    h, _, bills, *_ = _handlers(tmp_path)
    bills.create_template("Аренда", day_of_month=5, amount=100)
    message = FakeMessage()
    asyncio.run(h._query_bills(FakeUpdate(message)))
    call = message.calls[0]
    assert "Аренда" not in call["text"]
    labels = [b.text for row in call["reply_markup"].inline_keyboard for b in row]
    assert any("Аренда" in label for label in labels)


def test_query_bills_long_name_shown_full_with_short_button(tmp_path):
    h, _, bills, *_ = _handlers(tmp_path)
    long_name = "Очень длинное название платежа, которое совершенно точно не влезет в кнопку Telegram"
    bills.create_template(long_name, day_of_month=5, amount=100)
    message = FakeMessage()
    asyncio.run(h._query_bills(FakeUpdate(message)))
    call = message.calls[0]
    assert long_name in call["text"]
    assert "…" not in call["text"]
    labels = [b.text for row in call["reply_markup"].inline_keyboard for b in row]
    assert labels == ["✅ отметить"]


def test_inbox_cmd_short_text_is_button_only(tmp_path):
    h, _, _, inbox, _ = _handlers(tmp_path)
    inbox.create("мысль про отпуск")
    message = FakeMessage()
    asyncio.run(h.inbox_cmd(FakeUpdate(message), context=None))
    call = message.calls[0]
    assert "• мысль про отпуск" not in call["text"]  # не задублировано отдельной строкой
    labels = [b.text for row in call["reply_markup"].inline_keyboard for b in row]
    assert any("мысль про отпуск" in label for label in labels)


def test_inbox_cmd_long_text_shown_full_with_short_button(tmp_path):
    h, _, _, inbox, _ = _handlers(tmp_path)
    long_text = "Очень длинная мысль про отпуск, которая совершенно точно не влезет в кнопку Telegram целиком"
    inbox.create(long_text)
    message = FakeMessage()
    asyncio.run(h.inbox_cmd(FakeUpdate(message), context=None))
    call = message.calls[0]
    assert long_text in call["text"]
    assert "…" not in call["text"]
    labels = [b.text for row in call["reply_markup"].inline_keyboard for b in row]
    assert labels == ["→ задача"]
