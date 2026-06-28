"""Read-it-later (§15): SQLite-хранилище ссылок + скачивание/извлечение/саммари.

Хранилище — в стиле tasks.py (без ORM). Саммари страницы считается ОДИН раз при
сохранении (Gemini) и кладётся в БД; дайджест потом просто читает готовый summary.

extract_article — чистая функция (парсинг готового HTML), отделена от сети, чтобы
её можно было юнит-тестить без запросов. fetch_article/enrich_link ходят в сеть.
"""
from __future__ import annotations

import datetime
import logging
import sqlite3
from pathlib import Path
from typing import Optional

import httpx
from bs4 import BeautifulSoup, Tag

logger = logging.getLogger(__name__)

# Текста для саммари хватает заголовка + описания + начала статьи: полный
# readability-парсинг не делаем (см. §15).
EXTRACT_MAX_CHARS = 3000
FETCH_TIMEOUT = 15.0
_USER_AGENT = (
    "Mozilla/5.0 (compatible; JarvisBot/1.0; +https://github.com/konstantin-fomin/J.A.R.V.I.S.)"
)


class ReadStore:
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
                CREATE TABLE IF NOT EXISTS reads (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    url TEXT NOT NULL,
                    title TEXT,
                    summary TEXT,
                    status TEXT NOT NULL DEFAULT 'unread',
                    created_at TEXT NOT NULL
                )
                """
            )

    def create(self, url: str, title: Optional[str] = None,
               summary: Optional[str] = None, status: str = "unread") -> dict:
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO reads (url, title, summary, status, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (url, title, summary, status, now),
            )
            read_id = cur.lastrowid
        assert read_id is not None  # свежий INSERT всегда даёт rowid
        created = self.get(read_id)
        assert created is not None
        return created

    def get(self, read_id: int) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM reads WHERE id = ?", (read_id,)).fetchone()
        return dict(row) if row else None

    def list(self, status: Optional[str] = None) -> list[dict]:
        query = "SELECT * FROM reads"
        params: list = []
        if status:
            query += " WHERE status = ?"
            params.append(status)
        query += " ORDER BY id DESC"  # свежие сверху
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    def set_status(self, read_id: int, status: str) -> Optional[dict]:
        with self._connect() as conn:
            conn.execute("UPDATE reads SET status = ? WHERE id = ?", (status, read_id))
        return self.get(read_id)

    def mark_read(self, read_id: int) -> Optional[dict]:
        return self.set_status(read_id, "read")

    def delete(self, read_id: int) -> bool:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM reads WHERE id = ?", (read_id,))
        return cur.rowcount > 0


# --- скачивание и извлечение -------------------------------------------------

def _meta(soup: BeautifulSoup, *, prop: str = "", name: str = "") -> Optional[str]:
    """content из <meta property=...> или <meta name=...>, если задан и непустой."""
    attrs = {"property": prop} if prop else {"name": name}
    tag = soup.find("meta", attrs=attrs)
    if isinstance(tag, Tag):
        content = tag.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
    return None


def extract_article(html: str, url: str = "") -> tuple[Optional[str], str]:
    """HTML → (заголовок, текст-для-саммари). Чистая функция, без сети.

    Заголовок: og:title → <title>. Текст: og:description/meta description + текст
    первых <p> (обрезаем до EXTRACT_MAX_CHARS)."""
    soup = BeautifulSoup(html, "html.parser")

    title = _meta(soup, prop="og:title")
    if not title and isinstance(soup.title, Tag):
        title = soup.title.get_text(strip=True) or None

    desc = _meta(soup, prop="og:description") or _meta(soup, name="description") or ""
    paragraphs = " ".join(p.get_text(" ", strip=True) for p in soup.find_all("p"))
    text = "\n".join(part for part in (desc, paragraphs) if part).strip()
    return title, text[:EXTRACT_MAX_CHARS]


def fetch_article(url: str, client: Optional[httpx.Client] = None) -> str:
    """Скачивает страницу и возвращает HTML. Ходит в сеть (редиректы, таймаут, UA)."""
    headers = {"User-Agent": _USER_AGENT}
    if client is not None:
        resp = client.get(url, headers=headers, follow_redirects=True, timeout=FETCH_TIMEOUT)
    else:
        resp = httpx.get(url, headers=headers, follow_redirects=True, timeout=FETCH_TIMEOUT)
    resp.raise_for_status()
    return resp.text


_SUMMARY_PROMPT = """Сделай очень короткое саммари веб-страницы для списка «почитать
позже»: 2–3 предложения по-русски, о чём это и зачем читать. Без вступлений и
markdown — только сам текст саммари.

Заголовок: {title}

Текст страницы:
{text}"""


def summarize_article(llm, title: Optional[str], text: str) -> str:
    """Один вызов LLM → саммари 2–3 предложения. llm — объект с .chat(messages)."""
    prompt = _SUMMARY_PROMPT.format(title=title or "(без заголовка)", text=text)
    return llm.chat([{"role": "user", "content": prompt}]).strip()


def enrich_link(llm, url: str) -> dict:
    """fetch → extract → summarize. Возвращает {url, title, summary}.

    Сеть/парсинг/LLM обёрнуты в try/except: ссылку не теряем даже если страница
    не открылась или саммари не получилось — кладём заглушку в summary."""
    try:
        html = fetch_article(url)
        title, text = extract_article(html, url)
    except Exception:
        logger.exception("read-it-later: не удалось скачать %s", url)
        return {"url": url, "title": None, "summary": "(не удалось получить превью)"}
    if not text:
        return {"url": url, "title": title, "summary": "(не удалось получить превью)"}
    try:
        summary = summarize_article(llm, title, text)
    except Exception:
        logger.exception("read-it-later: не удалось сделать саммари %s", url)
        summary = "(не удалось сделать саммари)"
    return {"url": url, "title": title, "summary": summary}
