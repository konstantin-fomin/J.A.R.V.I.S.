"""Единая точка отправки сообщений в Telegram: markdown → HTML + safe fallback.

Все свободные тексты (ответы LLM: чат, /photo, /voice, /plan, weekly review,
decisions, намерения) должны идти через reply_html/send_html/edit_html вместо
голого reply_text/send_message/edit_message_text — иначе **markdown** от
Gemini уходит в Telegram как сырые звёздочки (Telegram не форматирует текст
без parse_mode). MarkdownV2 напрямую на сыром LLM-тексте небезопасен (падает
на неэкранированных спецсимволах вроде "." и "-") — конвертируем в HTML и
экранируем сами, а если Telegram всё равно отклонит разметку — шлём как
обычный текст, не теряя сообщение.
"""
import html as html_lib
import logging
import re

from telegram.error import TelegramError

logger = logging.getLogger(__name__)

TELEGRAM_MAX_LEN = 4096

ParseModeHTML = "HTML"


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def markdown_to_html(text: str) -> str:
    """Грубая конвертация markdown из LLM-ответов в Telegram-совместимый HTML.

    Поддерживает: ```блоки кода```, `инлайн-код`, **жирный**/__жирный__,
    заголовки "# ...", маркеры списков "- "/"* " → "• ", [текст](url) →
    ссылку. Всё остальное экранируется как обычный текст — Telegram покажет
    его буквально, но не упадёт с 400 Bad Request."""
    blocks: list[str] = []

    def _stash_block(m: re.Match) -> str:
        blocks.append(_escape(m.group(1).strip("\n")))
        return f"\x00B{len(blocks) - 1}\x00"

    text = re.sub(r"```(?:\w+\n)?(.*?)```", _stash_block, text, flags=re.DOTALL)

    inline: list[str] = []

    def _stash_inline(m: re.Match) -> str:
        inline.append(_escape(m.group(1)))
        return f"\x00I{len(inline) - 1}\x00"

    text = re.sub(r"`([^`\n]+)`", _stash_inline, text)

    text = _escape(text)
    text = re.sub(r"^#{1,6}\s+(.+)$", r"<b>\1</b>", text, flags=re.MULTILINE)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text, flags=re.DOTALL)
    text = re.sub(r"__(.+?)__", r"<b>\1</b>", text, flags=re.DOTALL)
    text = re.sub(r"^(\s*)[-*]\s+", r"\1• ", text, flags=re.MULTILINE)
    text = re.sub(r"\[([^\]]+)\]\((https?://[^\s)]+)\)", r'<a href="\2">\1</a>', text)

    for i, block in enumerate(blocks):
        text = text.replace(f"\x00B{i}\x00", f"<pre>{block}</pre>")
    for i, code in enumerate(inline):
        text = text.replace(f"\x00I{i}\x00", f"<code>{code}</code>")

    return text


def _split(text: str) -> list[str]:
    """Telegram не принимает сообщения длиннее 4096 символов."""
    if len(text) <= TELEGRAM_MAX_LEN:
        return [text]
    parts = []
    while text:
        parts.append(text[:TELEGRAM_MAX_LEN])
        text = text[TELEGRAM_MAX_LEN:]
    return parts


def _strip_tags(html_text: str) -> str:
    """Fallback-текст для случая, когда HTML-разметка не прошла у Telegram."""
    return html_lib.unescape(re.sub(r"<[^>]+>", "", html_text))


async def _send_one(send_fn, part: str, **kwargs) -> None:
    try:
        await send_fn(text=part, parse_mode=ParseModeHTML, **kwargs)
    except TelegramError:
        logger.warning("HTML-отправка в Telegram упала, шлю без форматирования", exc_info=True)
        await send_fn(text=_strip_tags(part), parse_mode=None, **kwargs)


async def reply_html(message, text: str, **kwargs) -> None:
    """Замена message.reply_text для текста, который может содержать markdown."""
    parts = _split(markdown_to_html(text))
    last = len(parts) - 1
    for i, part in enumerate(parts):
        part_kwargs = dict(kwargs) if i == last else {k: v for k, v in kwargs.items() if k != "reply_markup"}
        await _send_one(message.reply_text, part, **part_kwargs)


async def send_html(bot, chat_id, text: str, **kwargs) -> None:
    """Замена bot.send_message для текста, который может содержать markdown."""
    async def _send(text: str, **kw) -> None:
        await bot.send_message(chat_id=chat_id, text=text, **kw)

    parts = _split(markdown_to_html(text))
    last = len(parts) - 1
    for i, part in enumerate(parts):
        part_kwargs = dict(kwargs) if i == last else {k: v for k, v in kwargs.items() if k != "reply_markup"}
        await _send_one(_send, part, **part_kwargs)


async def edit_html(query, text: str, **kwargs) -> None:
    """Замена query.edit_message_text — без сплита, edit — всегда одно сообщение."""
    html_text = markdown_to_html(text)[:TELEGRAM_MAX_LEN]
    await _send_one(query.edit_message_text, html_text, **kwargs)
