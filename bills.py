"""SQLite-хранилище платежей. Простая обёртка без ORM — как tasks.py.

Три таблицы:
  bill_templates  — регулярный платёж, что и когда платить (паттерн Recurring:
                    шаблон → инстанс на период; создаётся редко)
  bill_instances  — конкретное начисление на месяц (авто от активных шаблонов)
  one_time_bills  — разовый платёж с конкретной датой (YYYY-MM-DD), БЕЗ шаблона.
                    Отдельная сущность намеренно (§3-bis-2): у bill_templates
                    в принципе нет способа выразить «заплатить один раз 14 июля»
                    — только «N-го числа каждый месяц» (day_of_month без года/
                    месяца). Раньше разовые платежи ошибочно заводили как
                    регулярные шаблоны — те молча плодили начисления каждый
                    месяц вместо одного платежа.

Разделение bill_templates/bill_instances принципиально: «оплачено» в июне не
должно остаться «оплачено» в июле. Таблицы создаются автоматически при первом
обращении.

Единый доступ к обеим сущностям (регулярной и разовой) — через составной id:
"r<id>" (bill_instances) / "o<id>" (one_time_bills), см. list_month/get_bill/
set_bill_status/due_on. Вызывающий код (дашборд, бот, IntentRouter) работает
только с составным id и полем kind — никогда не лезет в таблицы напрямую.
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
                    project TEXT,
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            # Миграция старых баз: добавляем project, если колонки ещё нет.
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(bill_templates)")}
            if "project" not in cols:
                conn.execute("ALTER TABLE bill_templates ADD COLUMN project TEXT")
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
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS one_time_bills (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    amount REAL,
                    due_date TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    paid_at TEXT,
                    category TEXT,
                    created_at TEXT NOT NULL
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
        project: Optional[str] = None,
    ) -> dict:
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO bill_templates "
                "(name, amount, day_of_month, category, project, active, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, 1, ?, ?)",
                (name, amount, day_of_month, category, project, now, now),
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

    def delete_template(self, template_id: int) -> None:
        """Удаляет шаблон вместе с его начислениями. Нужен для undo_last после
        bulk-создания платежей (§3-bis): откат создания шаблона = его удаление.
        Начисления сносим первыми — иначе остались бы висячие ссылки."""
        with self._connect() as conn:
            conn.execute("DELETE FROM bill_instances WHERE template_id = ?", (template_id,))
            conn.execute("DELETE FROM bill_templates WHERE id = ?", (template_id,))

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

    # --- Разовые платежи (one_time_bills, §3-bis-2) -------------------------

    def create_one_time(
        self,
        name: str,
        due_date: str,
        amount: Optional[float] = None,
        category: Optional[str] = None,
    ) -> dict:
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO one_time_bills "
                "(name, amount, due_date, status, category, created_at) "
                "VALUES (?, ?, ?, 'pending', ?, ?)",
                (name, amount, due_date, category, now),
            )
            one_time_id = cur.lastrowid
        return self.get_one_time(one_time_id)

    def get_one_time(self, one_time_id: int) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM one_time_bills WHERE id = ?", (one_time_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_one_time(self, year_month: Optional[str] = None) -> list[dict]:
        query = "SELECT * FROM one_time_bills"
        params: list = []
        if year_month:
            query += " WHERE due_date LIKE ?"
            params.append(f"{year_month}-%")
        query += " ORDER BY due_date, id"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    def set_one_time_status(self, one_time_id: int, status: Optional[str]) -> Optional[dict]:
        if status is None:
            return self.get_one_time(one_time_id)
        paid_at = datetime.datetime.now(datetime.timezone.utc).isoformat() if status == "paid" else None
        with self._connect() as conn:
            conn.execute(
                "UPDATE one_time_bills SET status = ?, paid_at = ? WHERE id = ?",
                (status, paid_at, one_time_id),
            )
        return self.get_one_time(one_time_id)

    def delete_one_time(self, one_time_id: int) -> None:
        """Удаляет разовый платёж. Нужен для undo_last после его создания."""
        with self._connect() as conn:
            conn.execute("DELETE FROM one_time_bills WHERE id = ?", (one_time_id,))

    # --- Единый доступ к регулярным + разовым платежам ----------------------
    # Составной id ("r<id>"/"o<id>") — единственное, что видит вызывающий код
    # (дашборд, бот, IntentRouter); какая это таблица внутри — их не касается.

    def list_month(self, year_month: str) -> list[dict]:
        """Единый список платежей месяца: bill_instances + one_time_bills,
        отсортированные по due_date. Дашборду/боту без разницы, откуда запись."""
        regular = [
            {**b, "id": f"r{b['id']}", "kind": "regular"}
            for b in self.list_instances(year_month)
        ]
        one_time = [
            {**b, "id": f"o{b['id']}", "kind": "one_time"}
            for b in self.list_one_time(year_month)
        ]
        combined = regular + one_time
        combined.sort(key=lambda b: (b["due_date"], b["id"]))
        return combined

    def get_bill(self, bill_id: str) -> Optional[dict]:
        """Универсальный geter по составному id — сама решает, в какую таблицу
        смотреть по префиксу."""
        kind, raw_id = _split_bill_id(bill_id)
        row = self.get_instance(raw_id) if kind == "regular" else self.get_one_time(raw_id)
        if row is None:
            return None
        return {**row, "id": bill_id, "kind": kind}

    def set_bill_status(self, bill_id: str, status: Optional[str]) -> Optional[dict]:
        """Универсальный сеттер статуса по составному id — сама решает, в какую
        таблицу писать. mark_bill_paid/undo используют только этот метод."""
        kind, raw_id = _split_bill_id(bill_id)
        row = (
            self.set_status(raw_id, status) if kind == "regular"
            else self.set_one_time_status(raw_id, status)
        )
        if row is None:
            return None
        return {**row, "id": bill_id, "kind": kind}

    def due_on(self, due_date: str, status: Optional[str] = None) -> list[dict]:
        """Платежи (регулярные + разовые) с конкретной датой — для напоминаний
        планировщика и /today. Единый формат — см. list_month."""
        query = f"{self._INSTANCE_SELECT} WHERE i.due_date = ?"
        one_time_query = "SELECT * FROM one_time_bills WHERE due_date = ?"
        params: list = [due_date]
        if status:
            query += " AND i.status = ?"
            one_time_query += " AND status = ?"
            params.append(status)
        with self._connect() as conn:
            regular_rows = conn.execute(f"{query} ORDER BY i.id", params).fetchall()
            one_time_rows = conn.execute(f"{one_time_query} ORDER BY id", params).fetchall()
        regular = [{**dict(r), "id": f"r{r['id']}", "kind": "regular"} for r in regular_rows]
        one_time = [{**dict(r), "id": f"o{r['id']}", "kind": "one_time"} for r in one_time_rows]
        return regular + one_time


def _split_bill_id(bill_id: str) -> tuple[str, int]:
    """Составной id ("r12"/"o7") → (kind, raw_id). Единственное место, где
    парсится префикс — внешний код (бот/REST/intents) через него не лезет."""
    if not bill_id or bill_id[0] not in ("r", "o"):
        raise ValueError(f"Некорректный id платежа: {bill_id!r}")
    kind = "regular" if bill_id[0] == "r" else "one_time"
    try:
        raw_id = int(bill_id[1:])
    except ValueError:
        raise ValueError(f"Некорректный id платежа: {bill_id!r}") from None
    return kind, raw_id


def current_month() -> str:
    return date.today().strftime("%Y-%m")
