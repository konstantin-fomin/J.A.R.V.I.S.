"""SQLite-хранилище обязательств (§19.1): кто кому что должен / чего ждёшь.

Стиль tasks.py/contacts.py — чистый sqlite3 без ORM. Таблица создаётся
автоматически при первом обращении. Это НЕ задачи: у задачи есть срок и она «моя
к выполнению», а обязательство — про отношения с человеком («жду от Пети отчёт»,
«я должен Маше денег»).

direction: 'waiting_on' — жду от кого-то; 'i_owe' — я должен.
status:    'open' | 'done' | 'cancelled'.
"""
from __future__ import annotations  # list() не должен затенять list[...] в аннотациях

import datetime
import sqlite3
from datetime import date
from pathlib import Path
from typing import Optional


class ObligationStore:
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
                CREATE TABLE IF NOT EXISTS obligations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    person TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    since_date TEXT NOT NULL,
                    follow_up_date TEXT,
                    status TEXT NOT NULL DEFAULT 'open',
                    source TEXT NOT NULL DEFAULT 'telegram',
                    related_project TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )

    def create(
        self,
        title: str,
        person: str,
        direction: str,
        since_date: Optional[str] = None,
        follow_up_date: Optional[str] = None,
        related_project: Optional[str] = None,
        source: str = "telegram",
    ) -> dict:
        since_date = since_date or date.today().isoformat()  # по умолчанию — сегодня
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO obligations "
                "(title, person, direction, since_date, follow_up_date, status, "
                "source, related_project, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, 'open', ?, ?, ?, ?)",
                (title, person, direction, since_date, follow_up_date,
                 source, related_project, now, now),
            )
            obl_id = cur.lastrowid
        assert obl_id is not None  # свежий INSERT всегда даёт rowid
        created = self.get(obl_id)
        assert created is not None
        return created

    def get(self, obl_id: int) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM obligations WHERE id = ?", (obl_id,)).fetchone()
        return dict(row) if row else None

    def list(
        self,
        direction: Optional[str] = None,
        person: Optional[str] = None,
        status: Optional[str] = None,
    ) -> list[dict]:
        """direction/status — точный матч (SQL); person — подстрочный
        регистронезависимый (Python .lower(), корректно для кириллицы)."""
        query = "SELECT * FROM obligations WHERE 1=1"
        params: list = []
        if direction:
            query += " AND direction = ?"
            params.append(direction)
        if status:
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY COALESCE(follow_up_date, '9999-12-31'), id"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        items = [dict(r) for r in rows]
        if person:
            needle = person.strip().lower()
            items = [o for o in items if needle in o["person"].lower()]
        return items

    def find(self, title_hint: Optional[str], status: Optional[str] = None) -> list[dict]:
        """Подстрочный регистронезависимый матч по title (склонения нормализует
        парсер интентов — именительный падеж, как для task title_hint)."""
        hint = (title_hint or "").strip().lower()
        if not hint:
            return []
        return [o for o in self.list(status=status) if hint in o["title"].lower()]

    def due_followups(self, today: date) -> list[dict]:
        """Открытые обязательства с follow_up_date <= today — для ежедневного job'а
        follow_up_obligations. Без follow_up_date не попадают."""
        cutoff = today.isoformat()
        return [
            o for o in self.list(status="open")
            if o["follow_up_date"] and o["follow_up_date"] <= cutoff
        ]

    def update(self, obl_id: int, **fields) -> Optional[dict]:
        fields = {k: v for k, v in fields.items() if v is not None}
        if not fields:
            return self.get(obl_id)
        fields["updated_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        with self._connect() as conn:
            conn.execute(f"UPDATE obligations SET {set_clause} WHERE id = ?", (*fields.values(), obl_id))
        return self.get(obl_id)

    def delete(self, obl_id: int) -> bool:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM obligations WHERE id = ?", (obl_id,))
        return cur.rowcount > 0
