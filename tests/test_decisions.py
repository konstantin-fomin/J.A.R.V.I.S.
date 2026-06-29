"""Тесты журнала решений (§19.3): DecisionLogger пишет структурированную заметку
в decisions/ существующего Obsidian vault и переиндексирует её, а query_decisions
находит её семантическим поиском.

Гейтинг интентов (log_decision/query_decisions — safe) тестируется без сети.
Живой сценарий extract→write→search прогоняется на настоящем Gemini (extract +
эмбеддинги) и пропускается без GEMINI_API_KEY.
"""
import pytest

import config
from bills import BillStore
from intents import RISK_LEVELS, IntentRouter
from tasks import TaskStore


# --- Гейтинг интентов (без сети) --------------------------------------------

def test_risk_levels_registered():
    assert RISK_LEVELS["log_decision"] == "safe"
    assert RISK_LEVELS["query_decisions"] == "safe"


def _router(tmp_path):
    return IntentRouter(TaskStore(tmp_path / "t.db"), BillStore(tmp_path / "b.db"))


def test_log_decision_gated_safe_executes_with_text(tmp_path):
    r = _router(tmp_path)
    res = r.resolve({"intent": "log_decision", "confidence": "low",
                     "text": "решили перейти на SQLite"})
    assert res.kind == "execute"  # safe → выполняем даже при low
    assert res.action["type"] == "log_decision"
    assert "SQLite" in res.action["text"]


def test_query_decisions_gated_safe(tmp_path):
    r = _router(tmp_path)
    res = r.resolve({"intent": "query_decisions", "confidence": "high",
                     "note": "почему отказались от Postgres"})
    assert res.kind == "execute"
    assert res.action["type"] == "query_decisions"


# --- Живой сценарий на настоящем Gemini --------------------------------------

@pytest.mark.skipif(not config.GEMINI_API_KEY, reason="нужен GEMINI_API_KEY для живого теста")
def test_log_decision_then_found_by_semantic_search(tmp_path):
    from decisions import DecisionLogger
    from llm.ollama_client import LLMClient, OllamaClient, gemini_embed
    from memory.chroma import ChromaIndex
    from memory.manager import MemoryManager
    from memory.obsidian import ObsidianVault

    vault = ObsidianVault(tmp_path / "vault")
    index = ChromaIndex(str(tmp_path / "chroma"), gemini_embed)
    memory = MemoryManager(vault, index, max_results=5)
    memory.sync()  # создаёт структуру vault и пустой индекс
    llm = LLMClient(OllamaClient(config.OLLAMA_BASE_URL, config.OLLAMA_MODEL))
    decisions = DecisionLogger(llm, memory)

    reply = decisions.log_decision(
        "Запиши решение: отказались от Postgres в пользу SQLite — база одна, "
        "нагрузка маленькая, не хочется тащить отдельный сервис. "
        "Рассматривали ещё MySQL, но он избыточен."
    )
    assert isinstance(reply, str) and reply.strip()

    # Заметка реально записана в decisions/
    files = memory.list_files()
    assert any(f.startswith("decisions/") and f.endswith(".md") for f in files)

    # …и находится семантическим поиском (по смыслу, не по точным словам запроса)
    out = decisions.query_decisions("почему мы выбрали SQLite, а не другую базу")
    assert "SQLite" in out


@pytest.mark.skipif(not config.GEMINI_API_KEY, reason="нужен GEMINI_API_KEY для живого теста")
def test_query_decisions_empty_is_honest(tmp_path):
    from decisions import DecisionLogger
    from llm.ollama_client import LLMClient, OllamaClient, gemini_embed
    from memory.chroma import ChromaIndex
    from memory.manager import MemoryManager
    from memory.obsidian import ObsidianVault

    vault = ObsidianVault(tmp_path / "vault")
    index = ChromaIndex(str(tmp_path / "chroma"), gemini_embed)
    memory = MemoryManager(vault, index, max_results=5)
    memory.sync()
    llm = LLMClient(OllamaClient(config.OLLAMA_BASE_URL, config.OLLAMA_MODEL))
    decisions = DecisionLogger(llm, memory)

    out = decisions.query_decisions("почему мы выбрали что-то там")
    assert "реш" in out.lower()  # честный ответ «решений не нашёл», а не пусто/ошибка
