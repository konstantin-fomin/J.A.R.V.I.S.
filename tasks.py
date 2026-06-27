"""SQLite-хранилище задач. Простая обёртка без ORM — как и остальной код проекта.

Таблица создаётся автоматически при первом обращении.
"""
import datetime
import sqlite3
from pathlib import Path
from typing import Optional


class TaskStore:
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
                CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    description TEXT,
                    status TEXT NOT NULL DEFAULT 'todo',
                    priority TEXT NOT NULL DEFAULT 'normal',
                    due_date TEXT,
                    due_time TEXT,
                    source TEXT NOT NULL DEFAULT 'telegram',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )

    def create(
        self,
        title: str,
        description: Optional[str] = None,
        due_date: Optional[str] = None,
        due_time: Optional[str] = None,
        priority: str = "normal",
        source: str = "telegram",
    ) -> dict:
        now = datetime.datetime.utcnow().isoformat()
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO tasks "
                "(title, description, status, priority, due_date, due_time, source, created_at, updated_at) "
                "VALUES (?, ?, 'todo', ?, ?, ?, ?, ?, ?)",
                (title, description, priority, due_date, due_time, source, now, now),
            )
            task_id = cur.lastrowid
        return self.get(task_id)

    def get(self, task_id: int) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return dict(row) if row else None

    def list(self, status: Optional[str] = None, due_date: Optional[str] = None) -> list[dict]:
        query = "SELECT * FROM tasks WHERE 1=1"
        params: list = []
        if status:
            query += " AND status = ?"
            params.append(status)
        if due_date:
            query += " AND due_date = ?"
            params.append(due_date)
        query += " ORDER BY COALESCE(due_time, '23:59'), id"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    def update(self, task_id: int, **fields) -> Optional[dict]:
        fields = {k: v for k, v in fields.items() if v is not None}
        if not fields:
            return self.get(task_id)
        fields["updated_at"] = datetime.datetime.utcnow().isoformat()
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        with self._connect() as conn:
            conn.execute(f"UPDATE tasks SET {set_clause} WHERE id = ?", (*fields.values(), task_id))
        return self.get(task_id)

    def delete(self, task_id: int) -> bool:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        return cur.rowcount > 0
