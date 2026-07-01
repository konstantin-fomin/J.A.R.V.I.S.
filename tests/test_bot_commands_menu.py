"""Тесты меню команд Telegram (setMyCommands): BOT_COMMANDS должен один-в-один
совпадать с реально зарегистрированными CommandHandler в build_application —
иначе меню списка команд разъедется с тем, что бот на самом деле умеет.

_set_bot_commands — post_init callback, вызывается один раз при старте бота
(Application.builder().post_init(...)), не через ручной @BotFather. Сеть не
дёргаем: build_application строит Application с фиктивным токеном (это не бьёт
в сеть — сеть трогается только на initialize()/run_polling), а _set_bot_commands
тестируется отдельно с фейковым bot.
"""
import asyncio

from telegram.ext import CommandHandler

import config
from bills import BillStore
from inbox import InboxStore
from tasks import TaskStore
from bot.telegram_bot import BOT_COMMANDS, _set_bot_commands, build_application


def _registered_commands(app) -> set[str]:
    names: set[str] = set()
    for handlers_list in app.handlers.values():
        for h in handlers_list:
            if isinstance(h, CommandHandler):
                names |= h.commands
    return names


class _FakeMemory:
    """build_application заводит ProactiveSuggester(memory.index, ...) — нужен
    только атрибут, find_suggestions в этих тестах не вызывается."""
    index = None


def _build_app(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SUGGESTIONS_DB_PATH", tmp_path / "s.db")
    tasks = TaskStore(tmp_path / "t.db")
    bills = BillStore(tmp_path / "b.db")
    inbox = InboxStore(tmp_path / "i.db")
    return build_application(
        "123:FAKE", memory=_FakeMemory(), llm=None, facts=None, bills=bills, tasks=tasks,
        inbox=inbox,
    )  # type: ignore[arg-type]


def test_bot_commands_match_registered_command_handlers(tmp_path, monkeypatch):
    app = _build_app(tmp_path, monkeypatch)
    registered = _registered_commands(app)
    declared = {cmd for cmd, _ in BOT_COMMANDS}
    assert registered == declared


def test_bot_commands_have_russian_descriptions():
    assert BOT_COMMANDS  # непусто
    for cmd, description in BOT_COMMANDS:
        assert cmd and description
        assert cmd.islower()


def test_build_application_wires_post_init_to_set_bot_commands(tmp_path, monkeypatch):
    app = _build_app(tmp_path, monkeypatch)
    assert app.post_init is _set_bot_commands


# --- _set_bot_commands: сам вызов Bot API -------------------------------------

class FakeBot:
    def __init__(self):
        self.calls: list[list] = []

    async def set_my_commands(self, commands):
        self.calls.append(commands)


class FakeApp:
    def __init__(self, bot):
        self.bot = bot


def test_set_bot_commands_calls_bot_api_with_declared_list():
    bot = FakeBot()
    asyncio.run(_set_bot_commands(FakeApp(bot)))
    assert len(bot.calls) == 1
    sent = bot.calls[0]
    assert [(c.command, c.description) for c in sent] == BOT_COMMANDS
