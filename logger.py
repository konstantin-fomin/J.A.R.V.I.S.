"""SQLite-журнал действий с возможностью отмены. Простая обёртка без ORM —
как tasks.py/bills.py. Таблица создаётся автоматически при первом обращении.

Каждая мутация над task/bill/calendar_event пишется сюда до/после изменения
(before_state/after_state в JSON). Отмена (undo_last) находит самую свежую
запись status='active', реверсирует её и помечает запись undone. См. §10 в
JARVIS_SPEC.md.
"""
import datetime
import json
import sqlite3
from pathlib import Path
from typing import Optional


def _dumps(state: Optional[dict]) -> Optional[str]:
    """dict → JSON-текст для хранения (None остаётся None). default=str —
    подстраховка от datetime, если в состояние просочится не-ISO-значение."""
    if state is None:
        return None
    return json.dumps(state, ensure_ascii=False, default=str)


def _loads(raw: Optional[str]) -> Optional[dict]:
    return json.loads(raw) if raw else None


class ActionLog:
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
                CREATE TABLE IF NOT EXISTS action_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'telegram',
                    entity_type TEXT NOT NULL,
                    entity_id TEXT,
                    action TEXT NOT NULL,
                    before_state TEXT,
                    after_state TEXT,
                    raw_message TEXT,
                    status TEXT NOT NULL DEFAULT 'active'
                )
                """
            )

    def log_action(
        self,
        source: str,
        entity_type: str,
        entity_id: Optional[str],
        action: str,
        before_state: Optional[dict] = None,
        after_state: Optional[dict] = None,
        raw_message: Optional[str] = None,
    ) -> dict:
        """Записывает совершённую мутацию (status='active') и возвращает запись."""
        now = datetime.datetime.utcnow().isoformat()
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO action_log "
                "(timestamp, source, entity_type, entity_id, action, "
                "before_state, after_state, raw_message, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active')",
                (
                    now, source, entity_type,
                    None if entity_id is None else str(entity_id),
                    action, _dumps(before_state), _dumps(after_state), raw_message,
                ),
            )
            log_id = cur.lastrowid
        assert log_id is not None  # свежий INSERT всегда даёт rowid
        rec = self.get(log_id)
        assert rec is not None
        return rec

    def get(self, log_id: int) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM action_log WHERE id = ?", (log_id,)
            ).fetchone()
        return self._row_to_dict(row)

    def latest_active(self) -> Optional[dict]:
        """Самая свежая запись со status='active' — кандидат на отмену."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM action_log WHERE status = 'active' "
                "ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return self._row_to_dict(row)

    def mark_undone(self, log_id: int) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE action_log SET status = 'undone' WHERE id = ?", (log_id,)
            )

    @staticmethod
    def _row_to_dict(row: Optional[sqlite3.Row]) -> Optional[dict]:
        if row is None:
            return None
        rec = dict(row)
        rec["before_state"] = _loads(rec["before_state"])
        rec["after_state"] = _loads(rec["after_state"])
        return rec
