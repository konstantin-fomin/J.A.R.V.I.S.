"""Тихие часы (§18.3): общий слой над job'ами планировщика.

Не меняет бизнес-логику job'ов — только решает, можно ли *сейчас* слать сообщение
в Telegram, и если нельзя (тихие часы) — откладывает доставку на конец окна, не
теряя сообщение. Чистые функции (is_quiet_now/seconds_until_quiet_end) тестируются
с инъекцией now; quiet_defer работает поверх JobQueue python-telegram-bot.
"""
from __future__ import annotations

import logging
from datetime import datetime, time, timedelta

import config

logger = logging.getLogger(__name__)


def _parse_hhmm(value: str) -> time:
    """«ЧЧ:ММ» → time. Кривое значение — отдаём полночь (тихих часов фактически нет)."""
    try:
        return time.fromisoformat(value.strip())
    except (ValueError, AttributeError):
        return time(0, 0)


def is_quiet_now(now: datetime | None = None,
                 start: str | None = None, end: str | None = None) -> bool:
    """Идут ли сейчас тихие часы. start включительно, end исключительно.

    Корректно обрабатывает переход через полночь (start > end, напр. 23:00→09:00).
    Окно нулевой длины (start == end) трактуем как «тихих часов нет»."""
    now = now or datetime.now()
    t = now.time()
    s = _parse_hhmm(start if start is not None else config.QUIET_HOURS_START)
    e = _parse_hhmm(end if end is not None else config.QUIET_HOURS_END)
    if s == e:
        return False
    if s < e:                      # обычное окно внутри суток
        return s <= t < e
    return t >= s or t < e          # окно через полночь


def seconds_until_quiet_end(now: datetime | None = None, end: str | None = None) -> int:
    """Сколько секунд до ближайшего конца тихих часов (момент QUIET_HOURS_END)."""
    now = now or datetime.now()
    e = _parse_hhmm(end if end is not None else config.QUIET_HOURS_END)
    target = datetime.combine(now.date(), e)
    if target <= now:               # конец окна сегодня уже прошёл — значит завтра
        target += timedelta(days=1)
    return int((target - now).total_seconds())


def quiet_defer(context, job_callback) -> bool:
    """Если сейчас тихие часы — перепланирует job_callback на конец окна через
    JobQueue.run_once и возвращает True (текущий вызов должен прекратиться, ничего
    не отправив). Иначе False — job работает как обычно.

    Без JobQueue (нет APScheduler) деферить некуда — возвращаем False, чтобы не
    проглотить сообщение молча."""
    if not is_quiet_now():
        return False
    jq = getattr(context, "job_queue", None)
    if jq is None:
        logger.warning("Тихие часы, но JobQueue недоступен — шлю без отсрочки")
        return False
    delay = seconds_until_quiet_end()
    name = getattr(context.job, "name", "job")
    jq.run_once(job_callback, when=delay, data=context.job.data, name=f"{name}_deferred")
    logger.info("Тихие часы: отложил «%s» на %d сек (до конца окна)", name, delay)
    return True
