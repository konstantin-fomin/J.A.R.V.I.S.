"""Тесты календаря без живого token.json: чистая логика конфликтов и intent-роутинг.

Google API не дёргаем — CalendarClient тестируем с подменённым list_events,
а IntentRouter — с FakeCalendar. Живой OAuth/сеть здесь не нужны.
"""
from datetime import datetime
from zoneinfo import ZoneInfo

from calendar_client import CalendarClient, events_to_remind, select_conflicts
from intents import IntentRouter

TZ = ZoneInfo("Europe/Moscow")


def dt(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=TZ)


def event(eid, title, start, end):
    return {"id": eid, "title": title, "start": start, "end": end}


# --- чистая функция пересечений --------------------------------------------

def test_select_conflicts_returns_overlapping_event():
    events = [event("a", "Созвон", dt(2026, 6, 28, 10), dt(2026, 6, 28, 11))]
    conflicts = select_conflicts(events, dt(2026, 6, 28, 10, 30), dt(2026, 6, 28, 11, 30))
    assert [e["id"] for e in conflicts] == ["a"]


def test_select_conflicts_ignores_adjacent_event():
    # встреча кончается ровно когда начинается новая — это НЕ пересечение
    events = [event("a", "Созвон", dt(2026, 6, 28, 9), dt(2026, 6, 28, 10))]
    conflicts = select_conflicts(events, dt(2026, 6, 28, 10), dt(2026, 6, 28, 11))
    assert conflicts == []


def test_select_conflicts_excludes_ignored_id():
    # при переносе встречи саму себя в конфликты не считаем
    events = [event("a", "Созвон", dt(2026, 6, 28, 10), dt(2026, 6, 28, 11))]
    conflicts = select_conflicts(
        events, dt(2026, 6, 28, 10, 30), dt(2026, 6, 28, 11, 30), ignore_id="a"
    )
    assert conflicts == []


# --- CalendarClient.find_conflicts опирается на list_events -----------------

def test_find_conflicts_uses_list_events():
    client = CalendarClient("credentials.json", "token.json", "Europe/Moscow")
    near = event("a", "Созвон", dt(2026, 6, 28, 10), dt(2026, 6, 28, 11))
    far = event("b", "Обед", dt(2026, 6, 28, 14), dt(2026, 6, 28, 15))
    client.list_events = lambda start, end: [near, far]  # подменяем сеть
    conflicts = client.find_conflicts(dt(2026, 6, 28, 10, 30), dt(2026, 6, 28, 11, 30))
    assert [e["id"] for e in conflicts] == ["a"]


# --- FakeCalendar для роутинга ---------------------------------------------

class FakeCalendar:
    timezone = "Europe/Moscow"

    def __init__(self, events=None):
        self.events = events or []
        self.created = []
        self.updated = []
        self.deleted = []

    def list_events(self, start, end):
        return list(self.events)

    def find_conflicts(self, start, end, ignore_event_id=None):
        return select_conflicts(self.events, start, end, ignore_event_id)

    def create_event(self, title, start, end):
        ev = event("new", title, start, end)
        self.created.append(ev)
        return ev

    def update_event(self, event_id, **fields):
        self.updated.append((event_id, fields))
        return {"id": event_id, **fields}

    def delete_event(self, event_id):
        self.deleted.append(event_id)


def router(calendar):
    return IntentRouter(tasks=None, bills=None, calendar=calendar)


# --- create_event -----------------------------------------------------------

def test_create_event_resolves_to_confirm_with_iso_times():
    res = router(FakeCalendar()).resolve(
        {"intent": "create_event", "confidence": "high", "title": "Дантист",
         "date": "2026-06-28", "start_time": "10:00", "end_time": "11:00"}
    )
    assert res.kind == "confirm"
    assert res.action["type"] == "create_event"
    assert res.action["start"] == dt(2026, 6, 28, 10).isoformat()
    assert res.action["end"] == dt(2026, 6, 28, 11).isoformat()


def test_create_event_defaults_to_one_hour_without_end_time():
    res = router(FakeCalendar()).resolve(
        {"intent": "create_event", "confidence": "high", "title": "Дантист",
         "date": "2026-06-28", "start_time": "10:00", "end_time": None}
    )
    assert res.action["end"] == dt(2026, 6, 28, 11).isoformat()


def test_create_event_warns_about_conflict():
    busy = FakeCalendar([event("a", "Созвон", dt(2026, 6, 28, 10, 30), dt(2026, 6, 28, 11, 30))])
    res = router(busy).resolve(
        {"intent": "create_event", "confidence": "high", "title": "Дантист",
         "date": "2026-06-28", "start_time": "10:00", "end_time": "11:00"}
    )
    assert res.kind == "confirm"
    assert "Пересекается" in res.label
    assert "Созвон" in res.label


def test_create_event_no_conflict_no_warning():
    free = FakeCalendar([event("a", "Созвон", dt(2026, 6, 28, 14), dt(2026, 6, 28, 15))])
    res = router(free).resolve(
        {"intent": "create_event", "confidence": "high", "title": "Дантист",
         "date": "2026-06-28", "start_time": "10:00", "end_time": "11:00"}
    )
    assert "Пересекается" not in res.label


# --- move_event -------------------------------------------------------------

