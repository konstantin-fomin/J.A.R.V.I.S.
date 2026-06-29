"""Тесты §20 Entity linking & Contact-aware reminders.

Покрывает:
  (a) Миграция: contacts.email, tasks.contact_id
  (b) create_task: автоматическая привязка contact_id по подстроке в title
  (c) query_by_contact: сборка профиля контакта (задачи + obligations + события)
  (d) calendar_client.list_events: поле attendees
  (e) build_reminder_text: секция контакта по email из attendees
  (f) stale_contact_reminder: дедуп через SuggestionLog

Сеть/Telegram/LLM/Google API не дёргаем.
"""
import datetime
import sqlite3
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

TZ = ZoneInfo("Europe/Moscow")


# ── helpers ──────────────────────────────────────────────────────────────────

def make_router(tmp_path, contacts=None, obligations=None, calendar=None):
    """IntentRouter на изолированных tmp-базах — как в других тестах."""
    from contacts import ContactStore
    from intents import IntentRouter
    from logger import ActionLog
    from obligations import ObligationStore
    from tasks import TaskStore

    tasks = TaskStore(tmp_path / "tasks.db")
    log = ActionLog(tmp_path / "log.db")
    cs = contacts or ContactStore(tmp_path / "contacts.db")
    obs = obligations or ObligationStore(tmp_path / "obl.db")
    return IntentRouter(tasks=tasks, bills=None, action_log=log, contacts=cs,
                        obligations=obs, calendar=calendar)


# ══════════════════════════════════════════════════════════════════════════════
# (a) МИГРАЦИЯ
# ══════════════════════════════════════════════════════════════════════════════

