"""Проактивные подсказки из заметок (§13).

Идея: если одна тема всплывает в журнале несколько раз за короткое окно —
предложить превратить её в задачу. Работа в три шага:

1. cluster_chunks — кластеризуем journal-чанки по близости эмбеддингов
   (single-link: дистанция ≤ max_distance связывает чанки в одну группу).
2. ProactiveSuggester.find_suggestions — оставляем чанки в окне window_days,
   ищем кластеры ≥ min_cluster, для каждого зовём label_fn (LLM формулирует
   тему) и отсеиваем недавно предложенные через SuggestionLog (дедуп).
3. Путь «Да» создаёт задачу обычным IntentRouter.execute(create_task) — этот
   модуль про обнаружение, не про создание.

Всё детерминированно и оффлайн-тестируемо: эмбеддинги приходят из индекса,
label_fn инъектируется. Сеть/Telegram/Gemini дёргает уже вызывающий код.
"""
import hashlib
import math
import re
import sqlite3
from datetime import date, timedelta
from pathlib import Path
from typing import Callable, Optional

# journal/ГГГГ-ММ-ДД.md — дату берём только из journal-файлов; topic/fact-файлы
# (без даты в имени) в окно не попадают.
_JOURNAL_RE = re.compile(r"journal/(\d{4}-\d{2}-\d{2})\.md$")


def cosine_distance(a: list[float], b: list[float]) -> float:
    """Косинусная дистанция (1 - сходство): 0 — идентичны, 1 — ортогональны.

    Вырожденный (нулевой) вектор считаем максимально далёким, а не роняем
    расчёт делением на ноль."""
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 1.0
    return 1.0 - dot / (na * nb)


def journal_date(file: str) -> Optional[date]:
    """Дата из имени journal-файла или None, если это не journal-запись."""
    m = _JOURNAL_RE.search(file)
    if m is None:
        return None
    try:
        return date.fromisoformat(m.group(1))
    except ValueError:
        return None


def theme_hash(label: str) -> str:
    """Стабильный идентификатор темы для дедупа: регистр и пробелы не значимы."""
    norm = " ".join(label.lower().split())
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()


def cluster_chunks(chunks: list[dict], max_distance: float, min_size: int) -> list[list[dict]]:
    """Группирует чанки по близости эмбеддингов (single-link агломерация).

    Чанки i и j связаны, если cosine_distance ≤ max_distance; связность
    транзитивна (объединяем компоненты). Возвращает только группы размером
    ≥ min_size — порог считается по числу записей, не по числу дней (см. §13)."""
    n = len(chunks)
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]  # path-halving
            x = parent[x]
        return x

    for i in range(n):
        for j in range(i + 1, n):
            if cosine_distance(chunks[i]["embedding"], chunks[j]["embedding"]) <= max_distance:
                parent[find(i)] = find(j)

    groups: dict[int, list[dict]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(chunks[i])
    return [g for g in groups.values() if len(g) >= min_size]


def build_suggestion_text(label: str) -> str:
    """Текст подсказки для пользователя."""
    return f"Заметил, что несколько раз упоминал «{label}» — превратить в задачу?"


_LABEL_PROMPT = """Ниже — несколько записей из дневника об одной повторяющейся теме.
Сформулируй эту тему как короткое название задачи: 2–4 слова, в именительном
падеже, без кавычек и пояснений. Верни ТОЛЬКО саму формулировку.

Записи:
{notes}"""


def propose_label(llm, texts: list[str]) -> str:
    """Просит LLM назвать тему кластера короткой формулировкой задачи.

    llm — любой объект с .chat(messages) → str (как LLMClient). Кавычки и лишние
    пробелы из ответа модели срезаем."""
    notes = "\n".join(f"- {t}" for t in texts)
    raw = llm.chat([{"role": "user", "content": _LABEL_PROMPT.format(notes=notes)}])
    return raw.strip().strip("«»\"'").strip()


class SuggestionLog:
    """Журнал показанных подсказок — чтобы не предлагать одно и то же часто.

    Простая SQLite-обёртка без ORM, как tasks.py/bills.py/logger.py."""

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
                CREATE TABLE IF NOT EXISTS suggestion_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    theme_hash TEXT NOT NULL,
                    label TEXT NOT NULL,
                    suggested_at TEXT NOT NULL
                )
                """
            )

    def mark_suggested(self, theme_hash: str, label: str, when: Optional[date] = None) -> None:
        """Фиксирует факт показа подсказки по теме (для дедупа)."""
        when = when or date.today()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO suggestion_log (theme_hash, label, suggested_at) VALUES (?, ?, ?)",
                (theme_hash, label, when.isoformat()),
            )

    def last_suggested(self, theme_hash: str) -> Optional[date]:
        """Дата самого свежего показа темы или None, если ещё не предлагали."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT MAX(suggested_at) AS last FROM suggestion_log WHERE theme_hash = ?",
                (theme_hash,),
            ).fetchone()
        if row is None or row["last"] is None:
            return None
        return date.fromisoformat(row["last"])

    def label_for(self, theme_hash: str) -> Optional[str]:
        """Текст темы из самого свежего показа (или None).

        Нужен для кнопки «Да»: она приходит асинхронно и после рестарта бота,
        поэтому формулировку темы достаём из лога, а не из памяти процесса."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT label FROM suggestion_log WHERE theme_hash = ? "
                "ORDER BY id DESC LIMIT 1",
                (theme_hash,),
            ).fetchone()
        return row["label"] if row is not None else None


class ProactiveSuggester:
    """Находит темы-кандидаты на задачу по journal-чанкам из индекса.

    index      — объект с методом journal_chunks() → [{text, file, embedding}].
    label_fn   — формулирует тему из текстов кластера (в проде — LLM).
    log        — SuggestionLog для дедупа.
    """

    def __init__(
        self,
        index,
        label_fn: Callable[[list[str]], str],
        log: SuggestionLog,
        window_days: int = 14,
        max_distance: float = 0.28,
        min_cluster: int = 3,
        repeat_block_days: int = 7,
    ):
        self.index = index
        self.label_fn = label_fn
        self.log = log
        self.window_days = window_days
        self.max_distance = max_distance
        self.min_cluster = min_cluster
        self.repeat_block_days = repeat_block_days

    def find_suggestions(self, today: Optional[date] = None) -> list[dict]:
        """Список подсказок [{label, hash, chunks}] для тем в окне, не предложенных
        недавно. Пустой список — если кластеров нет либо все на блокировке."""
        today = today or date.today()
        cutoff = today - timedelta(days=self.window_days)

        in_window: list[dict] = []
        for c in self.index.journal_chunks():
            d = journal_date(c["file"])
            if d is not None and cutoff <= d <= today:
                in_window.append(c)

        out: list[dict] = []
        for cluster in cluster_chunks(in_window, self.max_distance, self.min_cluster):
            label = self.label_fn([c["text"] for c in cluster])
            h = theme_hash(label)
            last = self.log.last_suggested(h)
            if last is not None and (today - last).days < self.repeat_block_days:
                continue  # предлагали недавно — молчим
            out.append({"label": label, "hash": h, "chunks": cluster})
        return out
