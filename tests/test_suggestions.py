"""Тесты проактивных подсказок из заметок (§13).

Кластеризация journal-чанков, окно 14 дней, дедуп через suggestion_log и путь
«Да → задача создана и залогирована» — всё оффлайн: эмбеддинги задаём руками,
label_fn инъектируется (детерминированно), сеть/Telegram/Gemini не дёргаем.
"""
import datetime
from datetime import date

import pytest

from logger import ActionLog
from intents import IntentRouter
from suggestions import (
    ProactiveSuggester,
    SuggestionLog,
    build_suggestion_text,
    cluster_chunks,
    cosine_distance,
    journal_date,
    propose_label,
    theme_hash,
)
from tasks import TaskStore

TODAY = date(2026, 6, 28)


def chunk(text, file, embedding):
    return {"text": text, "file": file, "embedding": embedding}


def jfile(d: date) -> str:
    return f"journal/{d:%Y-%m-%d}.md"


class FakeIndex:
    """Отдаёт заранее заданные journal-чанки с эмбеддингами (без ChromaDB)."""
    def __init__(self, chunks):
        self._chunks = chunks

    def journal_chunks(self):
        return list(self._chunks)


# --- cosine_distance ---------------------------------------------------------

def test_cosine_distance_identical_is_zero():
    assert cosine_distance([1.0, 0.0, 0.0], [1.0, 0.0, 0.0]) == pytest.approx(0.0)


def test_cosine_distance_orthogonal_is_one():
    assert cosine_distance([1.0, 0.0], [0.0, 1.0]) == pytest.approx(1.0)


