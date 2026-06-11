"""Индексация .md файлов памяти в ChromaDB и семантический поиск.

Эмбеддинги считаются через Ollama (модель задаётся в config.OLLAMA_EMBED_MODEL),
чтобы поиск нормально работал с русским языком. Хэши файлов хранятся рядом
с индексом — при старте переиндексируются только изменённые файлы.
"""
import hashlib
import json
from pathlib import Path
from typing import Callable

import chromadb

CHUNK_MAX_CHARS = 800


def _chunk_markdown(content: str) -> list[str]:
    """Режет markdown на куски: по заголовкам, крупные секции — по абзацам."""
    sections: list[str] = []
    current: list[str] = []
    for line in content.splitlines():
        if line.startswith("#") and current:
            sections.append("\n".join(current))
            current = []
        current.append(line)
    if current:
        sections.append("\n".join(current))

    chunks: list[str] = []
    for section in sections:
        section = section.strip()
        if not section:
            continue
        if len(section) <= CHUNK_MAX_CHARS:
            chunks.append(section)
            continue
        buf = ""
        for para in section.split("\n"):
            if len(buf) + len(para) > CHUNK_MAX_CHARS and buf:
                chunks.append(buf.strip())
                buf = ""
            buf += para + "\n"
        if buf.strip():
            chunks.append(buf.strip())
    return chunks


class ChromaIndex:
    def __init__(self, persist_dir: str, embed_fn: Callable[[list[str]], list[list[float]]]):
        self._embed = embed_fn
        self._client = chromadb.PersistentClient(path=persist_dir)
        self._collection = self._client.get_or_create_collection(
            name="bot_memory",
            metadata={"hnsw:space": "cosine"},
        )
        self._hashes_path = Path(persist_dir) / "file_hashes.json"

    # --- работа с хэшами ---

    def _load_hashes(self) -> dict[str, str]:
        if self._hashes_path.exists():
            return json.loads(self._hashes_path.read_text(encoding="utf-8"))
        return {}

    def _save_hashes(self, hashes: dict[str, str]) -> None:
        self._hashes_path.write_text(
            json.dumps(hashes, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    # --- индексация ---

    def sync(self, files: dict[str, str]) -> int:
        """Синхронизирует индекс с файлами {rel_path: content}.

        Переиндексирует новые и изменённые, удаляет исчезнувшие.
        Возвращает число переиндексированных файлов.
        """
        old_hashes = self._load_hashes()
        new_hashes = {
            rel: hashlib.md5(content.encode("utf-8")).hexdigest()
            for rel, content in files.items()
        }
        changed = [rel for rel, h in new_hashes.items() if old_hashes.get(rel) != h]
        removed = [rel for rel in old_hashes if rel not in new_hashes]

        for rel in changed:
            self._reindex_file(rel, files[rel])
        for rel in removed:
            self.remove_file(rel)

        self._save_hashes(new_hashes)
        return len(changed)

    def reindex_file(self, rel_path: str, content: str) -> None:
        """Инкрементальная переиндексация одного файла с обновлением хэша."""
        self._reindex_file(rel_path, content)
        hashes = self._load_hashes()
        hashes[rel_path] = hashlib.md5(content.encode("utf-8")).hexdigest()
        self._save_hashes(hashes)

    def _reindex_file(self, rel_path: str, content: str) -> None:
        self._collection.delete(where={"file": rel_path})
        chunks = _chunk_markdown(content)
        if not chunks:
            return
        self._collection.add(
            ids=[f"{rel_path}::{i}" for i in range(len(chunks))],
            documents=chunks,
            embeddings=self._embed(chunks),
            metadatas=[{"file": rel_path}] * len(chunks),
        )

    def remove_file(self, rel_path: str) -> None:
        self._collection.delete(where={"file": rel_path})
        hashes = self._load_hashes()
        if rel_path in hashes:
            del hashes[rel_path]
            self._save_hashes(hashes)

    # --- поиск ---

    def search(self, query: str, n_results: int) -> list[tuple[str, str]]:
        """Топ-N похожих кусков памяти. Возвращает [(текст, файл), ...]."""
        if self._collection.count() == 0:
            return []
        result = self._collection.query(
            query_embeddings=self._embed([query]),
            n_results=min(n_results, self._collection.count()),
        )
        docs = result["documents"][0]
        files = [m["file"] for m in result["metadatas"][0]]
        return list(zip(docs, files))
