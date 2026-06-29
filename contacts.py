"""SQLite-хранилище контактов (лёгкий персональный CRM). Стиль tasks.py/bills.py —
без ORM. Таблица создаётся автоматически при первом обращении. См. §14 в
JARVIS_SPEC.md.

birthday и last_contact_date хранятся ISO-строками 'ГГГГ-ММ-ДД'. День рождения
повторяется ежегодно: для «ближайших ДР» сравниваем месяц-день, год игнорируем.
"""
from __future__ import annotations  # метод list() не должен затенять list[...] в аннотациях

import datetime
import sqlite3
from datetime import date
from pathlib import Path
from typing import Optional


def days_until_birthday(birthday: date, today: date) -> int:
    """Сколько дней до ближайшего дня рождения (0 — сегодня, 1 — завтра, …).

    Год игнорируется. 29 февраля в невисокосный год отмечаем 1 марта."""
    def anniversary(year: int) -> date:
        try:
            return birthday.replace(year=year)
        except ValueError:  # 29 февраля в невисокосном году
            return date(year, 3, 1)

    this_year = anniversary(today.year)
    if this_year < today:
        this_year = anniversary(today.year + 1)
    return (this_year - today).days


class ContactStore:
    def __init__(self, db_path: Path):
        self._db_path = db_path
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS contacts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    last_contact_date TEXT,
                    birthday TEXT,
                    notes TEXT,
                    email TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            # Миграция старых баз: добавляем email, если колонки ещё нет (§20).
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(contacts)")}
            if "email" not in cols:
                conn.execute("ALTER TABLE contacts ADD COLUMN email TEXT")

    def create(
        self,
        name: str,
        last_contact_date: Optional[str] = None,
        birthday: Optional[str] = None,
        notes: Optional[str] = None,
        email: Optional[str] = None,
    ) -> dict:
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO contacts "
                "(name, last_contact_date, birthday, notes, email, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (name, last_contact_date, birthday, notes, email, now, now),
            )
            contact_id = cur.lastrowid
        assert contact_id is not None  # свежий INSERT всегда даёт rowid
        created = self.get(contact_id)
        assert created is not None
        return created

    def get(self, contact_id: int) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM contacts WHERE id = ?", (contact_id,)).fetchone()
        return dict(row) if row else None

    def list(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM contacts ORDER BY name COLLATE NOCASE, id").fetchall()
        return [dict(r) for r in rows]

    def update(self, contact_id: int, **fields) -> Optional[dict]:
        fields = {k: v for k, v in fields.items() if v is not None}
        if not fields:
            return self.get(contact_id)
        fields["updated_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        with self._connect() as conn:
            conn.execute(f"UPDATE contacts SET {set_clause} WHERE id = ?", (*fields.values(), contact_id))
        return self.get(contact_id)

    def delete(self, contact_id: int) -> bool:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM contacts WHERE id = ?", (contact_id,))
        return cur.rowcount > 0

    def find(self, name_hint: Optional[str]) -> list[dict]:
        """Подстрочный регистронезависимый матч по имени. Склонения нормализует
        парсер интентов (именительный падеж), как для title_hint."""
        hint = (name_hint or "").strip().lower()
        if not hint:
            return []
        # Python .lower() корректен и для кириллицы, в отличие от SQL LIKE.
        return [c for c in self.list() if hint in c["name"].lower()]

    def find_by_email(self, email: str) -> Optional[dict]:
        """Точный регистронезависимый матч по email. Возвращает первого совпавшего или None."""
        needle = email.strip().lower()
        if not needle:
            return None
        for c in self.list():
            if c.get("email") and c["email"].strip().lower() == needle:
                return c
        return None

    def upcoming_birthdays(self, within_days: int, today: Optional[date] = None) -> list[dict]:
        """Контакты с ДР в окне [сегодня, сегодня+within_days], по возрастанию
        числа дней до праздника. Контакты без birthday не попадают."""
        today = today or date.today()
        upcoming = []
        for c in self.list():
            if not c["birthday"]:
                continue
            try:
                bday = date.fromisoformat(c["birthday"])
            except ValueError:
                continue
            days = days_until_birthday(bday, today)
            if days <= within_days:
                upcoming.append((days, c))
        upcoming.sort(key=lambda pair: pair[0])
        return [c for _, c in upcoming]