def test_cosine_distance_zero_vector_is_far():
    # вырожденный вектор не должен ронять расчёт делением на ноль
    assert cosine_distance([0.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)


# --- journal_date ------------------------------------------------------------

def test_journal_date_parses_filename():
    assert journal_date("journal/2026-06-20.md") == date(2026, 6, 20)


def test_journal_date_none_for_non_journal():
    assert journal_date("topics/work.md") is None


# --- theme_hash --------------------------------------------------------------

def test_theme_hash_stable_under_case_and_spacing():
    assert theme_hash("Ремонт  ванной") == theme_hash("ремонт ванной")


def test_theme_hash_differs_per_theme():
    assert theme_hash("ремонт ванной") != theme_hash("отпуск в июле")


# --- cluster_chunks ----------------------------------------------------------

def test_cluster_found_when_three_similar():
    chunks = [
        chunk("a", "journal/2026-06-20.md", [1.0, 0.0, 0.0]),
        chunk("b", "journal/2026-06-22.md", [0.95, 0.05, 0.0]),
        chunk("c", "journal/2026-06-25.md", [0.9, 0.1, 0.0]),
        chunk("z", "journal/2026-06-26.md", [0.0, 0.0, 1.0]),  # про другое
    ]
    clusters = cluster_chunks(chunks, max_distance=0.35, min_size=3)
    assert len(clusters) == 1
    assert {c["text"] for c in clusters[0]} == {"a", "b", "c"}


def test_no_cluster_when_below_min_size():
    chunks = [
        chunk("a", "journal/2026-06-20.md", [1.0, 0.0, 0.0]),
        chunk("b", "journal/2026-06-22.md", [0.95, 0.05, 0.0]),  # только 2 похожих
        chunk("z", "journal/2026-06-26.md", [0.0, 0.0, 1.0]),
    ]
    assert cluster_chunks(chunks, max_distance=0.35, min_size=3) == []


def test_three_in_one_day_still_clusters():
    # порог — 3 разные записи, а не 3 разных дня: интенсивное обсуждение за день
    # должно ловиться (см. §13).
    chunks = [
        chunk("a", "journal/2026-06-26.md", [1.0, 0.0, 0.0]),
        chunk("b", "journal/2026-06-26.md", [0.97, 0.03, 0.0]),
        chunk("c", "journal/2026-06-26.md", [0.92, 0.08, 0.0]),
    ]
    clusters = cluster_chunks(chunks, max_distance=0.35, min_size=3)
    assert len(clusters) == 1


# --- build_suggestion_text ---------------------------------------------------

def test_build_suggestion_text():
    assert build_suggestion_text("ремонт ванной") == (
        "Заметил, что несколько раз упоминал «ремонт ванной» — превратить в задачу?"
    )


# --- ProactiveSuggester.find_suggestions -------------------------------------

def suggester(index, log, label="ремонт ванной"):
    return ProactiveSuggester(
        index, label_fn=lambda texts: label, log=log,
        window_days=14, max_distance=0.35, min_cluster=3, repeat_block_days=7,
    )


def test_suggestion_returned_for_cluster(tmp_path):
    log = SuggestionLog(tmp_path / "s.db")
    index = FakeIndex([
        chunk("a", jfile(date(2026, 6, 20)), [1.0, 0.0, 0.0]),
        chunk("b", jfile(date(2026, 6, 24)), [0.95, 0.05, 0.0]),
        chunk("c", jfile(date(2026, 6, 27)), [0.9, 0.1, 0.0]),
    ])
    out = suggester(index, log).find_suggestions(today=TODAY)
    assert len(out) == 1
    assert out[0]["label"] == "ремонт ванной"
    assert out[0]["hash"] == theme_hash("ремонт ванной")


def test_skip_when_recently_suggested(tmp_path):
    log = SuggestionLog(tmp_path / "s.db")
    log.mark_suggested(theme_hash("ремонт ванной"), "ремонт ванной",
                       when=date(2026, 6, 25))  # 3 дня назад < 7
    index = FakeIndex([
        chunk("a", jfile(date(2026, 6, 20)), [1.0, 0.0, 0.0]),
        chunk("b", jfile(date(2026, 6, 24)), [0.95, 0.05, 0.0]),
        chunk("c", jfile(date(2026, 6, 27)), [0.9, 0.1, 0.0]),
    ])
    assert suggester(index, log).find_suggestions(today=TODAY) == []


def test_suggest_again_after_block_window(tmp_path):
    log = SuggestionLog(tmp_path / "s.db")
    log.mark_suggested(theme_hash("ремонт ванной"), "ремонт ванной",
                       when=date(2026, 6, 10))  # 18 дней назад > 7
    index = FakeIndex([
        chunk("a", jfile(date(2026, 6, 20)), [1.0, 0.0, 0.0]),
        chunk("b", jfile(date(2026, 6, 24)), [0.95, 0.05, 0.0]),
        chunk("c", jfile(date(2026, 6, 27)), [0.9, 0.1, 0.0]),
    ])
    assert len(suggester(index, log).find_suggestions(today=TODAY)) == 1


def test_no_cluster_no_suggestion(tmp_path):
    log = SuggestionLog(tmp_path / "s.db")
    index = FakeIndex([
        chunk("a", jfile(date(2026, 6, 20)), [1.0, 0.0, 0.0]),
        chunk("z", jfile(date(2026, 6, 24)), [0.0, 1.0, 0.0]),  # про другое
    ])
    assert suggester(index, log).find_suggestions(today=TODAY) == []


def test_old_entries_outside_window_excluded(tmp_path):
    log = SuggestionLog(tmp_path / "s.db")
    index = FakeIndex([
        chunk("a", jfile(date(2026, 6, 27)), [1.0, 0.0, 0.0]),
        chunk("b", jfile(date(2026, 6, 26)), [0.95, 0.05, 0.0]),
        chunk("c", jfile(date(2026, 6, 1)), [0.9, 0.1, 0.0]),  # >14 дней назад
    ])
    # за окном осталось только 2 похожих → кластера нет
    assert suggester(index, log).find_suggestions(today=TODAY) == []


def test_topic_files_ignored(tmp_path):
    # topic/fact-файлы без дат в окно не попадают (у FakeIndex их и не отдаём,
    # но journal_chunks в проде уже отфильтрует — проверяем устойчивость к мусору)
    log = SuggestionLog(tmp_path / "s.db")
    index = FakeIndex([
        chunk("a", jfile(date(2026, 6, 27)), [1.0, 0.0, 0.0]),
        chunk("b", jfile(date(2026, 6, 26)), [0.95, 0.05, 0.0]),
        chunk("x", "topics/work.md", [0.93, 0.07, 0.0]),  # без даты
    ])
    assert suggester(index, log).find_suggestions(today=TODAY) == []


# --- SuggestionLog.label_for: восстановление темы после рестарта --------------

def test_label_for_returns_latest_label(tmp_path):
    # Кнопка «Да» приходит позже показа: лейбл темы достаём из лога (а не из
    # памяти процесса), чтобы рестарт бота не терял текст подсказки.
    log = SuggestionLog(tmp_path / "s.db")
    h = theme_hash("ремонт ванной")
    log.mark_suggested(h, "ремонт ванной", when=date(2026, 6, 20))
    log.mark_suggested(h, "ремонт ванной комнаты", when=date(2026, 6, 27))
    assert log.label_for(h) == "ремонт ванной комнаты"  # самый свежий


def test_label_for_none_when_unknown(tmp_path):
    log = SuggestionLog(tmp_path / "s.db")
    assert log.label_for("deadbeef") is None


# --- propose_label: формулировка темы через LLM ------------------------------

class FakeLLM:
    def __init__(self, reply):
        self.reply = reply
        self.seen = None

    def chat(self, messages):
        self.seen = messages
        return self.reply


def test_propose_label_strips_quotes_and_whitespace():
    llm = FakeLLM("  «ремонт ванной»\n")
    assert propose_label(llm, ["плитку выбрали", "сантехника"]) == "ремонт ванной"


def test_propose_label_passes_texts_to_llm():
    llm = FakeLLM("ремонт")
    propose_label(llm, ["обсуждали смету", "нашёл бригаду"])
    prompt = llm.seen[0]["content"]
    assert "обсуждали смету" in prompt and "нашёл бригаду" in prompt


# --- путь «Да»: задача создана и залогирована --------------------------------

def test_yes_creates_task_and_logs(tmp_path):
    tasks = TaskStore(tmp_path / "t.db")
    log = ActionLog(tmp_path / "a.db")
    router = IntentRouter(tasks, None, None, log, None)

    reply = router.execute({
        "type": "create_task",
        "params": {"title": "ремонт ванной", "source": "suggestion"},
        "source": "suggestion",
    })

    assert "ремонт ванной" in reply
    created = tasks.list()
    assert len(created) == 1
    assert created[0]["title"] == "ремонт ванной"
    assert created[0]["source"] == "suggestion"

    rec = log.latest_active()
    assert rec is not None
    assert rec["entity_type"] == "task"
    assert rec["action"] == "create"
    assert rec["source"] == "suggestion"
