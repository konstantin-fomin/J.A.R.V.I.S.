"""Тесты правила «никогда не обрезаем текст записи многоточием» — единая логика
для трёх мест (инбокс/задачи/платежи), см. render_actionable_list:

- Если полная подпись кнопки умещается в лимит Telegram (BUTTON_TEXT_LIMIT,
  1-64 символа согласно Bot API) — запись показываем ТОЛЬКО кнопкой с полным
  текстом в общем списке, без дублирования отдельной строкой.
- Если не умещается — запись целиком уходит отдельным Telegram-сообщением
  (long_messages): полный текст без обрезки как текст сообщения, кнопка-
  действие сразу под ним. Кнопка физически привязана к записи через
  принадлежность одному сообщению, а не через порядок/соседство в списке.

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
    lines, markup, long_messages = _render(items)
    assert lines == []
    assert long_messages == []
    labels = [b.text for row in markup.inline_keyboard for b in row]
    assert labels == ["» купить молоко"]


def test_long_label_becomes_standalone_message():
    long_text = "x" * 100
    items = [_item(1, long_text)]
    lines, markup, long_messages = _render(items)
    # длинная запись не попадает в общий список вообще — ни текстом, ни кнопкой
    assert lines == []
    assert markup is None
    assert len(long_messages) == 1
    msg_text, msg_markup = long_messages[0]
    assert msg_text == f"• {long_text}"
    labels = [b.text for row in msg_markup.inline_keyboard for b in row]
    assert labels == ["→ действие"]
    assert long_text not in labels[0]  # компактная кнопка не повторяет текст записи


def test_no_ellipsis_anywhere_for_long_text():
    long_text = "y" * 200
    items = [_item(1, long_text)]
    _, _, long_messages = _render(items)
    msg_text, _ = long_messages[0]
    assert "…" not in msg_text
    assert msg_text == f"• {long_text}"


def test_non_actionable_item_always_shown_as_text_no_button():
    items = [_item(1, "готово", actionable=False)]
    lines, markup, long_messages = _render(items)
    assert lines == ["• готово"]
    assert markup is None
    assert long_messages == []


def test_boundary_exact_limit_becomes_button():
    """Ровно BUTTON_TEXT_LIMIT символов — ещё влезает, идёт кнопкой в общем списке."""
    text = "z" * (BUTTON_TEXT_LIMIT - len("» "))
    items = [_item(1, text)]
    lines, markup, long_messages = _render(items)
    assert lines == []
    assert markup is not None
    assert long_messages == []


def test_boundary_one_over_limit_becomes_standalone_message():
    text = "z" * (BUTTON_TEXT_LIMIT - len("» ") + 1)
    items = [_item(1, text)]
    lines, markup, long_messages = _render(items)
    assert lines == []
    assert markup is None
    assert long_messages == [(f"• {text}", long_messages[0][1])]


def test_only_short_items_single_list_no_standalone_messages():
    """Список из одних коротких записей — как раньше, без изменений в поведении."""
    items = [_item(1, "молоко"), _item(2, "хлеб"), _item(3, "сыр")]
    lines, markup, long_messages = _render(items)
    assert lines == []
    labels = [b.text for row in markup.inline_keyboard for b in row]
    assert labels == ["» молоко", "» хлеб", "» сыр"]
    assert long_messages == []


def test_mixed_short_and_one_long_produces_one_standalone_message():
    long_text = "l" * 100
    items = [_item(1, "молоко"), _item(2, long_text), _item(3, "хлеб")]
    lines, markup, long_messages = _render(items)
    assert lines == []
    labels = [b.text for row in markup.inline_keyboard for b in row]
    assert labels == ["» молоко", "» хлеб"]  # длинная запись сюда не попала
    assert len(long_messages) == 1
    msg_text, msg_markup = long_messages[0]
    assert msg_text == f"• {long_text}"
    assert msg_markup.inline_keyboard[0][0].callback_data == "x:2"


def test_mixed_short_and_multiple_long_do_not_mix_up():
    long_a = "a" * 100
    long_b = "b" * 100
    items = [_item(1, "молоко"), _item(2, long_a), _item(3, long_b)]
    lines, markup, long_messages = _render(items)
    labels = [b.text for row in markup.inline_keyboard for b in row]
    assert labels == ["» молоко"]
    assert len(long_messages) == 2
    # каждая длинная запись — своя пара (текст, кнопка со своим callback_data),
    # без путаницы порядка/номера
    text_a, markup_a = long_messages[0]
    text_b, markup_b = long_messages[1]
    assert text_a == f"• {long_a}"
    assert markup_a.inline_keyboard[0][0].callback_data == "x:2"
    assert text_b == f"• {long_b}"
    assert markup_b.inline_keyboard[0][0].callback_data == "x:3"


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
LONG_TITLE_2 = "Второе очень длинное название задачи, которое тоже совершенно точно не влезет в лимит кнопки Telegram"


def test_query_tasks_short_title_is_button_only(tmp_path):
    h, tasks, *_ = _handlers(tmp_path)
    tasks.create("купить молоко")
    message = FakeMessage()
    asyncio.run(h._query_tasks(FakeUpdate(message), {"filter": None}))
    assert len(message.calls) == 1  # только основной список, без доп. сообщений
    call = message.calls[0]
    assert "купить молоко" not in call["text"]  # не задублировано текстом
    labels = [b.text for row in call["reply_markup"].inline_keyboard for b in row]
    assert labels == ["☑️ купить молоко"]


def test_query_tasks_long_title_sent_as_separate_message(tmp_path):
    h, tasks, *_ = _handlers(tmp_path)
    tasks.create(LONG_TITLE)
    message = FakeMessage()
    asyncio.run(h._query_tasks(FakeUpdate(message), {"filter": None}))
    assert len(message.calls) == 2  # список (пустой) + отдельное сообщение
    list_call, long_call = message.calls
    assert LONG_TITLE not in list_call["text"]
    assert list_call.get("reply_markup") is None  # в списке кнопок для длинной нет

    assert LONG_TITLE in long_call["text"]
    assert "…" not in long_call["text"]
    labels = [b.text for row in long_call["reply_markup"].inline_keyboard for b in row]
    assert labels == ["✅ отметить"]
    assert LONG_TITLE not in labels[0]


def test_query_tasks_mixed_short_and_long_sends_list_then_separate_message(tmp_path):
    h, tasks, *_ = _handlers(tmp_path)
    tasks.create("купить молоко")
    tasks.create(LONG_TITLE)
    message = FakeMessage()
    asyncio.run(h._query_tasks(FakeUpdate(message), {"filter": None}))
    assert len(message.calls) == 2
    list_call, long_call = message.calls
    assert LONG_TITLE not in list_call["text"]
    labels = [b.text for row in list_call["reply_markup"].inline_keyboard for b in row]
    assert labels == ["☑️ купить молоко"]
    assert LONG_TITLE in long_call["text"]


def test_query_tasks_multiple_long_titles_do_not_mix_up(tmp_path):
    h, tasks, *_ = _handlers(tmp_path)
    t1 = tasks.create(LONG_TITLE)
    t2 = tasks.create(LONG_TITLE_2)
    message = FakeMessage()
    asyncio.run(h._query_tasks(FakeUpdate(message), {"filter": None}))
    assert len(message.calls) == 3  # список (пустой) + 2 отдельных сообщения
    list_call, call_1, call_2 = message.calls
    assert list_call.get("reply_markup") is None

    assert LONG_TITLE in call_1["text"]
    assert LONG_TITLE_2 not in call_1["text"]
    assert call_1["reply_markup"].inline_keyboard[0][0].callback_data == f"task_done:{t1['id']}"

    assert LONG_TITLE_2 in call_2["text"]
    assert LONG_TITLE not in call_2["text"]
    assert call_2["reply_markup"].inline_keyboard[0][0].callback_data == f"task_done:{t2['id']}"


def test_query_bills_short_name_is_button_only(tmp_path):
    h, _, bills, *_ = _handlers(tmp_path)
    bills.create_template("Аренда", day_of_month=5, amount=100)
    message = FakeMessage()
    asyncio.run(h._query_bills(FakeUpdate(message)))
    assert len(message.calls) == 1
    call = message.calls[0]
    assert "Аренда" not in call["text"]
    labels = [b.text for row in call["reply_markup"].inline_keyboard for b in row]
    assert any("Аренда" in label for label in labels)


def test_query_bills_long_name_sent_as_separate_message(tmp_path):
    h, _, bills, *_ = _handlers(tmp_path)
    long_name = "Очень длинное название платежа, которое совершенно точно не влезет в кнопку Telegram"
    bills.create_template(long_name, day_of_month=5, amount=100)
    message = FakeMessage()
    asyncio.run(h._query_bills(FakeUpdate(message)))
    assert len(message.calls) == 2
    list_call, long_call = message.calls
    assert long_name not in list_call["text"]
    assert long_name in long_call["text"]
    assert "…" not in long_call["text"]
    labels = [b.text for row in long_call["reply_markup"].inline_keyboard for b in row]
    assert labels == ["✅ отметить"]


def test_inbox_cmd_short_text_is_button_only(tmp_path):
    h, _, _, inbox, _ = _handlers(tmp_path)
    inbox.create("мысль про отпуск")
    message = FakeMessage()
    asyncio.run(h.inbox_cmd(FakeUpdate(message), context=None))
    assert len(message.calls) == 1
    call = message.calls[0]
    assert "• мысль про отпуск" not in call["text"]  # не задублировано отдельной строкой
    labels = [b.text for row in call["reply_markup"].inline_keyboard for b in row]
    assert any("мысль про отпуск" in label for label in labels)


def test_inbox_cmd_short_text_no_verbose_action_phrase(tmp_path):
    # Подпись кнопки — компактный префикс + текст, как у задач («☑️ title») и
    # платежей («✅ Оплачено · name»), а не проговорённая целиком фраза действия
    # («→ в задачу: ...») — иначе короткая запись (частый случай) выглядит как
    # старый многословный формат, который и должна была устранить render_actionable_list.
    h, _, _, inbox, _ = _handlers(tmp_path)
    inbox.create("мысль про отпуск")
    message = FakeMessage()
    asyncio.run(h.inbox_cmd(FakeUpdate(message), context=None))
    call = message.calls[0]
    labels = [b.text for row in call["reply_markup"].inline_keyboard for b in row]
    assert labels == ["→ мысль про отпуск"]
    assert "в задачу" not in labels[0]


def test_inbox_cmd_long_text_sent_as_separate_message(tmp_path):
    h, _, _, inbox, _ = _handlers(tmp_path)
    long_text = "Очень длинная мысль про отпуск, которая совершенно точно не влезет в кнопку Telegram целиком"
    inbox.create(long_text)
    message = FakeMessage()
    asyncio.run(h.inbox_cmd(FakeUpdate(message), context=None))
    assert len(message.calls) == 2
    list_call, long_call = message.calls
    assert long_text not in list_call["text"]
    assert long_text in long_call["text"]
    assert "…" not in long_call["text"]
    labels = [b.text for row in long_call["reply_markup"].inline_keyboard for b in row]
    assert labels == ["→ задача"]


def test_inbox_cmd_multiple_long_texts_do_not_mix_up(tmp_path):
    h, _, _, inbox, _ = _handlers(tmp_path)
    long_a = "Первая очень длинная мысль про отпуск, которая совершенно точно не влезет в кнопку Telegram"
    long_b = "Вторая очень длинная мысль про ремонт, которая тоже совершенно точно не влезет в кнопку Telegram"
    item_a = inbox.create(long_a)
    item_b = inbox.create(long_b)
    message = FakeMessage()
    asyncio.run(h.inbox_cmd(FakeUpdate(message), context=None))
    assert len(message.calls) == 3  # список (заголовок группы, пустой) + 2 сообщения
    list_call, call_1, call_2 = message.calls
    assert long_a not in list_call["text"]
    assert long_b not in list_call["text"]

    assert long_a in call_1["text"]
    assert long_b not in call_1["text"]
    assert call_1["reply_markup"].inline_keyboard[0][0].callback_data == f"inbox2task:{item_a['id']}"

    assert long_b in call_2["text"]
    assert long_a not in call_2["text"]
    assert call_2["reply_markup"].inline_keyboard[0][0].callback_data == f"inbox2task:{item_b['id']}"
