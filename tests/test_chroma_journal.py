"""Тест ChromaIndex.journal_chunks() против НАСТОЯЩЕГО индекса (не FakeIndex).

Сам индекс — реальный ChromaDB в tmp-каталоге; эмбеддинги задаёт детерминированная
заглушка (в проде они так же инъектируются снаружи как Callable). Проверяем, что
метод отдаёт только journal-чанки с эмбеддингами, а topic/fact-файлы отсекает —
именно эти данные потребляет ProactiveSuggester (§13).

Плюс интеграционный тест дефолтного порога кластеризации на геометрии, снятой с
РЕАЛЬНЫХ Gemini-эмбеддингов живого журнала: связанные чанки (про яйца) лежат
≤0.26, а несвязанная запись (про AI-модель) — в 0.33 от ближайшего. При старом
пороге 0.35 она ошибочно сливалась в кластер, при 0.28 — отсекается.
"""
import hashlib
from datetime import date

from memory.chroma import ChromaIndex
from suggestions import ProactiveSuggester, SuggestionLog, journal_date

_DIM = 8


def _vec(text: str) -> list[float]:
    """Детерминированный ненулевой эмбеддинг (cosine-space не любит нулевые)."""
    v = [1.0] * _DIM
    for w in text.lower().split():
        h = int(hashlib.sha1(w.encode("utf-8")).hexdigest(), 16)
        v[h % _DIM] += 1.0
    return v


def fake_embed(texts: list[str]) -> list[list[float]]:
    return [_vec(t) for t in texts]


def make_index(tmp_path) -> ChromaIndex:
    return ChromaIndex(str(tmp_path / "chroma"), fake_embed)


def test_journal_chunks_returns_only_journal_with_embeddings(tmp_path):
    index = make_index(tmp_path)
    index.sync({
        "journal/2026-06-20.md": "# Журнал\n\n- ремонт ванной начали",
        "journal/2026-06-22.md": "# Журнал\n\n- выбирали плитку для ванной",
        "topics/work.md": "# Работа\n\nПётр любит чёрный чай",
        "goals.md": "# Цели\n\nвыучить английский",
    })

    chunks = index.journal_chunks()

    assert {c["file"] for c in chunks} == {
        "journal/2026-06-20.md", "journal/2026-06-22.md",
    }
    for c in chunks:
        assert c["text"].strip()
        assert isinstance(c["embedding"], list)
        assert len(c["embedding"]) == _DIM


def test_journal_chunks_empty_index(tmp_path):
    assert make_index(tmp_path).journal_chunks() == []


# --- Дефолтный порог кластеризации на реальной геометрии Gemini --------------

# 4-мерные векторы, воспроизводящие замеренные на живом журнале дистанции:
#   яйца попарно через E1: E1-E2=0.18, E1-E3=0.26 (≤0.28);
#   AI-день: до E1 = 0.33 (между 0.28 и 0.35 — вот где ломался старый порог).
_EGG1 = "варка яиц всмятку сколько минут"
_EGG2 = "сколько варить перепелиные яйца"
_EGG3 = "свежесть яйца всплывает можно есть"
_AI = "какую AI модель ты используешь"

_GEOMETRY = {
    _EGG1: [1.0, 0.0, 0.0, 0.0],
    _EGG2: [0.82, 0.572, 0.0, 0.0],   # дист до EGG1 ≈ 0.18
    _EGG3: [0.74, 0.0, 0.673, 0.0],   # дист до EGG1 ≈ 0.26
    _AI:   [0.67, 0.0, 0.0, 0.742],   # дист до EGG1 ≈ 0.33
}


def geo_embed(texts: list[str]) -> list[list[float]]:
    return [_GEOMETRY[t] for t in texts]


def _egg_index(tmp_path) -> ChromaIndex:
    index = ChromaIndex(str(tmp_path / "geo"), geo_embed)
    index.sync({
        "journal/2026-06-15.md": _EGG1,
        "journal/2026-06-18.md": _EGG2,
        "journal/2026-06-25.md": _EGG3,
        "journal/2026-06-20.md": _AI,
    })
    return index


def test_default_threshold_separates_theme_from_noise(tmp_path):
    # Конструктор БЕЗ явного max_distance — проверяем именно дефолт.
    suggester = ProactiveSuggester(
        _egg_index(tmp_path),
        label_fn=lambda texts: "варка яиц",
        log=SuggestionLog(tmp_path / "s.db"),
    )
    out = suggester.find_suggestions(today=date(2026, 6, 28))
    assert len(out) == 1
    days = {journal_date(c["file"]) for c in out[0]["chunks"]}
    assert date(2026, 6, 20) not in days          # запись про AI-модель не затянута
    assert days == {date(2026, 6, 15), date(2026, 6, 18), date(2026, 6, 25)}


def test_loose_threshold_overmerges(tmp_path):
    # Документируем, ПОЧЕМУ ужали порог: при 0.35 AI-день сливается с яйцами.
    suggester = ProactiveSuggester(
        _egg_index(tmp_path),
        label_fn=lambda texts: "варка яиц",
        log=SuggestionLog(tmp_path / "s.db"),
        max_distance=0.35,
    )
    out = suggester.find_suggestions(today=date(2026, 6, 28))
    assert len(out) == 1
    days = {journal_date(c["file"]) for c in out[0]["chunks"]}
    assert date(2026, 6, 20) in days              # переслияние: AI попал в кластер