def test_move_event_preserves_duration():
    cal = FakeCalendar([event("c1", "Созвон", dt(2026, 6, 28, 14), dt(2026, 6, 28, 15))])
    res = router(cal).resolve(
        {"intent": "move_event", "confidence": "high", "title_hint": "созвон",
         "date": "2026-06-29", "start_time": "16:00"}
    )
    assert res.kind == "confirm"
    assert res.action["type"] == "move_event"
    assert res.action["event_id"] == "c1"
    assert res.action["start"] == dt(2026, 6, 29, 16).isoformat()
    assert res.action["end"] == dt(2026, 6, 29, 17).isoformat()  # длительность 1ч сохранена


def test_move_event_not_found_message():
    res = router(FakeCalendar()).resolve(
        {"intent": "move_event", "confidence": "high", "title_hint": "созвон",
         "date": "2026-06-29", "start_time": "16:00"}
    )
    assert res.kind == "message"
    assert "не нашёл" in res.text.lower()


# --- delete_event -----------------------------------------------------------

def test_delete_event_resolves_to_confirm():
    cal = FakeCalendar([event("d1", "Дантист", dt(2026, 6, 28, 10), dt(2026, 6, 28, 11))])
    res = router(cal).resolve(
        {"intent": "delete_event", "confidence": "high", "title_hint": "дантист"}
    )
    assert res.kind == "confirm"
    assert res.action["type"] == "delete_event"
    assert res.action["event_id"] == "d1"


# --- query_events (read-only → execute сразу) -------------------------------

def test_query_events_executes_without_confirm():
    cal = FakeCalendar([event("d1", "Дантист", dt(2026, 6, 28, 10), dt(2026, 6, 28, 11))])
    res = router(cal).resolve(
        {"intent": "query_events", "confidence": "high", "filter": "today"}
    )
    assert res.kind == "execute"
    assert res.action["type"] == "query_events"


# --- календарь не настроен ---------------------------------------------------

def test_calendar_intent_without_calendar_returns_message():
    res = router(None).resolve(
        {"intent": "create_event", "confidence": "high", "title": "X",
         "date": "2026-06-28", "start_time": "10:00"}
    )
    assert res.kind == "message"
    assert "календар" in res.text.lower()


# --- execute вызывает клиента -----------------------------------------------

def test_execute_create_event_calls_client():
    cal = FakeCalendar()
    reply = router(cal).execute(
        {"type": "create_event", "title": "Дантист",
         "start": dt(2026, 6, 28, 10).isoformat(), "end": dt(2026, 6, 28, 11).isoformat()}
    )
    assert len(cal.created) == 1
    assert cal.created[0]["title"] == "Дантист"
    assert "Дантист" in reply


def test_execute_delete_event_calls_client():
    cal = FakeCalendar()
    reply = router(cal).execute({"type": "delete_event", "event_id": "d1", "title": "Дантист"})
    assert cal.deleted == ["d1"]
    assert "Дантист" in reply


# --- логика напоминаний (чистая) -------------------------------------------

def test_events_to_remind_includes_event_in_window():
    now = dt(2026, 6, 28, 9, 0)
    horizon = dt(2026, 6, 28, 9, 15)
    events = [event("a", "Созвон", dt(2026, 6, 28, 9, 10), dt(2026, 6, 28, 10))]
    due = events_to_remind(events, now, horizon, reminded=set())
    assert [e["id"] for e in due] == ["a"]


def test_events_to_remind_skips_already_reminded():
    now = dt(2026, 6, 28, 9, 0)
    horizon = dt(2026, 6, 28, 9, 15)
    events = [event("a", "Созвон", dt(2026, 6, 28, 9, 10), dt(2026, 6, 28, 10))]
    assert events_to_remind(events, now, horizon, reminded={"a"}) == []


def test_events_to_remind_skips_event_beyond_horizon():
    now = dt(2026, 6, 28, 9, 0)
    horizon = dt(2026, 6, 28, 9, 15)
    events = [event("a", "Созвон", dt(2026, 6, 28, 11), dt(2026, 6, 28, 12))]
    assert events_to_remind(events, now, horizon, reminded=set()) == []


def test_events_to_remind_skips_already_started():
    now = dt(2026, 6, 28, 9, 0)
    horizon = dt(2026, 6, 28, 9, 15)
    events = [event("a", "Созвон", dt(2026, 6, 28, 8, 50), dt(2026, 6, 28, 10))]
    assert events_to_remind(events, now, horizon, reminded=set()) == []


# --- web: /api/calendar/today ----------------------------------------------

def _client(calendar):
    from fastapi.testclient import TestClient

    from web.server import create_app

    app = create_app(None, None, None, None, None, calendar=calendar)
    return TestClient(app)


def test_calendar_today_returns_empty_without_calendar():
    resp = _client(None).get("/api/calendar/today")
    assert resp.status_code == 200
    assert resp.json() == []


def test_calendar_today_serializes_events():
    cal = FakeCalendar([event("a", "Созвон", dt(2026, 6, 28, 10), dt(2026, 6, 28, 11))])
    resp = _client(cal).get("/api/calendar/today")
    assert resp.status_code == 200
    body = resp.json()
    assert body[0]["id"] == "a"
    assert body[0]["title"] == "Созвон"
    assert body[0]["start"] == dt(2026, 6, 28, 10).isoformat()


def test_calendar_today_returns_empty_on_api_error():
    class Broken:
        timezone = "Europe/Moscow"

        def list_events(self, start, end):
            raise RuntimeError("API down")

    resp = _client(Broken()).get("/api/calendar/today")
    assert resp.status_code == 200
    assert resp.json() == []
