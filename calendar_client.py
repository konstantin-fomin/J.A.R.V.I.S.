"""Обёртка над Google Calendar API + чистая логика пересечений встреч.

OAuth: credentials.json (OAuth-клиент из Google Cloud Console) + token.json
(рефреш-токен, генерится отдельным generate_calendar_token.py на машине с
браузером — не на headless VPS). Access-токен бот обновляет сам.

Google-импорты ленивые (внутри методов): модуль грузится даже без установленных
либ/токена, поэтому select_conflicts тестируется без сети. См. JARVIS_SPEC.md §9.
"""
import datetime as dt
import logging
from pathlib import Path
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/calendar"]


class CalendarError(Exception):
    """Ошибка работы с календарём (нет токена, отказ API и т.п.)."""


def select_conflicts(events: list[dict], start, end, ignore_id=None) -> list[dict]:
    """Чистая функция: события из events, пересекающиеся с интервалом [start, end).

    Пересечение строгое (a.start < end and start < a.end) — встык не считается.
    ignore_id исключает саму переносимую встречу из конфликтов с собой."""
    return [
        e
        for e in events
        if e.get("id") != ignore_id and e["start"] < end and start < e["end"]
    ]


def events_to_remind(events: list[dict], now, horizon, reminded: set) -> list[dict]:
    """События, по которым пора напомнить: начало в (now, horizon] и ещё не напоминали.

    reminded — множество id уже разосланных напоминаний (чтобы не дублировать)."""
    return [
        e
        for e in events
        if e["id"] not in reminded and now <= e["start"] <= horizon
    ]


def _parse_google_dt(value: dict, tz) -> dt.datetime:
    """Google 'start'/'end' ('dateTime' или 'date' для весь-день) → aware datetime."""
    if value.get("dateTime"):
        return dt.datetime.fromisoformat(value["dateTime"])
    day = dt.date.fromisoformat(value["date"])
    return dt.datetime.combine(day, dt.time.min, tzinfo=tz)


def _parse_google_item(item: dict, tz) -> dict:
    """Один Google Calendar event item → внутренний dict. §20: добавляем attendees."""
    attendees = [
        a["email"] for a in item.get("attendees", [])
        if a.get("email")
    ]
    return {
        "id": item["id"],
        "title": item.get("summary", "(без названия)"),
        "description": item.get("description", ""),
        "start": _parse_google_dt(item["start"], tz),
        "end": _parse_google_dt(item["end"], tz),
        "html_link": item.get("htmlLink", ""),
        "attendees": attendees,
    }


class CalendarClient:
    """Тонкая обёртка над Google Calendar v3 (календарь 'primary')."""

    def __init__(self, credentials_path, token_path, timezone: str):
        self.credentials_path = Path(credentials_path)
        self.token_path = Path(token_path)
        self.timezone = timezone
        self._tz = ZoneInfo(timezone)
        self._service = None

    def _get_service(self):
        """Ленивая сборка сервиса: читает token.json, при необходимости рефрешит."""
        if self._service is not None:
            return self._service
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        if not self.token_path.exists():
            raise CalendarError(f"token.json не найден: {self.token_path}")
        creds = Credentials.from_authorized_user_file(str(self.token_path), SCOPES)
        if not creds.valid:
            if creds.expired and creds.refresh_token:
                creds.refresh(Request())
                self.token_path.write_text(creds.to_json())
            else:
                raise CalendarError("token.json недействителен — перегенерируй (см. §9)")
        self._service = build("calendar", "v3", credentials=creds, cache_discovery=False)
        return self._service

    def list_events(self, start, end) -> list[dict]:
        """Встречи, пересекающие [start, end), отсортированы по началу."""
        resp = (
            self._get_service()
            .events()
            .list(
                calendarId="primary",
                timeMin=start.isoformat(),
                timeMax=end.isoformat(),
                singleEvents=True,
                orderBy="startTime",
            )
            .execute()
        )
        return [_parse_google_item(item, self._tz) for item in resp.get("items", [])]

    def get_event(self, event_id) -> dict | None:
        """Одна встреча по id (или None, если не найдена/удалена). Нужно для
        снимка before_state в журнале действий перед переносом/удалением."""
        from googleapiclient.errors import HttpError

        try:
            item = (
                self._get_service()
                .events()
                .get(calendarId="primary", eventId=event_id)
                .execute()
            )
        except HttpError:
            return None
        return {
            "id": item["id"],
            "title": item.get("summary", "(без названия)"),
            "start": _parse_google_dt(item["start"], self._tz),
            "end": _parse_google_dt(item["end"], self._tz),
            "html_link": item.get("htmlLink", ""),
        }

    def create_event(self, title, start, end) -> dict:
        body = {
            "summary": title,
            "start": {"dateTime": start.isoformat(), "timeZone": self.timezone},
            "end": {"dateTime": end.isoformat(), "timeZone": self.timezone},
        }
        item = self._get_service().events().insert(calendarId="primary", body=body).execute()
        return {"id": item["id"], "title": title, "start": start, "end": end,
                "html_link": item.get("htmlLink", "")}

    def update_event(self, event_id, **fields) -> dict:
        body: dict = {}
        if "title" in fields:
            body["summary"] = fields["title"]
        if "start" in fields:
            body["start"] = {"dateTime": fields["start"].isoformat(), "timeZone": self.timezone}
        if "end" in fields:
            body["end"] = {"dateTime": fields["end"].isoformat(), "timeZone": self.timezone}
        item = (
            self._get_service()
            .events()
            .patch(calendarId="primary", eventId=event_id, body=body)
            .execute()
        )
        return {"id": item["id"], "title": item.get("summary", "")}

    def delete_event(self, event_id) -> None:
        self._get_service().events().delete(calendarId="primary", eventId=event_id).execute()

    def find_conflicts(self, start, end, ignore_event_id=None) -> list[dict]:
        """Встречи, пересекающиеся с [start, end). ignore_event_id — для переноса."""
        return select_conflicts(self.list_events(start, end), start, end, ignore_event_id)


def load_calendar():
    """CalendarClient, если есть и credentials.json, и token.json (именно файлы),
    иначе None.

    None означает «календарь не настроен» — бот работает без него. Проверяем
    is_file(), а не exists(): bind-mount несуществующего файла Docker создаёт как
    каталог — такой «токен» невалиден, календарь должен остаться отключённым."""
    import config

    if not config.CALENDAR_CREDENTIALS_PATH.is_file() or not config.CALENDAR_TOKEN_PATH.is_file():
        return None
    return CalendarClient(
        config.CALENDAR_CREDENTIALS_PATH,
        config.CALENDAR_TOKEN_PATH,
        config.CALENDAR_TIMEZONE,
    )