class TestMigration:
    def test_contacts_email_added_to_fresh_db(self, tmp_path):
        """Свежая БД контактов содержит поле email."""
        from contacts import ContactStore
        ContactStore(tmp_path / "c.db")
        conn = sqlite3.connect(tmp_path / "c.db")
        cols = {r[1] for r in conn.execute("PRAGMA table_info(contacts)")}
        conn.close()
        assert "email" in cols

    def test_contacts_email_migration_on_existing_db(self, tmp_path):
        """ALTER TABLE добавляет email в старую базу без email (как на проде)."""
        db = tmp_path / "c.db"
        # создаём старую схему без email
        conn = sqlite3.connect(db)
        conn.execute("""
            CREATE TABLE contacts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                last_contact_date TEXT,
                birthday TEXT,
                notes TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        conn.execute("INSERT INTO contacts (name, created_at, updated_at) "
                     "VALUES ('Пётр', '2026-01-01', '2026-01-01')")
        conn.commit()
        conn.close()

        from contacts import ContactStore
        ContactStore(db)  # должна тихо смигрировать

        conn = sqlite3.connect(db)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(contacts)")}
        rows = conn.execute("SELECT email FROM contacts WHERE name='Пётр'").fetchall()
        conn.close()

        assert "email" in cols
        assert rows[0][0] is None  # существующие данные целы, email=NULL

    def test_tasks_contact_id_added_to_fresh_db(self, tmp_path):
        """Свежая БД задач содержит поле contact_id."""
        from tasks import TaskStore
        TaskStore(tmp_path / "t.db")
        conn = sqlite3.connect(tmp_path / "t.db")
        cols = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
        conn.close()
        assert "contact_id" in cols

    def test_tasks_contact_id_migration_on_backup_db(self):
        """Миграция на реальном бэкапе tasks.db: данные целы, contact_id добавлен."""
        import shutil, tempfile
        backup = Path(
            "/tmp/claude-0/-root-J-A-R-V-I-S-/b332f8e5-36f9-453b-9546-33fd966ecefe"
            "/scratchpad/backup_test/jarvis-backup-2026-06-29/jarvis/tasks.db"
        )
        if not backup.exists():
            pytest.skip("бэкап не распакован")

        with tempfile.TemporaryDirectory() as td:
            dst = Path(td) / "tasks.db"
            shutil.copy(backup, dst)

            # До миграции: project не было в бэкапе — убедимся
            conn = sqlite3.connect(dst)
            cols_before = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
            count_before = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
            conn.close()

            from tasks import TaskStore
            TaskStore(dst)  # миграция

            conn = sqlite3.connect(dst)
            cols_after = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
            count_after = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
            conn.close()

        assert "contact_id" in cols_after
        assert count_after == count_before  # ни одна запись не потеряна


# ══════════════════════════════════════════════════════════════════════════════
# (b) create_task: автоматическая привязка contact_id
# ══════════════════════════════════════════════════════════════════════════════

class TestCreateTaskContactLinking:
    def test_contact_linked_when_name_in_title(self, tmp_path):
        """Если в title есть имя контакта — contact_id проставляется автоматически."""
        from contacts import ContactStore
        from tasks import TaskStore
        from intents import IntentRouter
        from logger import ActionLog

        cs = ContactStore(tmp_path / "c.db")
        contact = cs.create(name="Пётр", email="petr@example.com")
        tasks = TaskStore(tmp_path / "t.db")
        router = IntentRouter(tasks=tasks, bills=None,
                              action_log=ActionLog(tmp_path / "log.db"),
                              contacts=cs)

        router.execute(
            {"type": "create_task", "params": {"title": "Позвонить Петру", "source": "telegram"}}
        )
        created = tasks.list()[0]
        assert created["contact_id"] == contact["id"]

    def test_no_contact_link_when_no_match(self, tmp_path):
        """Если совпадения нет — задача создаётся с contact_id=None."""
        from contacts import ContactStore
        from tasks import TaskStore
        from intents import IntentRouter
        from logger import ActionLog

        cs = ContactStore(tmp_path / "c.db")
        cs.create(name="Аня")
        tasks = TaskStore(tmp_path / "t.db")
        router = IntentRouter(tasks=tasks, bills=None,
                              action_log=ActionLog(tmp_path / "log.db"),
                              contacts=cs)

        router.execute(
            {"type": "create_task", "params": {"title": "Купить молоко", "source": "telegram"}}
        )
        assert tasks.list()[0]["contact_id"] is None

    def test_contact_linking_best_effort_no_contacts(self, tmp_path):
        """Без contacts-стора задача создаётся нормально (contact_id=None)."""
        from tasks import TaskStore
        from intents import IntentRouter
        from logger import ActionLog

        tasks = TaskStore(tmp_path / "t.db")
        router = IntentRouter(tasks=tasks, bills=None,
                              action_log=ActionLog(tmp_path / "log.db"),
                              contacts=None)

        router.execute(
            {"type": "create_task", "params": {"title": "Позвонить Петру", "source": "telegram"}}
        )
        assert tasks.list()[0]["contact_id"] is None


# ══════════════════════════════════════════════════════════════════════════════
# (c) query_by_contact
# ══════════════════════════════════════════════════════════════════════════════

class TestQueryByContact:
    def _setup(self, tmp_path):
        from contacts import ContactStore
        from tasks import TaskStore
        from intents import IntentRouter
        from logger import ActionLog
        from obligations import ObligationStore

        cs = ContactStore(tmp_path / "c.db")
        ts = TaskStore(tmp_path / "t.db")
        obs = ObligationStore(tmp_path / "obl.db")
        log = ActionLog(tmp_path / "log.db")
        router = IntentRouter(tasks=ts, bills=None, action_log=log,
                              contacts=cs, obligations=obs)
        return cs, ts, obs, router

    def test_query_by_contact_shows_contact_info(self, tmp_path):
        cs, ts, obs, router = self._setup(tmp_path)
        cs.create(name="Аня", last_contact_date="2026-06-01",
                  email="anya@example.com")

        resolution = router.resolve({"intent": "query_by_contact", "name_hint": "Аня"})
        assert resolution.kind == "execute"
        text = router.execute(resolution.action)
        assert "Аня" in text

    def test_query_by_contact_not_found(self, tmp_path):
        cs, ts, obs, router = self._setup(tmp_path)

        resolution = router.resolve({"intent": "query_by_contact", "name_hint": "Вася"})
        assert resolution.kind == "message"

    def test_query_by_contact_includes_linked_tasks(self, tmp_path):
        cs, ts, obs, router = self._setup(tmp_path)
        contact = cs.create(name="Аня")
        ts.create(title="Написать Ане", contact_id=contact["id"])

        resolution = router.resolve({"intent": "query_by_contact", "name_hint": "Аня"})
        text = router.execute(resolution.action)
        assert "Написать Ане" in text

    def test_query_by_contact_includes_obligations(self, tmp_path):
        cs, ts, obs, router = self._setup(tmp_path)
        cs.create(name="Аня")
        obs.create(title="Отдать книгу", person="Аня", direction="i_owe",
                   since_date="2026-06-01")

        resolution = router.resolve({"intent": "query_by_contact", "name_hint": "Аня"})
        text = router.execute(resolution.action)
        assert "Отдать книгу" in text

    def test_query_by_contact_shows_last_contact_date(self, tmp_path):
        cs, ts, obs, router = self._setup(tmp_path)
        cs.create(name="Аня", last_contact_date="2026-05-15")

        resolution = router.resolve({"intent": "query_by_contact", "name_hint": "Аня"})
        text = router.execute(resolution.action)
        assert "2026-05-15" in text

    def test_query_by_contact_events_via_email_match(self, tmp_path):
        """query_by_contact включает события за ±14 дней если email совпал с attendees."""
        cs, ts, obs, router = self._setup(tmp_path)
        cs.create(name="Аня", email="anya@example.com")

        now = datetime.datetime(2026, 6, 29, 12, 0, tzinfo=TZ)
        soon = now + datetime.timedelta(days=3)

        class FakeCalendar:
            def list_events(self, start, end):
                return [{
                    "id": "ev1",
                    "title": "Созвон с Аней",
                    "start": soon,
                    "end": soon + datetime.timedelta(hours=1),
                    "attendees": ["anya@example.com", "me@work.com"],
                }]

        router.calendar = FakeCalendar()
        resolution = router.resolve({"intent": "query_by_contact", "name_hint": "Аня"})
        text = router.execute(resolution.action)
        assert "Созвон с Аней" in text

    def test_query_by_contact_intent_in_intents_set(self):
        from intents import INTENTS
        assert "query_by_contact" in INTENTS

    def test_query_by_contact_is_safe(self):
        from intents import RISK_LEVELS
        assert RISK_LEVELS.get("query_by_contact") == "safe"


# ══════════════════════════════════════════════════════════════════════════════
# (d) calendar_client.list_events: поле attendees
# ══════════════════════════════════════════════════════════════════════════════

class TestCalendarAttendees:
    def test_list_events_includes_attendees(self):
        """_parse_google_item должен включать attendees из event['attendees']."""
        from calendar_client import _parse_google_item
        item = {
            "id": "abc",
            "summary": "Созвон",
            "start": {"dateTime": "2026-06-29T10:00:00+03:00"},
            "end": {"dateTime": "2026-06-29T11:00:00+03:00"},
            "attendees": [
                {"email": "anya@example.com", "responseStatus": "accepted"},
                {"email": "me@work.com", "responseStatus": "accepted"},
            ],
        }
        from zoneinfo import ZoneInfo
        event = _parse_google_item(item, ZoneInfo("Europe/Moscow"))
        assert "attendees" in event
        assert "anya@example.com" in event["attendees"]

    def test_list_events_attendees_empty_when_absent(self):
        from calendar_client import _parse_google_item
        item = {
            "id": "abc",
            "summary": "Одиночная",
            "start": {"dateTime": "2026-06-29T10:00:00+03:00"},
            "end": {"dateTime": "2026-06-29T11:00:00+03:00"},
        }
        from zoneinfo import ZoneInfo
        event = _parse_google_item(item, ZoneInfo("Europe/Moscow"))
        assert event.get("attendees", []) == []


# ══════════════════════════════════════════════════════════════════════════════
# (e) build_reminder_text: секция контакта по email из attendees
# ══════════════════════════════════════════════════════════════════════════════

class TestPreMeetingContactSection:
    def _event(self, attendees=None):
        now = datetime.datetime(2026, 6, 29, 15, 0, tzinfo=TZ)
        return {
            "id": "e1",
            "title": "Созвон с Аней",
            "start": now,
            "end": now + datetime.timedelta(hours=1),
            "description": "",
            "attendees": attendees or [],
        }

    def test_contact_section_added_when_email_matches(self, tmp_path):
        """Если attendee email совпадает с contacts.email — добавляется секция контакта."""
        from contacts import ContactStore
        from bot.telegram_bot import build_reminder_text

        cs = ContactStore(tmp_path / "c.db")
        cs.create(name="Аня", email="anya@example.com",
                  last_contact_date="2026-06-01")

        ev = self._event(attendees=["anya@example.com"])
        text = build_reminder_text(ev, 15, memory=None, contacts=cs)

        assert "Аня" in text
        assert "2026-06-01" in text

    def test_no_contact_section_when_no_email_match(self, tmp_path):
        from contacts import ContactStore
        from bot.telegram_bot import build_reminder_text

        cs = ContactStore(tmp_path / "c.db")
        cs.create(name="Аня", email="other@example.com")

        ev = self._event(attendees=["anya@example.com"])
        text = build_reminder_text(ev, 15, memory=None, contacts=cs)

        assert "Контакт" not in text or "other" not in text

    def test_contact_section_includes_open_obligations(self, tmp_path):
        from contacts import ContactStore
        from obligations import ObligationStore
        from bot.telegram_bot import build_reminder_text

        cs = ContactStore(tmp_path / "c.db")
        cs.create(name="Аня", email="anya@example.com")
        obs = ObligationStore(tmp_path / "obl.db")
        obs.create(title="Вернуть долг", person="Аня", direction="i_owe",
                   since_date="2026-06-01")

        ev = self._event(attendees=["anya@example.com"])
        text = build_reminder_text(ev, 15, memory=None, contacts=cs, obligations=obs)

        assert "Вернуть долг" in text

    def test_existing_memory_notes_preserved(self, tmp_path):
        """Секция контакта добавляется ДОПОЛНИТЕЛЬНО к заметкам, а не вместо."""
        from contacts import ContactStore
        from bot.telegram_bot import build_reminder_text

        cs = ContactStore(tmp_path / "c.db")
        cs.create(name="Аня", email="anya@example.com",
                  last_contact_date="2026-06-01")

        class FakeMemory:
            def relevant_notes(self, q, k, max_distance):
                return [("Аня любит джаз", "topics/anya.md")]

        ev = self._event(attendees=["anya@example.com"])
        text = build_reminder_text(ev, 15, memory=FakeMemory(), contacts=cs)

        assert "Аня любит джаз" in text
        assert "2026-06-01" in text  # секция контакта тоже

    def test_build_reminder_text_no_contacts_unchanged(self):
        """Без contacts-параметра поведение неизменно (обратная совместимость)."""
        from bot.telegram_bot import build_reminder_text

        ev = self._event(attendees=["x@y.com"])
        text = build_reminder_text(ev, 15, memory=None)  # contacts не передаём
        assert "🔔 Через 15 мин встреча" in text
        assert "Контакт" not in text


# ══════════════════════════════════════════════════════════════════════════════
# (f) stale_contact_reminder
# ══════════════════════════════════════════════════════════════════════════════

class TestStaleContactReminder:
    def test_stale_contacts_found(self, tmp_path):
        """contacts_stale_for(contacts, days=14) возвращает тех, с кем не общались 14+ дней."""
        from contacts import ContactStore
        from bot.telegram_bot import contacts_stale_for

        cs = ContactStore(tmp_path / "c.db")
        cs.create(name="Аня", last_contact_date="2026-06-01")  # 28 дней назад
        cs.create(name="Вася", last_contact_date="2026-06-28")  # 1 день назад
        cs.create(name="Миша")  # нет даты — игнорируем

        today = datetime.date(2026, 6, 29)
        stale = contacts_stale_for(cs, days=14, today=today)

        names = [c["name"] for c in stale]
        assert "Аня" in names
        assert "Вася" not in names
        assert "Миша" not in names

    def test_stale_contact_not_repeated_within_block_days(self, tmp_path):
        """Уже показанный контакт не попадает в reminder повторно (дедуп SuggestionLog)."""
        from contacts import ContactStore
        from suggestions import SuggestionLog, theme_hash
        from bot.telegram_bot import contacts_stale_for, filter_stale_for_send

        cs = ContactStore(tmp_path / "c.db")
        cs.create(name="Аня", last_contact_date="2026-06-01")
        log = SuggestionLog(tmp_path / "sugg.db")

        today = datetime.date(2026, 6, 29)
        stale = contacts_stale_for(cs, days=14, today=today)
        # Имитируем: показали вчера
        log.mark_suggested(theme_hash(f"stale:{stale[0]['id']}"), "Аня",
                           when=today - datetime.timedelta(days=1))

        to_send = filter_stale_for_send(stale, log, block_days=7, today=today)
        assert to_send == []

    def test_stale_contact_shown_after_block_expires(self, tmp_path):
        """По истечении block_days контакт снова попадает в напоминание."""
        from contacts import ContactStore
        from suggestions import SuggestionLog, theme_hash
        from bot.telegram_bot import contacts_stale_for, filter_stale_for_send

        cs = ContactStore(tmp_path / "c.db")
        cs.create(name="Аня", last_contact_date="2026-06-01")
        log = SuggestionLog(tmp_path / "sugg.db")

        today = datetime.date(2026, 6, 29)
        stale = contacts_stale_for(cs, days=14, today=today)
        # Показали давно — 10 дней назад (block=7, уже прошёл)
        log.mark_suggested(theme_hash(f"stale:{stale[0]['id']}"), "Аня",
                           when=today - datetime.timedelta(days=10))

        to_send = filter_stale_for_send(stale, log, block_days=7, today=today)
        assert len(to_send) == 1
        assert to_send[0]["name"] == "Аня"
