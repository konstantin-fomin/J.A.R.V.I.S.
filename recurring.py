"""Повторяющиеся задачи (§18.2): SQLite-хранилище шаблонов + генерация инстансов.

Отдельно от Bills намеренно: у платежей фиксированный день месяца, у повторяющихся
задач — гибкая повторяемость (каждый день / по дню недели / по числу месяца).
Паттерн Recurring как у bills.py (шаблон → инстанс), но генерация «на день»
(ensure_day), а инстансы складываются в обычный TaskStore с пометкой
source='recurring'. Таблица создаётся автоматически при первом обращении.
"""
import calendar
import datetime
import sqlite3
from datetime import date
from pathlib import Path
from typing import Optional


class RecurringTaskStore:
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
                CREATE TABLE IF NOT EXISTS recurring_task_templates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    recurrence_type TEXT NOT NULL,
                    day_of_week INTEGER,
                    day_of_month INTEGER,
                    time TEXT,
                    project TEXT,
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )

    # --- Шаблоны -----------------------------------------------------------

    def create_template(
        self,
        title: str,
        recurrence_type: str,
        day_of_week: Optional[int] = None,
        day_of_month: Optional[int] = None,
        time: Optional[str] = None,
        project: Optional[str] = None,
    ) -> dict:
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO recurring_task_templates "
                "(title, recurrence_type, day_of_week, day_of_month, time, project, "
                "active, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)",
                (title, recurrence_type, day_of_week, day_of_month, time, project, now, now),
            )
            template_id = cur.lastrowid
        return self.get_template(template_id)  # type: ignore[return-value]

    def get_template(self, template_id: int) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM recurring_task_templates WHERE id = ?", (template_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_templates(self, active_only: bool = False) -> list[dict]:
        query = "SELECT * FROM recurring_task_templates"
        if active_only:
            query += " WHERE active = 1"
        query += " ORDER BY id"
        with self._connect() as conn:
            rows = conn.execute(query).fetchall()
        return [dict(r) for r in rows]

    def update_template(self, template_id: int, **fields) -> Optional[dict]:
        fields = {k: v for k, v in fields.items() if v is not None}
        if not fields:
            return self.get_template(template_id)
        if "active" in fields:
            fields["active"] = 1 if fields["active"] else 0
        fields["updated_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        with self._connect() as conn:
            conn.execute(
                f"UPDATE recurring_task_templates SET {set_clause} WHERE id = ?",
                (*fields.values(), template_id),
            )
        return self.get_template(template_id)

    def delete_template(self, template_id: int) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM recurring_task_templates WHERE id = ?", (template_id,)
            )
        return cur.rowcount > 0

    # --- Генерация инстансов -----------------------------------------------

    @staticmethod
    def _fires_on(template: dict, target: date) -> bool:
        """Стреляет ли шаблон в конкретный день. monthly зажимает day_of_month до
        последнего дня месяца (как _due_date в bills.py) — иначе 31-е в феврале
        никогда бы не сработало."""
        rtype = template["recurrence_type"]
        if rtype == "daily":
            return True
        if rtype == "weekly":
            return template["day_of_week"] == target.weekday()
        if rtype == "monthly":
            last_day = calendar.monthrange(target.year, target.month)[1]
            day = min(template["day_of_month"] or 1, last_day)
            return target.day == day
        return False

    def due_templates(self, target: date) -> list[dict]:
        """Активные шаблоны, которые стреляют в target."""
        return [t for t in self.list_templates(active_only=True) if self._fires_on(t, target)]

    def ensure_day(self, target: date, tasks) -> int:
        """Лениво создаёт task-инстансы на target для активных подходящих шаблонов.

        Идемпотентно: задача помечается source='recurring' + recurring_template_id,
        повторный запуск в тот же день дубль не создаёт. Возвращает число созданных.
        """
        created = 0
        iso = target.isoformat()
        for t in self.due_templates(target):
            if tasks.recurring_exists(t["id"], iso):
                continue
            tasks.create(
                title=t["title"], due_date=iso, due_time=t["time"],
                project=t["project"], source="recurring", recurring_template_id=t["id"],
            )
            created += 1
        return created
