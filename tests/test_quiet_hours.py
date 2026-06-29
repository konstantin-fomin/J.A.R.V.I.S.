"""Тесты тихих часов (§18.3): чистая функция is_quiet_now() на границах окна и
живой сценарий — job remind_bills откладывает отправку в тихие часы и доставляет
сообщение после открытия окна.

is_quiet_now тестируется юнитом (явные start/end, инъекция now). Деферинг —
интеграционно через настоящий remind_bills с фейковыми bot/job_queue: сеть и
Telegram не дёргаем, время управляем через окно тихих часов в config.
"""
import asyncio
from datetime import datetime, timedelta

import pytest

import config
from bills import BillStore
from scheduler_utils import is_quiet_now, seconds_until_quiet_end


# --- is_quiet_now: границы окна ---------------------------------------------

def _at(hh, mm=0):
    return datetime(2026, 6, 29, hh, mm)


@pytest.mark.parametrize(
    "now, expected",
    [
        (_at(23, 0), True),    # ровно начало — тихо (включительно)
        (_at(8, 59), True),    # за минуту до конца — ещё тихо
        (_at(9, 0), False),    # ровно конец — уже не тихо (исключительно)
        (_at(0, 0), True),     # полночь внутри окна
        (_at(2, 30), True),    # глубокая ночь
        (_at(12, 0), False),   # день — не тихо
        (_at(22, 59), False),  # за минуту до начала — ещё не тихо
    ],
)
def test_is_quiet_now_wraparound_boundaries(now, expected):
    assert is_quiet_now(now, start="23:00", end="09:00") is expected


def test_is_quiet_now_non_wrapping_window():
    # окно внутри суток (не через полночь): 01:00–06:00
    assert is_quiet_now(_at(0, 30), start="01:00", end="06:00") is False
    assert is_quiet_now(_at(1, 0), start="01:00", end="06:00") is True
    assert is_quiet_now(_at(5, 59), start="01:00", end="06:00") is True
    assert is_quiet_now(_at(6, 0), start="01:00", end="06:00") is False


def test_is_quiet_now_zero_length_window_is_never_quiet():
    assert is_quiet_now(_at(9, 0), start="09:00", end="09:00") is False
    assert is_quiet_now(_at(23, 0), start="09:00", end="09:00") is False


def test_seconds_until_quiet_end_same_day():
    # сейчас 23:30, конец окна 09:00 → до конца ~9.5 часов (на следующий день)
    secs = seconds_until_quiet_end(_at(23, 30), end="09:00")
    assert secs == int(timedelta(hours=9, minutes=30).total_seconds())


def test_seconds_until_quiet_end_later_today():
    # сейчас 02:00, конец окна 09:00 → 7 часов (в эти же сутки)
    secs = seconds_until_quiet_end(_at(2, 0), end="09:00")
    assert secs == int(timedelta(hours=7).total_seconds())


# --- живой сценарий: remind_bills откладывает и доставляет -------------------

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
        self.scheduled = []  # список (callback, when, data, name)

    def run_once(self, callback, when, data=None, name=None):
        self.scheduled.append({"callback": callback, "when": when, "data": data, "name": name})


class FakeContext:
    def __init__(self, data, name, bot, job_queue):
        self.job = FakeJob(data, name)
        self.bot = bot
        self.job_queue = job_queue


def _quiet_window_around_now(monkeypatch):
    """Выставляет окно тихих часов так, чтобы СЕЙЧАС было тихо (центрируем на now)."""
    now = datetime.now()
    monkeypatch.setattr(config, "QUIET_HOURS_START", (now - timedelta(hours=1)).strftime("%H:%M"))
    monkeypatch.setattr(config, "QUIET_HOURS_END", (now + timedelta(hours=1)).strftime("%H:%M"))


def _open_window_now(monkeypatch):
    """Выставляет окно тихих часов в будущем — значит СЕЙЧАС уже не тихо."""
    now = datetime.now()
    monkeypatch.setattr(config, "QUIET_HOURS_START", (now + timedelta(hours=2)).strftime("%H:%M"))
    monkeypatch.setattr(config, "QUIET_HOURS_END", (now + timedelta(hours=3)).strftime("%H:%M"))


def test_remind_bills_defers_in_quiet_hours_and_delivers_after(tmp_path, monkeypatch):
    from bot import telegram_bot

    # один неоплаченный платёж на завтра
    bills = BillStore(tmp_path / "bills.db")
    tomorrow = (datetime.now().date() + timedelta(days=1))
    bills.create_template("аренда", day_of_month=tomorrow.day, amount=100)
    bills.ensure_month(tomorrow.strftime("%Y-%m"))

    monkeypatch.setattr(config, "ALLOWED_USER_ID", 42)
    bot = FakeBot()
    jq = FakeJobQueue()
    ctx = FakeContext(data=bills, name="remind_bills", bot=bot, job_queue=jq)

    # 1) тихие часы → ничего не отправлено, но job переставлен на конец окна
    _quiet_window_around_now(monkeypatch)
    asyncio.run(telegram_bot.remind_bills(ctx))
    assert bot.sent == []                 # в тихие часы молчим
    assert len(jq.scheduled) == 1         # отложили доставку
    deferred = jq.scheduled[0]
    assert deferred["callback"] is telegram_bot.remind_bills

    # 2) окно открылось → отложенный запуск доставляет сообщение ровно один раз
    _open_window_now(monkeypatch)
    ctx2 = FakeContext(data=deferred["data"], name="remind_bills_deferred", bot=bot, job_queue=jq)
    asyncio.run(deferred["callback"](ctx2))
    assert len(bot.sent) == 1             # доставлено после открытия окна
    assert bot.sent[0]["chat_id"] == 42
