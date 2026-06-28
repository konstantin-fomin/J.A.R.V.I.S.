"""SQLite-хранилище платежей. Простая обёртка без ORM — как tasks.py.

Две таблицы (паттерн Recurring: шаблон → инстанс на период):
  bill_templates  — что и когда платить (создаётся редко)
  bill_instances  — конкретное начисление на месяц (авто от активных шаблонов)

Разделение уровней принципиально: «оплачено» в июне не должно остаться
«оплачено» в июле. Таблицы создаются автоматически при первом обращении.
"""
import calendar
import datetime
import sqlite3
from datetime import date
from pathlib import Path
from typing import Optional


def _due_date(year_month: str, day_of_month: int) -> str:
    """Конкретная дата начисления в этом месяце.

    День из шаблона зажимается до последнего дня месяца — иначе day_of_month=31
    в феврале дал бы несуществующую дату.
    """
    year, month = (int(p) for p in year_month.split("-"))
    last_day = calendar.monthrange(year, month)[1]
    day = min(day_of_month, last_day)
    return f"{year:04d}-{month:02d}-{day:02d}"


class BillStore:
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
                CREATE TABLE IF NOT EXISTS bill_templates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    amount REAL,
                    day_of_month INTEGER NOT NULL,
                    category TEXT,
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS bill_instances (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    template_id INTEGER NOT NULL REFERENCES bill_templates(id),
                    year_month TEXT NOT NULL,
                    due_date TEXT NOT NULL,
                    amount REAL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    paid_at TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE(template_id, year_month)
                )
                """
            )

    # --- Шаблоны -----------------------------------------------------------

    def create_template(
        self,
        name: str,
        day_of_month: int,
        amount: Optional[float] = None,
        category: Optional[str] = None,
    ) -> dict:
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO bill_templates "
                "(name, amount, day_of_month, category, active, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 1, ?, ?)",
                (name, amount, day_of_month, category, now, now),
            )
            template_id = cur.lastrowid
        return self.get_template(template_id)

    def get_template(self, template_id: int) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM bill_templates WHERE id = ?", (template_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_templates(self, active_only: bool = False) -> list[dict]:
        query = "SELECT * FROM bill_templates"
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
                f"UPDATE bill_templates SET {set_clause} WHERE id = ?",
                (*fields.values(), template_id),
            )
        return self.get_template(template_id)

    # --- Начисления --------------------------------------------------------

    def ensure_month(self, year_month: str) -> int:
        """Лениво создаёт начисления для всех активных шаблонов на этот месяц.

        Идемпотентно: UNIQUE(template_id, year_month) + INSERT OR IGNORE.
        Возвращает число реально созданных начислений.
        """
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        created = 0
        with self._connect() as conn:
            templates = conn.execute(
                "SELECT * FROM bill_templates WHERE active = 1"
            ).fetchall()
            for t in templates:
                due = _due_date(year_month, t["day_of_month"])
                cur = conn.execute(
                    "INSERT OR IGNORE INTO bill_instances "
                    "(template_id, year_month, due_date, amount, status, created_at) "
                    "VALUES (?, ?, ?, ?, 'pending', ?)",
                    (t["id"], year_month, due, t["amount"], now),
                )
                created += cur.rowcount
        return created

    _INSTANCE_SELECT = (
        "SELECT i.*, t.name AS name, t.category AS category "
        "FROM bill_instances i JOIN bill_templates t ON t.id = i.template_id"
    )

    def get_instance(self, instance_id: int) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute(
                f"{self._INSTANCE_SELECT} WHERE i.id = ?", (instance_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_instances(self, year_month: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                f"{self._INSTANCE_SELECT} WHERE i.year_month = ? "
                "ORDER BY i.due_date, i.id",
                (year_month,),
            ).fetchall()
        return [dict(r) for r in rows]

    def due_on(self, due_date: str, status: Optional[str] = None) -> list[dict]:
        """Начисления с конкретной датой — для напоминаний планировщика."""
        query = f"{self._INSTANCE_SELECT} WHERE i.due_date = ?"
        params: list = [due_date]
        if status:
            query += " AND i.status = ?"
            params.append(status)
        query += " ORDER BY i.id"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    def set_status(self, instance_id: int, status: Optional[str]) -> Optional[dict]:
        if status is None:
            return self.get_instance(instance_id)
        paid_at = datetime.datetime.now(datetime.timezone.utc).isoformat() if status == "paid" else None
        with self._connect() as conn:
            conn.execute(
                "UPDATE bill_instances SET status = ?, paid_at = ? WHERE id = ?",
                (status, paid_at, instance_id),
            )
        return self.get_instance(instance_id)


def current_month() -> str:
    return date.today().strftime("%Y-%m")
