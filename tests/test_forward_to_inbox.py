"""Тесты форварда в инбокс (эргономика бота, п.5): пересланное текстовое
сообщение (update.message.forward_origin не None) уходит в инбокс напрямую,
с source="forward", минуя intent-парсинг (LLM не дёргаем) — сам факт форварда
уже однозначный сигнал «сохрани на потом». Путь идёт через IntentRouter.execute
(capture уже в _LOGGED), значит журналируется и отменяемо undo_last, как обычный
capture из свободного текста."""
import asyncio

import pytest

import config
from bills import BillStore
from bot.handlers import Handlers
from inbox import InboxStore
from logger import ActionLog
from tasks import TaskStore


@pytest.fixture(autouse=True)
def _no_user_restriction(monkeypatch):
    monkeypatch.setattr(config, "ALLOWED_USER_ID", None)


class _BoomLLM:
    def chat(self, *args, **kwargs):
        raise AssertionError("LLM не должен вызываться для форварда")


class FakeMessage:
    def __init__(self, text="", forwarded=False):
        self.text = text
        self.forward_origin = object() if forwarded else None
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
    return h, inbox, alog


def test_forwarded_message_creates_inbox_item_with_source_forward(tmp_path):
    h, inbox, _ = _handlers(tmp_path)
    message = FakeMessage("интересная статья про роботов", forwarded=True)
    asyncio.run(h.handle_text(FakeUpdate(message), context=None))

    items = inbox.list()
    assert len(items) == 1
    assert items[0]["text"] == "интересная статья про роботов"
    assert items[0]["source"] == "forward"
    assert "инбокс" in message.calls[0]["text"].lower()


def test_forwarded_message_is_logged_and_undoable(tmp_path):
    h, inbox, alog = _handlers(tmp_path)
    message = FakeMessage("прочитать на выходных", forwarded=True)
    asyncio.run(h.handle_text(FakeUpdate(message), context=None))

    rec = alog.latest_active()
    assert rec["entity_type"] == "inbox" and rec["action"] == "create"

    item_id = inbox.list()[0]["id"]
    undo = h.router.resolve({"intent": "undo_last"})
    h.router.execute(undo.action)
    assert inbox.get(item_id) is None


def test_non_forwarded_text_does_not_use_forward_path(tmp_path, monkeypatch):
    """Обычный текст (без форварда) не должен попадать в _capture_forward —
    должен уйти в обычный _process_text (intent-парсинг/чат), который здесь
    подменён заглушкой, чтобы не тянуть LLM/память."""
    h, inbox, _ = _handlers(tmp_path)
    called_process_text = False

    async def fake_process_text(update, context, text):
        nonlocal called_process_text
        called_process_text = True

    monkeypatch.setattr(h, "_process_text", fake_process_text)
    message = FakeMessage("просто сообщение без форварда", forwarded=False)
    asyncio.run(h.handle_text(FakeUpdate(message), context=None))

    assert called_process_text
    assert inbox.list() == []
