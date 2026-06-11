"""Фоновое извлечение фактов о пользователе из диалога.

После каждого обмена сообщениями диалог отправляется в LLM с просьбой
извлечь долгосрочные факты. Факты дописываются в topics/*.md и
переиндексируются в ChromaDB. Ошибки логируются и глотаются —
это фоновая задача, она не должна ломать основной диалог.
"""
import json
import logging
import re

from memory.manager import MemoryManager

logger = logging.getLogger(__name__)

EXTRACT_PROMPT = """Проанализируй диалог пользователя с ассистентом и извлеки важные \
долгосрочные факты о пользователе: работа, семья, здоровье, привычки, \
предпочтения, события, цели.

Верни ТОЛЬКО JSON-массив без пояснений и без markdown, в формате:
[{{"topic": "work", "fact": "работает дизайнером", "file": "topics/work.md"}}]

Правила:
- topic — короткое имя темы латиницей (work, family, health, hobbies, goals...)
- fact — краткий факт на русском, одним предложением
- file — всегда "topics/<topic>.md"
- мимолётные детали разговора (приветствия, вопросы к ассистенту) — НЕ факты
- если важных фактов нет — верни []

Диалог:
{dialog}"""

# Файл темы принимаем только в безопасном виде, без путей наружу
_SAFE_FILE = re.compile(r"^topics/[a-zа-яё0-9_-]+\.md$", re.IGNORECASE)


def _parse_facts(raw: str) -> list[dict]:
    """Достаёт JSON-массив из ответа модели (она может обернуть его в ```json)."""
    start, end = raw.find("["), raw.rfind("]")
    if start == -1 or end <= start:
        return []
    try:
        data = json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def _target_file(item: dict) -> str | None:
    """Безопасный путь файла темы: из item["file"] или собранный из topic."""
    file = str(item.get("file", "")).strip().replace("\\", "/")
    if _SAFE_FILE.match(file):
        return file
    topic = str(item.get("topic", "")).strip().lower().replace(" ", "_")
    topic = re.sub(r"[^a-zа-яё0-9_-]", "", topic)
    if not topic:
        return None
    return f"topics/{topic}.md"


class FactExtractor:
    """Извлекает факты из диалога через LLM и складывает их в файлы памяти."""

    def __init__(self, llm, memory: MemoryManager):
        self.llm = llm
        self.memory = memory

    def extract_and_save(self, user_text: str, assistant_text: str) -> int:
        """Возвращает число записанных новых фактов."""
        dialog = f"Пользователь: {user_text}\nАссистент: {assistant_text}"
        try:
            raw = self.llm.chat(
                [{"role": "user", "content": EXTRACT_PROMPT.format(dialog=dialog)}]
            )
        except Exception:
            logger.exception("Извлечение фактов: запрос к LLM не удался")
            return 0

        saved = 0
        for item in _parse_facts(raw):
            fact = str(item.get("fact", "")).strip()
            rel_path = _target_file(item)
            if not fact or not rel_path:
                continue
            try:
                if self.memory.add_fact(rel_path, fact):
                    saved += 1
            except Exception:
                logger.exception("Извлечение фактов: не смог записать в %s", rel_path)
        if saved:
            logger.info("Извлечение фактов: записано новых — %d", saved)
        return saved
