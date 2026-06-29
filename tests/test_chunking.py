"""Тесты нарезки markdown на чанки (memory/chroma._chunk_markdown), §12.

Баг pre-meeting: пустые секции шаблонов («## Факты», «## Заметки» в about_me.md/
goals.md и темах) попадали в индекс как самостоятельные чанки-заголовки. Короткие
общие слова заголовков ложно близки к любому запросу (замерено: ## Заметки лежит к
«Работа» ближе реального контента), порогом их не отсечь — значит фильтруем на
индексации: чанк из одних заголовков в индекс не идёт.
"""
from memory.chroma import _chunk_markdown
from memory.obsidian import ABOUT_ME_TEMPLATE, GOALS_TEMPLATE


def _is_header_only(chunk: str) -> bool:
    return all((not ln.strip()) or ln.lstrip().startswith("#")
               for ln in chunk.splitlines())


def test_empty_template_produces_no_chunks():
    # about_me.md/goals.md «из коробки» — только заголовки пустых секций → в индекс
    # идти нечему (раньше давали 3 чанка-заголовка каждый).
    assert _chunk_markdown(ABOUT_ME_TEMPLATE) == []
    assert _chunk_markdown(GOALS_TEMPLATE) == []


def test_header_only_sections_are_dropped_but_content_kept():
    md = "# Работа\n\n## Факты\n- встреча с командой по понедельникам\n\n## Заметки\n"
    chunks = _chunk_markdown(md)
    # ни одного чанка из одних заголовков
    assert not any(_is_header_only(c) for c in chunks)
    # содержательная секция уцелела вместе со своим заголовком (контекст)
    assert any("встреча с командой по понедельникам" in c for c in chunks)


def test_bare_title_without_body_is_dropped():
    assert _chunk_markdown("# Просто заголовок\n") == []


def test_content_without_header_is_kept():
    chunks = _chunk_markdown("- купил молоко и хлеб")
    assert chunks == ["- купил молоко и хлеб"]


def test_journal_entry_kept_with_its_header():
    md = "# Журнал 2026-06-29\n\n- **11:00** [user] созвон по проекту лендинг"
    chunks = _chunk_markdown(md)
    assert len(chunks) == 1
    assert "созвон по проекту лендинг" in chunks[0]
