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
import logging
from datetime import date, datetime, time
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel

import config
from bills import BillStore, current_month
from llm.ollama_client import LLMClient
from memory.facts import FactExtractor
from memory.manager import MemoryManager
from tasks import TaskStore

logger = logging.getLogger(__name__)

INDEX_HTML = Path(__file__).parent / "index.html"


def _serialize_event(e: dict) -> dict:
    """Событие календаря → JSON-safe dict (datetime → ISO-строка)."""
    return {
        "id": e["id"],
        "title": e["title"],
        "start": e["start"].isoformat(),
        "end": e["end"].isoformat(),
        "html_link": e.get("html_link", ""),
    }


class ChatRequest(BaseModel):
    message: str


class TaskCreate(BaseModel):
    title: str
    description: Optional[str] = None
    due_date: Optional[str] = None    # "2026-06-28"
    due_time: Optional[str] = None    # "12:00"
    priority: Optional[str] = "normal"
    source: Optional[str] = "dashboard"


class TaskUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    status: Optional[str] = None      # todo / done / cancelled
    priority: Optional[str] = None
    due_date: Optional[str] = None
    due_time: Optional[str] = None


class BillTemplateCreate(BaseModel):
    name: str
    amount: Optional[float] = None    # nullable — сумма может быть переменной
    day_of_month: int                 # 1-31
    category: Optional[str] = None


class BillTemplateUpdate(BaseModel):
    name: Optional[str] = None
    amount: Optional[float] = None
    day_of_month: Optional[int] = None
    category: Optional[str] = None
    active: Optional[bool] = None      # деактивация без удаления истории


class BillInstanceUpdate(BaseModel):
    status: Optional[str] = None       # pending / paid


def create_app(
    memory: MemoryManager,
    llm: LLMClient,
    facts: FactExtractor,
    tasks: TaskStore,
    bills: BillStore,
    calendar=None,
) -> FastAPI:
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

    @app.get("/api/tasks")
    def list_tasks(status: Optional[str] = None, due: Optional[str] = None) -> dict:
        due_date = date.today().isoformat() if due == "today" else due
        return {"tasks": tasks.list(status=status, due_date=due_date)}

    @app.post("/api/tasks")
    def create_task(req: TaskCreate) -> dict:
        if not req.title.strip():
            raise HTTPException(status_code=400, detail="Пустой title")
        task = tasks.create(
            title=req.title.strip(),
            description=req.description,
            due_date=req.due_date,
            due_time=req.due_time,
            priority=req.priority or "normal",
            source=req.source or "dashboard",
        )
        return {"task": task}

    @app.patch("/api/tasks/{task_id}")
    def update_task(task_id: int, req: TaskUpdate) -> dict:
        existing = tasks.get(task_id)
        if not existing:
            raise HTTPException(status_code=404, detail="Задача не найдена")
        updated = tasks.update(task_id, **req.model_dump(exclude_unset=True))
        return {"task": updated}

    @app.delete("/api/tasks/{task_id}")
    def delete_task(task_id: int) -> dict:
        if not tasks.delete(task_id):
            raise HTTPException(status_code=404, detail="Задача не найдена")
        return {"deleted": task_id}

    @app.get("/api/bills")
    def list_bills(month: Optional[str] = None) -> dict:
        ym = month or current_month()
        # Лениво создаём начисления месяца при первом обращении в этом месяце
        bills.ensure_month(ym)
        return {"month": ym, "bills": bills.list_instances(ym)}

    @app.get("/api/bills/templates")
    def list_bill_templates() -> dict:
        return {"templates": bills.list_templates()}

    @app.post("/api/bills/templates")
    def create_bill_template(req: BillTemplateCreate) -> dict:
        if not req.name.strip():
            raise HTTPException(status_code=400, detail="Пустой name")
        if not 1 <= req.day_of_month <= 31:
            raise HTTPException(status_code=400, detail="day_of_month должен быть 1-31")
        template = bills.create_template(
            name=req.name.strip(),
            day_of_month=req.day_of_month,
            amount=req.amount,
            category=req.category,
        )
        return {"template": template}

    @app.patch("/api/bills/templates/{template_id}")
    def update_bill_template(template_id: int, req: BillTemplateUpdate) -> dict:
        if not bills.get_template(template_id):
            raise HTTPException(status_code=404, detail="Шаблон не найден")
        fields = req.model_dump(exclude_unset=True)
        if "day_of_month" in fields and not 1 <= fields["day_of_month"] <= 31:
            raise HTTPException(status_code=400, detail="day_of_month должен быть 1-31")
        return {"template": bills.update_template(template_id, **fields)}

    @app.patch("/api/bills/{instance_id}")
    def update_bill_instance(instance_id: int, req: BillInstanceUpdate) -> dict:
        if not bills.get_instance(instance_id):
            raise HTTPException(status_code=404, detail="Начисление не найдено")
        if req.status is not None and req.status not in ("pending", "paid"):
            raise HTTPException(status_code=400, detail="status должен быть pending или paid")
        return {"bill": bills.set_status(instance_id, req.status)}

    @app.get("/api/calendar/today")
    def calendar_today() -> list:
        # Календарь опционален: без настроенного token.json или при ошибке API
        # возвращаем [], чтобы дашборд не падал. См. JARVIS_SPEC.md §9.
        if calendar is None:
            return []
        try:
            tz = ZoneInfo(config.CALENDAR_TIMEZONE)
            now = datetime.now(tz)
            start = datetime.combine(now.date(), time.min, tzinfo=tz)
            end = datetime.combine(now.date(), time.max, tzinfo=tz)
            return [_serialize_event(e) for e in calendar.list_events(start, end)]
        except Exception:
            logger.exception("Не удалось получить события календаря")
            return []

    return app
