"""Журнал решений (§19.3): «запиши решение …» / «почему отказались от X».

Это НЕ новая таблица — решения хранятся как структурированные markdown-заметки в
папке decisions/ существующего Obsidian vault и индексируются тем же ChromaIndex,
что и вся память. IntentRouter не знает про LLM/память, поэтому извлечение и запись
живут здесь и вызываются хендлером (как save_link/weekly_review).

DecisionLogger держит два провайдера:
  - llm     — для извлечения структуры решения из свободного текста (Gemini);
  - memory  — MemoryManager: запись заметки (add_decision) и поиск (search_decisions).
"""
import logging
import re
from datetime import date

logger = logging.getLogger(__name__)

EXTRACT_PROMPT = """Ты — помощник, который превращает заметку о принятом решении в \
структуру. Верни ТОЛЬКО один JSON-объект без markdown и пояснений.

Поля:
- "decision" — что решили (кратко, одно-два предложения).
- "reason" — почему так решили (причина/обоснование). Если не сказано — пустая строка.
- "alternatives" — какие варианты рассматривали и отвергли (или пустая строка).
- "related_to" — к какому проекту/теме относится решение (или пустая строка).
- "title" — очень короткий заголовок решения (3-6 слов) для имени файла.

Сообщение:
{text}

JSON строго вида:
{{"decision": "...", "reason": "...", "alternatives": "...", "related_to": "...", "title": "..."}}"""


def _parse_json_object(raw: str) -> dict:
    """Достаёт JSON-объект из ответа модели (она может обернуть его в ```json)."""
    import json
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end <= start:
        return {}
    try:
        data = json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _slug(title: str) -> str:
    """Короткий slug для имени файла из заголовка решения. Кириллицу оставляем
    (Linux/Obsidian с ней работают), бьём по словам, режем до 6 слов."""
    words = re.findall(r"\w+", title.lower(), flags=re.UNICODE)
    slug = "-".join(words[:6])
    return slug or "decision"


def render_decision(fields: dict, today: date) -> str:
    """Структура решения → markdown-заметка для decisions/. Пустые поля опускаем."""
    title = (fields.get("title") or fields.get("decision") or "Решение").strip()
    lines = [f"# {title}", "", f"- **Дата:** {today.isoformat()}"]
    labels = [
        ("decision", "Решение"),
        ("reason", "Причина"),
        ("alternatives", "Альтернативы"),
        ("related_to", "Связано с"),
    ]
    for key, label in labels:
        value = str(fields.get(key) or "").strip()
        if value:
            lines.append(f"- **{label}:** {value}")
    return "\n".join(lines) + "\n"


def format_decisions(results: list[tuple[str, str]]) -> str:
    """Найденные куски решений → ответ пользователю."""
    if not results:
        return "По журналу решений ничего не нашёл 🤔"
    blocks = ["🧭 Нашёл в журнале решений:", ""]
    for text, _ in results:  # имя файла не показываем — только содержимое куска
        blocks.append(text.strip())
        blocks.append("")
    return "\n".join(blocks).strip()


class DecisionLogger:
    def __init__(self, llm, memory):
        self.llm = llm          # LLMClient — извлечение структуры решения
        self.memory = memory    # MemoryManager — запись заметки + поиск

    def log_decision(self, text: str, today: date | None = None) -> str:
        """Извлекает структуру решения из текста, пишет заметку в decisions/ и
        переиндексирует её. Возвращает ответ пользователю."""
        today = today or date.today()
        try:
            raw = self.llm.chat([{"role": "user", "content": EXTRACT_PROMPT.format(text=text)}])
        except Exception:
            logger.exception("log_decision: запрос к LLM не удался")
            return "Не смог разобрать решение 😔 Попробуй сформулировать иначе."
        fields = _parse_json_object(raw)
        if not str(fields.get("decision") or "").strip():
            # модель не нашла решения — сохраняем исходный текст как есть, не теряя
            fields = {"decision": text.strip(), "title": text.strip()[:40]}
        rel_path = f"decisions/{today.isoformat()}-{_slug(fields.get('title') or fields['decision'])}.md"
        content = render_decision(fields, today)
        self.memory.add_decision(rel_path, content)
        return f"🧭 Записал решение: «{fields['decision'].strip()}»"

    def query_decisions(self, query: str) -> str:
        """Семантический поиск по журналу решений (фильтр по папке decisions/)."""
        query = (query or "").strip()
        if not query:
            return "Уточни, какое решение ищешь 🤔"
        results = self.memory.search_decisions(query, k=5)
        return format_decisions(results)
