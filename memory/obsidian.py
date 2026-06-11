"""Чтение и запись .md файлов памяти в Obsidian vault. Все файлы — UTF-8."""
from datetime import datetime
from pathlib import Path

ABOUT_ME_TEMPLATE = """# Обо мне

## Факты

## Заметки
"""

GOALS_TEMPLATE = """# Цели и планы

## Факты

## Заметки
"""


class ObsidianVault:
    def __init__(self, vault_path: Path):
        self.root = Path(vault_path)

    def ensure_structure(self) -> None:
        """Создаёт папку памяти и базовые файлы, если их нет."""
        (self.root / "journal").mkdir(parents=True, exist_ok=True)
        (self.root / "topics").mkdir(parents=True, exist_ok=True)
        about = self.root / "about_me.md"
        if not about.exists():
            about.write_text(ABOUT_ME_TEMPLATE, encoding="utf-8")
        goals = self.root / "goals.md"
        if not goals.exists():
            goals.write_text(GOALS_TEMPLATE, encoding="utf-8")

    def list_files(self) -> list[str]:
        """Относительные пути всех .md файлов памяти."""
        return sorted(
            p.relative_to(self.root).as_posix()
            for p in self.root.rglob("*.md")
        )

    def read_file(self, rel_path: str) -> str:
        return (self.root / rel_path).read_text(encoding="utf-8")

    def delete_file(self, rel_path: str) -> bool:
        path = self.root / rel_path
        if path.is_file():
            path.unlink()
            return True
        return False

    def append_journal(self, author: str, text: str) -> str:
        """Дописывает запись в журнал за сегодня. Возвращает относительный путь файла."""
        now = datetime.now()
        rel_path = f"journal/{now:%Y-%m-%d}.md"
        path = self.root / rel_path
        if not path.exists():
            path.write_text(f"# Журнал {now:%Y-%m-%d}\n\n", encoding="utf-8")
        entry = text.strip().replace("\n", "\n  ")
        with path.open("a", encoding="utf-8") as f:
            f.write(f"- **{now:%H:%M}** [{author}] {entry}\n")
        return rel_path
