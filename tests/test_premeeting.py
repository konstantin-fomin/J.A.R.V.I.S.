"""Тесты pre-meeting context bundle: релевантные заметки в напоминании о встрече.

MemoryManager.relevant_notes тестируется с подменённым индексом (FakeIndex с
заранее заданными дистанциями), а build_reminder_text — с фейковой памятью.
Сеть/эмбеддинги/Telegram не дёргаем.
"""
import datetime
from zoneinfo import ZoneInfo

import config
from bot.telegram_bot import build_reminder_text
from memory.manager import MemoryManager

TZ = ZoneInfo("Europe/Moscow")


def dt(hour, minute=0):
    return datetime.datetime(2026, 6, 28, hour, minute, tzinfo=TZ)


def event(title, start, description=""):
    return {"id": "e1", "title": title, "start": start, "end": start, "description": description}


class FakeIndex:
    """Возвращает заранее заданные (текст, файл, дистанция) — без эмбеддингов."""
    def __init__(self, scored):
        self._scored = scored

    def search_scored(self, query, n_results):
        return self._scored[:n_results]


def mm(scored):
    return MemoryManager(vault=None, index=FakeIndex(scored), max_results=5)  # type: ignore[arg-type]


# --- MemoryManager.relevant_notes: фильтр по порогу дистанции -----------------

def test_relevant_notes_keeps_only_within_threshold():
    m = mm([("Петров любит чёрный чай", "topics/work.md", 0.2),
            ("рецепт борща", "topics/food.md", 0.9)])
    assert m.relevant_notes("Петров", k=3, max_distance=0.6) == [
        ("Петров любит чёрный чай", "topics/work.md")
    ]


def test_relevant_notes_empty_when_all_too_far():
    m = mm([("совсем не про то", "journal/x.md", 0.95)])
    assert m.relevant_notes("Петров", k=3, max_distance=0.6) == []


def test_relevant_notes_caps_at_k():
    scored = [("a", "f.md", 0.1), ("b", "f.md", 0.1), ("c", "f.md", 0.1), ("d", "f.md", 0.1)]
    assert len(mm(scored).relevant_notes("q", k=3, max_distance=0.6)) == 3


# --- дефолтный порог pre-meeting строже общего chat-поиска ---------------------

def test_default_premeeting_threshold_cuts_smalltalk_keeps_contact():
    """Дефолтный порог pre-meeting отсекает small-talk-шум, но держит тематическую
    заметку-контакт. Дистанции — реально замеренные на Gemini-эмбеддингах:
    заметка про Петра против «Встреча с Петром» — 0.28, а болтовня «как дела» и
    факты дом/машина против любого запроса оседают на шумовом полу ~0.39–0.40.
    При прежних 0.6 шум проходил; строгий порог (0.32) его режет."""
    scored = [
        ("## Факты\n- Пётр — коллега по работе, ведёт проект миграции",
         "topics/petr.md", 0.2823),
        ("## Факты\n- Пользователь имеет дом.\n- Пользователь имеет машину.",
         "topics/property.md", 0.3892),
        ("- [я] Привет. Как твои дела\n- [бот] Привет! Мои дела отлично",
         "journal/2026-06-13.md", 0.4009),
    ]
    notes = mm(scored).relevant_notes(
        "Встреча с Петром", k=3, max_distance=config.MEMORY_RELEVANCE_MAX_DISTANCE)
    assert [file for _text, file in notes] == ["topics/petr.md"]


# --- build_reminder_text: есть релевантная заметка ----------------------------

def test_reminder_includes_notes_section_when_relevant():
    m = mm([("Обсуждали оффер: Пётр ждёт ответ по зарплате", "topics/work.md", 0.2)])
    text = build_reminder_text(event("Встреча с Петром", dt(10)), 15, m,
                               notes_count=3, max_distance=0.6)
    assert "🔔 Через 15 мин встреча: «Встреча с Петром» в 10:00" in text
    assert "📝 Из твоих заметок:" in text
    assert "оффер" in text


# --- build_reminder_text: нет релевантного — секции нет -----------------------

def test_reminder_no_section_when_nothing_relevant():
    m = mm([("рецепт борща", "topics/food.md", 0.92)])
    text = build_reminder_text(event("Встреча с Петром", dt(10)), 15, m,
                               notes_count=3, max_distance=0.6)
    assert "Из твоих заметок" not in text
    assert text == "🔔 Через 15 мин встреча: «Встреча с Петром» в 10:00"


def test_reminder_without_memory_is_plain():
    text = build_reminder_text(event("Дантист", dt(9)), 5, None)
    assert "Из твоих заметок" not in text
    assert text == "🔔 Через 5 мин встреча: «Дантист» в 09:00"


# --- запрос в память строится из названия + описания --------------------------

class RecordingMemory:
    def __init__(self):
        self.query = None

    def relevant_notes(self, query, k, max_distance):
        self.query = query
        return []


def test_query_combines_title_and_description():
    rec = RecordingMemory()
    build_reminder_text(event("Созвон", dt(10), description="по проекту лендинг"),
                        10, rec, notes_count=3, max_distance=0.6)
    assert "Созвон" in rec.query and "лендинг" in rec.query


# --- ведущий markdown-заголовок чанка не показываем в сниппете ----------------

def test_snippet_strips_leading_markdown_header():
    # Содержательные чанки хранятся вместе со своим заголовком секции («## Заметки»)
    # для контекста эмбеддинга — но в сниппете напоминания заголовок не нужен,
    # показываем сам текст заметки (см. баг §12: «Из твоих заметок: ## Заметки»).
    m = mm([("## Заметки\n- Пётр ждёт ответ по офферу", "topics/work.md", 0.2)])
    text = build_reminder_text(event("Встреча с Петром", dt(10)), 15, m,
                               notes_count=3, max_distance=0.6)
    bullet = next(line for line in text.splitlines() if line.startswith("• "))
    assert "#" not in bullet                 # заголовок секции выкинут
    assert "Пётр ждёт ответ по офферу" in bullet


# --- длинные заметки сокращаются ---------------------------------------------

def test_long_note_is_truncated_to_one_short_bullet():
    long = ("много текста " * 50).strip()
    m = mm([(long, "topics/big.md", 0.1)])
    text = build_reminder_text(event("X", dt(10)), 5, m, notes_count=3, max_distance=0.6)
    bullet = next(line for line in text.splitlines() if line.startswith("• "))
    assert len(bullet) <= 140
    assert bullet.endswith("…")
