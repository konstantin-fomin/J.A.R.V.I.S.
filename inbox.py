"""SQLite-хранилище инбокса: быстрый захват без классификации, разбор позже.

Простая обёртка без ORM — как tasks.py/bills.py. Таблица создаётся автоматически
при первом обращении. Запись живёт в статусе 'pending', пока её не превратят в
задачу (кнопка «→ в задачу» в /inbox) — тогда статус становится 'processed'.

Очереди разбора (§19.2): сверх pending/processed запись можно переклассифицировать
в 'someday' / 'needs_decision' / 'maybe_later' (intent inbox_reclassify). Статус —
свободный TEXT, так что схема не меняется; меняется лишь набор значений и
группировка в /inbox. capture журналируется и отменяем (отсюда delete).
"""
import datetime
import sqlite3
from pathlib import Path
from typing import Optional


def convert_inbox_item_to_task(tasks, inbox: "InboxStore", item: dict) -> dict:
    """Общая операция «inbox-запись → задача» для обоих входов (REST-эндпоинт и
    NL-intent inbox_to_task): создаёт задачу с ДОСЛОВНЫМ текстом записи (без LLM —
    быстро и предсказуемо) и помечает запись processed. Возвращает созданную задачу.

    Логирование/undo здесь НЕ делаем — это решает вызывающий: REST идёт мимо
    ActionLog (как PATCH /api/tasks, §10), а NL-путь оборачивается IntentRouter."""
    task = tasks.create(title=item["text"], source="inbox")
    inbox.set_status(item["id"], "processed")
    return task


class InboxStore:
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
                CREATE TABLE IF NOT EXISTS inbox_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    text TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'telegram',
                    created_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending'
                )
                """
            )

    def create(self, text: str, source: str = "telegram") -> dict:
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO inbox_items (text, source, created_at, status) "
                "VALUES (?, ?, ?, 'pending')",
                (text, source, now),
            )
            item_id = cur.lastrowid
        assert item_id is not None
        item = self.get(item_id)
        assert item is not None
        return item

    def get(self, item_id: int) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM inbox_items WHERE id = ?", (item_id,)
            ).fetchone()
        return dict(row) if row else None

    def list(self, status: Optional[str] = None) -> list[dict]:
        query = "SELECT * FROM inbox_items"
        params: list = []
        if status:
            query += " WHERE status = ?"
            params.append(status)
        query += " ORDER BY id"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    def set_status(self, item_id: int, status: str) -> Optional[dict]:
        with self._connect() as conn:
            conn.execute(
                "UPDATE inbox_items SET status = ? WHERE id = ?", (status, item_id)
            )
        return self.get(item_id)

    def delete(self, item_id: int) -> bool:
        """Удаляет запись инбокса (нужно для отмены capture через undo_last, §19.2)."""
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM inbox_items WHERE id = ?", (item_id,))
        return cur.rowcount > 0
