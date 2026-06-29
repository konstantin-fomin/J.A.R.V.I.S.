"""Единый интерфейс памяти: Obsidian (хранение) + ChromaDB (поиск)."""
from datetime import date, timedelta

from memory.chroma import ChromaIndex
from memory.obsidian import ObsidianVault


class MemoryManager:
    def __init__(self, vault: ObsidianVault, index: ChromaIndex, max_results: int):
        self.vault = vault
        self.index = index
        self.max_results = max_results

    def sync(self) -> int:
        """Полная синхронизация индекса с vault. Возвращает число переиндексированных файлов."""
        self.vault.ensure_structure()
        files = {rel: self.vault.read_file(rel) for rel in self.vault.list_files()}
        return self.index.sync(files)

    def remember(self, query: str) -> str:
        """Ищет похожие воспоминания, возвращает текст для системного промпта."""
        results = self.index.search(query, self.max_results)
        if not results:
            return "(память пока пуста)"
        return "\n\n".join(f"[{file}]\n{text}" for text, file in results)

    def relevant_notes(self, query: str, k: int = 3, max_distance: float = 0.32) -> list[tuple[str, str]]:
        """Top-k релевантных кусков памяти, отфильтрованных по порогу косинусной
        дистанции (меньше = ближе). Возвращает [(текст, файл), ...] — может быть
        пусто, если ничего достаточно релевантного нет.

        В отличие от remember() (обычный chat-pipeline берёт top-N без отсева),
        здесь применяется порог: нерелевантные совпадения не подмешиваются.
        Порог задаётся вызывающим (на VPS — из config.MEMORY_RELEVANCE_MAX_DISTANCE);
        0.32 — строгий дефолт-fallback для проактивных вставок (см. config).
        """
        scored = self.index.search_scored(query, k)
        return [(text, file) for text, file, dist in scored if dist <= max_distance]

    def log_message(self, author: str, text: str) -> None:
        """Записывает сообщение в журнал и обновляет индекс."""
        rel_path = self.vault.append_journal(author, text)
        self.index.reindex_file(rel_path, self.vault.read_file(rel_path))

    def add_fact(self, rel_path: str, fact: str) -> bool:
        """Дописывает факт в файл темы и переиндексирует его. False — дубль."""
        if not self.vault.append_fact(rel_path, fact):
            return False
        self.index.reindex_file(rel_path, self.vault.read_file(rel_path))
        return True

    def add_decision(self, rel_path: str, content: str) -> str:
        """Пишет заметку-решение (§19.3) в decisions/ и переиндексирует её тем же
        ChromaIndex, что и остальную память. Возвращает относительный путь."""
        self.vault.write_file(rel_path, content)
        self.index.reindex_file(rel_path, content)
        return rel_path

    def search_decisions(self, query: str, k: int = 5) -> list[tuple[str, str]]:
        """Семантический поиск, отфильтрованный по папке decisions/. Берём с запасом
        (поиск общий по всей памяти) и оставляем только куски из журнала решений."""
        results = self.index.search(query, k * 4)
        return [(text, file) for text, file in results if file.startswith("decisions/")][:k]

    def goals(self) -> str:
        """Содержимое goals.md (пустая строка, если файла нет)."""
        try:
            return self.vault.read_file("goals.md")
        except FileNotFoundError:
            return ""

    def recent_journal(self, days: int = 3) -> str:
        """Журнал за последние N дней одним текстом (включая сегодня)."""
        parts = []
        today = date.today()
        for offset in range(days - 1, -1, -1):
            day = today - timedelta(days=offset)
            try:
                parts.append(self.vault.read_file(f"journal/{day:%Y-%m-%d}.md"))
            except FileNotFoundError:
                continue
        return "\n\n".join(parts)

    def list_files(self) -> list[str]:
        return self.vault.list_files()

    def forget(self, topic: str) -> str | None:
        """Удаляет файл памяти по имени темы. Возвращает удалённый путь или None."""
        topic = topic.strip().removesuffix(".md")
        candidates = [f"topics/{topic}.md", f"{topic}.md", f"journal/{topic}.md"]
        existing = {f.lower(): f for f in self.vault.list_files()}
        for candidate in candidates:
            rel = existing.get(candidate.lower())
            if rel and self.vault.delete_file(rel):
                self.index.remove_file(rel)
                return rel
        return None
