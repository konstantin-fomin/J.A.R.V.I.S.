"""Веб-интерфейс: чат с ботом и просмотр памяти через браузер.

Эндпоинты:
  GET  /                    — страница чата (web/index.html)
  POST /chat                — сообщение боту, ответ JSON {"answer": ...}
  GET  /memory              — список файлов памяти
  GET  /memory/{filename}   — содержимое файла памяти

История чата хранится в памяти процесса (бот однопользовательский).
Эндпоинты обычные def — FastAPI выполняет их в thread pool, поэтому
блокирующие вызовы LLM не вешают сервер.
"""
from datetime import date
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel

import config
from llm.ollama_client import LLMClient
from memory.facts import FactExtractor
from memory.manager import MemoryManager

INDEX_HTML = Path(__file__).parent / "index.html"


class ChatRequest(BaseModel):
    message: str


def create_app(memory: MemoryManager, llm: LLMClient, facts: FactExtractor) -> FastAPI:
    app = FastAPI(title="J.A.R.V.I.S.", docs_url=None, redoc_url=None)
    history: list[dict] = []

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(INDEX_HTML)

    @app.post("/chat")
    def chat(req: ChatRequest, background: BackgroundTasks) -> dict:
        text = req.message.strip()
        if not text:
            raise HTTPException(status_code=400, detail="Пустое сообщение")

        memory_context = memory.remember(text)
        messages = [
            {
                "role": "system",
                "content": config.SYSTEM_PROMPT.format(
                    date=date.today().isoformat(),
                    memory_context=memory_context,
                ),
            },
            *history,
            {"role": "user", "content": text},
        ]
        try:
            answer = llm.chat(messages)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Ошибка LLM: {exc}") from exc

        memory.log_message("я", text)
        memory.log_message("бот", answer)

        history.append({"role": "user", "content": text})
        history.append({"role": "assistant", "content": answer})
        del history[: -config.MAX_HISTORY_MESSAGES]

        background.add_task(facts.extract_and_save, text, answer)
        return {"answer": answer}

    @app.get("/memory")
    def list_memory() -> dict:
        return {"files": memory.list_files()}

    @app.get("/memory/{filename:path}")
    def read_memory(filename: str) -> PlainTextResponse:
        # Отдаём только файлы, которые vault сам перечисляет, —
        # это отсекает выход за пределы папки памяти
        if filename not in memory.list_files():
            raise HTTPException(status_code=404, detail="Файл не найден")
        return PlainTextResponse(memory.vault.read_file(filename))

    return app
