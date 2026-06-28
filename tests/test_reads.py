"""Тесты read-it-later (§15): хранилище, извлечение текста, intents и undo.

ReadStore — самостоятельное SQLite-хранилище (стиль tasks.py). extract_article —
ЧИСТАЯ функция (парсинг готового HTML, без сети): тестируем на статичной разметке.
Реальное скачивание (fetch_article/enrich_link) сеть не дёргаем в юнит-тестах —
оно проверяется отдельным live-прогоном. Интеграция intent→действие/undo — через
реальный IntentRouter с ActionLog; саммари в save_link приходит готовым в params
(как его принёс бы хендлер), поэтому LLM тоже не нужен.
"""
import pytest

from intents import IntentRouter
from logger import ActionLog
from reads import ReadStore, extract_article


# --- ReadStore как хранилище -------------------------------------------------

def test_create_unread_and_list_by_status(tmp_path):
    s = ReadStore(tmp_path / "r.db")
    s.create(url="https://e.com/a", title="A", summary="про A")
    assert len(s.list("unread")) == 1
    assert s.list("read") == []


def test_mark_read_moves_status(tmp_path):
    s = ReadStore(tmp_path / "r.db")
    r = s.create(url="https://e.com/a", title="A")
    s.mark_read(r["id"])
    assert s.list("unread") == []
    assert len(s.list("read")) == 1
    assert s.get(r["id"])["status"] == "read"


def test_list_all_and_delete(tmp_path):
    s = ReadStore(tmp_path / "r.db")
    a = s.create(url="https://e.com/1")
    s.create(url="https://e.com/2")
    assert len(s.list()) == 2
    assert s.delete(a["id"]) is True
    assert len(s.list()) == 1


# --- extract_article (без сети, на реальной разметке) ------------------------

SAMPLE_HTML = """<html><head>
<title>Заголовок из title</title>
<meta property="og:title" content="OG Заголовок">
<meta property="og:description" content="Краткое описание из og.">
</head><body>
<p>Первый абзац текста статьи про интересное.</p>
<p>Второй абзац с деталями.</p>
</body></html>"""


def test_extract_prefers_og_title_and_collects_text():
    title, text = extract_article(SAMPLE_HTML, "https://e.com")
    assert title == "OG Заголовок"
    assert "Краткое описание из og." in text
    assert "Первый абзац" in text


def test_extract_falls_back_to_title_tag():
    html = "<html><head><title>Только title</title></head><body><p>тело</p></body></html>"
    title, text = extract_article(html)
    assert title == "Только title"
    assert "тело" in text


def test_extract_empty_when_no_content():
    title, text = extract_article("<html></html>")
    assert title is None
    assert text == ""


# --- интеграция через IntentRouter ------------------------------------------

@pytest.fixture
def router(tmp_path):
    reads = ReadStore(tmp_path / "r.db")
    alog = ActionLog(tmp_path / "a.db")
    r = IntentRouter(None, None, calendar=None, action_log=alog, reads=reads)
    return r, reads, alog


def test_save_link_creates_and_logs(router):
    r, reads, alog = router
    res = r.resolve({"intent": "save_link", "confidence": "high", "url": "https://e.com/x"})
    assert res.kind == "execute"
    # хендлер обогащает params саммари до execute — эмулируем это
    res.action["params"].update(title="Статья X", summary="Два предложения.")
    reply = r.execute(res.action)
    assert "Статья X" in reply
    items = reads.list("unread")
    assert len(items) == 1 and items[0]["summary"] == "Два предложения."
    rec = alog.latest_active()
    assert rec["entity_type"] == "read" and rec["action"] == "create"


def test_query_reads_lists_only_unread(router):
    r, reads, _ = router
    reads.create(url="https://e.com/a", title="Альфа", summary="с1")
    reads.create(url="https://e.com/b", title="Бета", summary="с2", status="read")
    reply = r.execute(r.resolve({"intent": "query_reads", "confidence": "high"}).action)
    assert "Альфа" in reply and "Бета" not in reply


def test_mark_read_via_intent(router):
    r, reads, _ = router
    reads.create(url="https://e.com/a", title="Длинный заголовок про Rust")
    res = r.resolve({"intent": "mark_read", "confidence": "high", "title_hint": "rust"})
    assert res.kind == "execute"
    r.execute(res.action)
    assert reads.list("unread") == []


def test_mark_read_not_found_is_message(router):
    r, *_ = router
    res = r.resolve({"intent": "mark_read", "confidence": "high", "title_hint": "нет такого"})
    assert res.kind == "message"


def test_save_link_then_undo_removes(router):
    r, reads, _ = router
    res = r.resolve({"intent": "save_link", "confidence": "high", "url": "https://e.com/x"})
    res.action["params"].update(title="X", summary="s")
    r.execute(res.action)
    assert len(reads.list()) == 1
    r.execute(r.resolve({"intent": "undo_last"}).action)
    assert reads.list() == []


def test_mark_read_then_undo_restores_unread(router):
    r, reads, _ = router
    c = reads.create(url="https://e.com/a", title="A")
    r.execute(r.resolve({"intent": "mark_read", "confidence": "high", "title_hint": "a"}).action)
    assert reads.get(c["id"])["status"] == "read"
    r.execute(r.resolve({"intent": "undo_last"}).action)
    assert reads.get(c["id"])["status"] == "unread"
