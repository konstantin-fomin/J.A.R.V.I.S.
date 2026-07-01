"""Тесты конвертации markdown → Telegram HTML и безопасной отправки с fallback.

Баг: бот слал в Telegram сырой markdown от Gemini (**жирный**, списки) без
parse_mode — звёздочки были видны буквально. Сеть/Telegram не дёргаем (как и
в test_voice.py/test_vision.py) — send_fn здесь фейковый async-колбэк.
Асинхронные хендлеры гоняем через asyncio.run, как в test_obligations.py/
test_quiet_hours.py — pytest-asyncio в проекте не подключён."""
import asyncio

from telegram.error import BadRequest

from bot.telegram_format import markdown_to_html, reply_html, send_html, edit_html


# --- markdown_to_html --------------------------------------------------------

def test_bold_double_star_converts_to_b_tag():
    assert markdown_to_html("это **важно** очень") == "это <b>важно</b> очень"


def test_bold_double_underscore_converts_to_b_tag():
    assert markdown_to_html("это __важно__ очень") == "это <b>важно</b> очень"


def test_bullet_dash_converts_to_bullet_char():
    assert markdown_to_html("- пункт один\n- пункт два") == "• пункт один\n• пункт два"


def test_bullet_star_converts_to_bullet_char():
    assert markdown_to_html("* пункт") == "• пункт"


def test_inline_code_wrapped_in_code_tag():
    assert markdown_to_html("запусти `ls -la`") == "запусти <code>ls -la</code>"


def test_fenced_code_block_wrapped_in_pre_tag():
    result = markdown_to_html("```\nprint(1)\n```")
    assert result == "<pre>print(1)</pre>"


def test_link_converts_to_anchor_tag():
    result = markdown_to_html("см. [сайт](https://example.com)")
    assert result == 'см. <a href="https://example.com">сайт</a>'


def test_html_special_chars_escaped_outside_code():
    assert markdown_to_html("5 < 10 && a > b") == "5 &lt; 10 &amp;&amp; a &gt; b"


def test_html_special_chars_escaped_inside_code():
    assert markdown_to_html("`a < b`") == "<code>a &lt; b</code>"


def test_plain_text_with_price_and_parens_is_untouched():
    text = "100.50 руб (примерно)"
    assert markdown_to_html(text) == text


def test_numbered_list_untouched():
    text = "1. первое\n2. второе"
    assert markdown_to_html(text) == text


# --- fakes для reply_html/send_html/edit_html --------------------------------

class FakeMessage:
    def __init__(self, fail_html: bool = False):
        self.fail_html = fail_html
        self.calls: list[dict] = []

    async def reply_text(self, text, parse_mode=None, **kwargs):
        self.calls.append({"text": text, "parse_mode": parse_mode, **kwargs})
        if self.fail_html and parse_mode is not None:
            raise BadRequest("Can't parse entities")


class FakeBot:
    def __init__(self, fail_html: bool = False):
        self.fail_html = fail_html
        self.calls: list[dict] = []

    async def send_message(self, chat_id, text, parse_mode=None, **kwargs):
        self.calls.append({"chat_id": chat_id, "text": text, "parse_mode": parse_mode, **kwargs})
        if self.fail_html and parse_mode is not None:
            raise BadRequest("Can't parse entities")


class FakeQuery:
    def __init__(self, fail_html: bool = False):
        self.fail_html = fail_html
        self.calls: list[dict] = []

    async def edit_message_text(self, text, parse_mode=None, **kwargs):
        self.calls.append({"text": text, "parse_mode": parse_mode, **kwargs})
        if self.fail_html and parse_mode is not None:
            raise BadRequest("Can't parse entities")


# --- reply_html ---------------------------------------------------------------

def test_reply_html_sends_converted_text_with_html_parse_mode():
    message = FakeMessage()
    asyncio.run(reply_html(message, "**важно**"))
    assert message.calls == [{"text": "<b>важно</b>", "parse_mode": "HTML"}]


def test_reply_html_falls_back_to_plain_text_on_telegram_error():
    message = FakeMessage(fail_html=True)
    asyncio.run(reply_html(message, "**важно**"))
    assert len(message.calls) == 2
    assert message.calls[0]["parse_mode"] == "HTML"
    assert message.calls[1] == {"text": "важно", "parse_mode": None}


def test_reply_html_splits_long_text_and_keeps_markup_on_last_part_only():
    message = FakeMessage()
    long_text = "a" * 5000
    asyncio.run(reply_html(message, long_text, reply_markup="kb"))
    assert len(message.calls) == 2
    assert "reply_markup" not in message.calls[0]
    assert message.calls[1]["reply_markup"] == "kb"


# --- send_html ------------------------------------------------------------

def test_send_html_sends_converted_text_with_html_parse_mode():
    bot = FakeBot()
    asyncio.run(send_html(bot, chat_id=42, text="- пункт"))
    assert bot.calls == [{"chat_id": 42, "text": "• пункт", "parse_mode": "HTML"}]


def test_send_html_falls_back_to_plain_text_on_telegram_error():
    bot = FakeBot(fail_html=True)
    asyncio.run(send_html(bot, chat_id=42, text="**важно**"))
    assert len(bot.calls) == 2
    assert bot.calls[1] == {"chat_id": 42, "text": "важно", "parse_mode": None}


# --- edit_html --------------------------------------------------------------

def test_edit_html_sends_converted_text_with_html_parse_mode():
    query = FakeQuery()
    asyncio.run(edit_html(query, "**готово**"))
    assert query.calls == [{"text": "<b>готово</b>", "parse_mode": "HTML"}]


def test_edit_html_falls_back_to_plain_text_on_telegram_error():
    query = FakeQuery(fail_html=True)
    asyncio.run(edit_html(query, "**готово**"))
    assert len(query.calls) == 2
    assert query.calls[1] == {"text": "готово", "parse_mode": None}
