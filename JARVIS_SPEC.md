# Jarvis — спецификация и статус (v3)

Бриф для реализации в Claude Code / Opus 4.8.

---

## 0. Архитектурное решение

**Расширяем существующий репозиторий `konstantin-fomin/J.A.R.V.I.S.`, а не делаем отдельный сервис "Jarvis Core".**

Это не "подстроились под то, что было" — это правильное решение для масштаба проекта:

- Один пользователь, один VPS, один деплой → разнесение на отдельные сервисы (API / bot / scheduler как разные процессы) добавляет сетевое взаимодействие, авторизацию между сервисами, отдельные логи и точки отказа без единой выгоды — нет нагрузки, которая требовала бы независимого масштабирования.
- main.py уже поднимает Telegram-бота (отдельный поток) и FastAPI-веб (главный поток) в одном процессе — это и есть монолит, в который добавляются новые возможности.
- Один SQLite-файл под задачи рядом с уже существующей памятью (Obsidian + ChromaDB) → бэкап тривиален.

**Память (notes/long-term facts) не трогаем.** Текущая реализация — markdown-файлы (Obsidian-совместимые) + автоэкстракция фактов + семантический поиск через ChromaDB — объективно лучше, чем плоская SQL-таблица с keyword-поиском, которая была в первой версии этой спеки. Ничего не переписываем, просто добавляем рядом Tasks / Calendar / Reminders / Bills.

**Где этого решения может не хватить:** когда дойдём до Calendar + Scheduler + Bot-intents (парсинг свободного текста в JSON-намерения от Gemini), там логика выиграет от более структурированного подхода — Pydantic-схемы для intent-объектов, возможно SQLAlchemy вместо чистого `sqlite3`, если таблиц станет много. Это не принципиальный вопрос — решает тот, кто реализует.

### Структура репозитория (как есть сейчас)

```
main.py                  # точка входа: бот в потоке + FastAPI веб в главном
config.py                # настройки из .env
bot/                      # Telegram: polling и обработчики (python-telegram-bot)
llm/                      # роутер провайдеров (ollama/groq/gemini/openrouter/openai/anthropic)
memory/                   # Obsidian-файлы + ChromaDB + менеджер + извлечение фактов
web/                      # FastAPI-сервер (чат + просмотр памяти), порт 8000
tasks.py                  # Tasks — готово, см. раздел 2
```

Важно: эмбеддинги для памяти **всегда** считаются через Gemini (`text-embedding-004` / "Gemini Embedding 1"), даже если основной провайдер ответов — Ollama. `GEMINI_API_KEY` обязателен в любом случае.

⚠️ В `.env.example` / `config.py` дефолт `GEMINI_MODEL=gemini-2.5-pro` — это просто дефолт в репозитории, не реальное состояние. На VPS реально стоит **Gemini 2.5 Flash** для ответов + отдельно Gemini Embedding для эмбеддингов — подтверждено скрином из AI Studio (проект "jarvis", Tier 1, нагрузка ничтожная: 4 RPM из лимита 1K).

### Ресурсы VPS — подтверждено достаточно

1 vCPU / 1GB RAM / 25GB SSD (Vultr, Frankfurt). Текущая загрузка: CPU ~1%, RAM ~450/950 МБ, диск 14.2/22.9 ГБ. Апгрейд не требуется — вся тяжёлая работа (LLM, эмбеддинги) уходит в облако на Gemini, VPS только оркестрирует лёгкие Python-процессы. Tasks/Calendar/Reminders/Bills/scheduler добавляют считанные МБ RAM и КБ-МБ на диск. Единственное ограничение: не переключать `LLM_PROVIDER` на локальный `ollama` на этой машине — даже маленькая модель требует нескольких ГБ RAM, которых физически нет.

Bandwidth (895 ГБ/мес из ~1ТБ+2ТБ свободного пула) — не связан с проектом, это VPN-стриминг через другие контейнеры на том же VPS. Никаких действий для Jarvis не требуется.

---

## 1. Полный список фич (актуальный)

### Core / БД
- [x] **Tasks** — title, description, status, priority, due_date/time, source — готово и протестировано (раздел 2)
- [ ] **Calendar** (Google Calendar как источник правды): просмотр встреч на сегодня/завтра, создание/перенос/удаление через подтверждение, conflict detection
- [ ] **Reminders** — разовые напоминания по времени, отдельно от tasks
- [ ] **Recurring** (шаблон + ежемесячные начисления) — первый конкретный кейс: **Bills/Payments** (раздел 3), архитектура переиспользуется и для повторяющихся задач/привычек позже
- [ ] **Bills / обязательные платежи** — см. раздел 3, дизайн готов
- [ ] **Inbox** — быстрый захват без классификации, разбор позже
- [ ] **Projects** — поле `project` у task/note/memory для группировки
- [ ] **Action log** — что Jarvis сделал сам автоматически и по какому сообщению
- [ ] **Undo** последнего автоматического действия
- [ ] **Backup** данных по расписанию — см. раздел 4, план готов

### Telegram-бот
- [x] Свободный текст → ответ с учётом памяти (уже работает)
- [x] `/plan`, `/memory`, `/forget` (уже работает)
- [x] Свободный текст → structured intent через Gemini (create_task / complete_task / delete_task / query_tasks / mark_bill_paid / query_bills) — раздел 8
- [ ] **Голосовые сообщения** → транскрипция + intent в одном вызове Gemini — см. техническую заметку в разделе 5
- [x] Подтверждение для опасных действий (delete) — кнопки Да/Нет, раздел 8
- [x] Confidence threshold — переспрос при низкой уверенности интента, раздел 8
- [ ] Мультимодальный ввод фото (доска/визитка → заметка/задача через Gemini vision)
- [ ] `/bills` — сводка платежей текущего месяца со статусом оплачено/нет

### Уведомления / scheduler
- [ ] Утренняя сводка, вечерний итог
- [ ] Напоминание перед встречей — расширенная версия: **pre-meeting context bundle** (подтягивать из памяти всё, что писал по теме/человеку, не просто факт времени)
- [ ] Reminders по времени
- [ ] Напоминание о платеже за 1 день до даты (раздел 3)
- [ ] Quiet hours (не слать ночью)

### Dashboard
- [x] Время, дата, погода (Open-Meteo) — прототип готов
- [ ] Задачи на сегодня — дёргать `/api/tasks?due=today`
- [ ] Расписание / ближайшая встреча — после Calendar
- [ ] **Блок "ближайшие платежи"** — список с чекбоксами оплачено/нет, рядом с задачами
- [ ] Быстрая заметка + последние заметки
- [ ] Inbox (неразобранное)

### Память и проактивность (V3)
- [x] Семантический поиск по заметкам/памяти — уже есть (ChromaDB)
- [ ] **Проактивные подсказки из заметок** — если тема упомянута N раз за период, предложить превратить в задачу
- [ ] Лёгкая личная CRM (контакты: последний контакт, дни рождения)
- [ ] Read-it-later: ссылка → summary + тег "почитать", еженедельный дайджест непрочитанного
- [ ] Структурирование по проектам/темам
- [ ] Weekly review
- [ ] AI-сводка: важное/просроченное/забытое

### Отменено / не делаем
- ~~Статус дня~~ — неоднозначная метрика
- ~~Трекер заявок на работу / драфты рекрутерам~~ — работа уже найдена, неактуально
- ~~Письмо в будущее~~
- ~~Погодные nudges~~
- ~~Трекер настроения/энергии~~
- ~~Общий трекер расходов (журнал трат)~~ — не уверен, что будет вестись; отличается от Bills по природе (трекинг постфактум vs обязательства с дедлайном)
- ~~Геолокационные напоминания~~ — не обсуждали приоритет, отложено без решения

### Инфраструктура
- [x] VPS — подтверждено достаточно (см. раздел 0)
- [x] Gemini API — подключен, Flash + Embedding, нагрузка мизерная
- [ ] Spend cap в AI Studio (Spend tab → Monthly spend cap, ~$3-5) — не срочно при текущей нагрузке
- [ ] Tailscale — скачан, настройка отложена до момента, когда дашборд/бэкапы будут дёргать VPS с домашнего ПК. План: `tailscale serve` поверх существующего localhost-сервиса, без смены `WEB_HOST` и без открытия портов наружу
- [ ] Бэкап критичных данных — план готов, раздел 4

---

## 2. Tasks — готово, протестировано

Реализовано и прогнано через `FastAPI TestClient` (create / list / list по статусу / update / delete / delete несуществующей / пустой title) — все кейсы проходят.

### Новый файл `tasks.py` (в корне, рядом с `config.py`)

```python
"""SQLite-хранилище задач. Простая обёртка без ORM — как и остальной код проекта.

Таблица создаётся автоматически при первом обращении.
"""
import datetime
import sqlite3
from pathlib import Path
from typing import Optional


class TaskStore:
    def __init__(self, db_path: Path):
        self._db_path = db_path
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    description TEXT,
                    status TEXT NOT NULL DEFAULT 'todo',
                    priority TEXT NOT NULL DEFAULT 'normal',
                    due_date TEXT,
                    due_time TEXT,
                    source TEXT NOT NULL DEFAULT 'telegram',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )

    def create(
        self,
        title: str,
        description: Optional[str] = None,
        due_date: Optional[str] = None,
        due_time: Optional[str] = None,
        priority: str = "normal",
        source: str = "telegram",
    ) -> dict:
        now = datetime.datetime.utcnow().isoformat()
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO tasks "
                "(title, description, status, priority, due_date, due_time, source, created_at, updated_at) "
                "VALUES (?, ?, 'todo', ?, ?, ?, ?, ?, ?)",
                (title, description, priority, due_date, due_time, source, now, now),
            )
            task_id = cur.lastrowid
        return self.get(task_id)

    def get(self, task_id: int) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return dict(row) if row else None

    def list(self, status: Optional[str] = None, due_date: Optional[str] = None) -> list[dict]:
        query = "SELECT * FROM tasks WHERE 1=1"
        params: list = []
        if status:
            query += " AND status = ?"
            params.append(status)
        if due_date:
            query += " AND due_date = ?"
            params.append(due_date)
        query += " ORDER BY COALESCE(due_time, '23:59'), id"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    def update(self, task_id: int, **fields) -> Optional[dict]:
        fields = {k: v for k, v in fields.items() if v is not None}
        if not fields:
            return self.get(task_id)
        fields["updated_at"] = datetime.datetime.utcnow().isoformat()
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        with self._connect() as conn:
            conn.execute(f"UPDATE tasks SET {set_clause} WHERE id = ?", (*fields.values(), task_id))
        return self.get(task_id)

    def delete(self, task_id: int) -> bool:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        return cur.rowcount > 0
```

### Патч `config.py` — добавить после `CHROMA_PERSIST_DIR`

```python
# Задачи (SQLite, отдельно от памяти)
TASKS_DB_PATH = Path(os.getenv("TASKS_DB_PATH") or (BASE_DIR / "tasks.db"))
```

### Патч `web/server.py`

Импорты:

```python
from typing import Optional
from tasks import TaskStore
```

Pydantic-модели рядом с `ChatRequest`:

```python
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
```

Сигнатура `create_app`:

```python
def create_app(memory: MemoryManager, llm: LLMClient, facts: FactExtractor, tasks: TaskStore) -> FastAPI:
```

Роуты перед `return app`:

```python
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
```

### Патч `main.py`

```python
from tasks import TaskStore  # к импортам

# после facts = FactExtractor(llm, memory):
tasks_store = TaskStore(config.TASKS_DB_PATH)

# в uvicorn.run(...):
uvicorn.run(
    create_app(memory, llm, facts, tasks_store),
    ...
)
```

### Проверка вручную после деплоя

```bash
curl -X POST http://localhost:8000/api/tasks -H "Content-Type: application/json" \
  -d '{"title": "Написать HR", "due_date": "2026-06-28", "due_time": "12:00", "priority": "high"}'

curl http://localhost:8000/api/tasks?due=today
curl -X PATCH http://localhost:8000/api/tasks/1 -H "Content-Type: application/json" -d '{"status": "done"}'
curl -X DELETE http://localhost:8000/api/tasks/1
```

---

## 3. Bills / обязательные платежи — дизайн готов (к реализации)

Мотивация: пользователь уже вручную ведёт в заметках список "что и когда платить" (квартира, кредит, машина, кредитка). Это не разовая задача и не журнал трат — это повторяющееся обязательство с датой и суммой.

### Модель: шаблон + ежемесячное начисление

Важно не путать эти два уровня — иначе "оплачено" в июне навечно останется "оплачено" и в июле:

**`bill_templates`** (создаётся один раз, редко меняется)

| поле | тип | примечание |
|---|---|---|
| id | INTEGER PK | |
| name | TEXT | "Квартира", "Кредит", "Машина", "Кредитка" |
| amount | REAL | nullable — сумма может быть переменной (напр. кредитка) |
| day_of_month | INTEGER | 1-31 |
| category | TEXT | nullable |
| active | BOOLEAN | default true — для остановки без удаления истории |

**`bill_instances`** (авто-создаётся на каждый месяц от активных шаблонов)

| поле | тип | примечание |
|---|---|---|
| id | INTEGER PK | |
| template_id | FK → bill_templates | |
| year_month | TEXT | "2026-07" |
| due_date | DATE | конкретная дата в этом месяце |
| amount | REAL | копия из шаблона на момент создания (на случай если сумма поменяется позже) |
| status | TEXT | `pending` / `paid` |
| paid_at | DATETIME | nullable |

### Поведение

- Scheduler в начале каждого месяца (или лениво — при первом обращении к боту/дашборду в новом месяце) создаёт `bill_instances` для всех активных шаблонов.
- Напоминание — **за 1 день до `due_date`**.
- Отметка "оплачено" — вручную, кнопкой в Telegram (inline keyboard под напоминанием) или чекбоксом на дашборде. PATCH `status=paid`, `paid_at=now`.
- Просмотр в любой момент, не только по напоминанию:
  - Бот: `/bills` — список начислений текущего месяца со статусами.
  - Dashboard: блок "Ближайшие платежи" рядом с задачами, с чекбоксами.

### API (по аналогии с tasks)

```
GET  /api/bills?month=2026-07          → список начислений месяца
GET  /api/bills/templates               → список шаблонов
POST /api/bills/templates               → создать шаблон { name, amount?, day_of_month, category? }
PATCH /api/bills/templates/{id}         → изменить/деактивировать
PATCH /api/bills/{instance_id}          → { status: "paid" }
```

Это первая конкретная реализация паттерна **Recurring** — при необходимости такая же связка "шаблон → инстанс на период" переиспользуется позже для привычек/повторяющихся задач, если до них дойдём.

---

## 4. Backup — план (к реализации вместе с Tailscale)

Раздельные слои:

- **Бэкап всего сервера (Vultr Auto Backups)** — выключен у пользователя осознанно. Не обязателен: это грубый full-disk snapshot за +20% к цене инстанса, а нужные данные — это конкретный небольшой набор файлов, который дешевле и нагляднее бэкапить отдельно.
- **Бэкап данных приложений** — нужен, сейчас отсутствует.

### Что бэкапить (по приоритету)

1. `vaultwarden` — данные пароль-менеджера (самое критичное на всём VPS, не только для Jarvis)
2. Amnezia configs (DNS-forwarder, awg2) — конфиги/ключи VPN
3. Jarvis: `bot-memory/` (Obsidian-заметки), `tasks.db`, `bills.db` (или таблицы в той же БД), `chroma_db/`
4. Все `.env` файлы (токены, ключи — не лежат в git)
5. `adguardhome`, `workout-bot`, `vps-monitor-bot` — низкий приоритет, легко пересоздать

### Механизм

Cron на VPS (раз в сутки для данных Jarvis, реже для редко меняющихся VPN-конфигов) → архивирует папки выше → отправляет архив на домашний ПК по приватной сети **Tailscale** (`rsync`/`scp`), без публичного хостинга и без новых сервисов. Если домашний ПК не всегда онлайн — рассмотреть бесплатный тир Backblaze B2 (10 ГБ) как третью точку, но только если способ с домашним ПК не сработает на практике.

Важно: бэкап, который никогда не проверяли на восстановление — это не бэкап, а надежда. После реализации — хотя бы раз реально развернуть из архива и убедиться, что vaultwarden/amnezia поднимаются с восстановленными данными.

---

## 5. Голосовые сообщения — техническая заметка

Gemini поддерживает аудио как вход и может транскрибировать + сразу извлекать structured intent в одном вызове (тот же пайплайн, что и для текста, просто audio-блок вместо текстового).

⚠️ **Важный технический момент:** голосовые в Telegram приходят как `.ogg` с кодеком **Opus**. Gemini API официально поддерживает контейнер OGG, но по факту это подразумевает **OGG Vorbis** — с OGG/Opus файлами были зафиксированы отказы API. Решение: конвертировать `.ogg` → `.wav`/`.mp3` через `ffmpeg` в обработчике голосового сообщения **перед** отправкой в Gemini. Это одна строчка в pipeline, не архитектурная проблема — но если её не заложить сразу, есть шанс потратить время на дебаг "почему Gemini ругается на формат".

---

## 6. Что дальше (порядок не зафиксирован, на обсуждение)

Варианты следующего шага: Bills (дизайн уже готов, можно реализовывать сразу), Reminders + scheduler, Calendar, Bot-intents (Gemini parsing свободного текста), голосовые сообщения, Tailscale + Backup. Calendar и Bot-intents естественно создают зависимость друг от друга — возможно, логичнее делать их вместе.

---

## 7. Заметки про окружение (отловлено при тестировании)

- `fastapi` 0.138.1 + `pydantic` 2.13.4 на момент тестов — рабочая связка, `req.model_dump(exclude_unset=True)` работает как ожидается.
- В новых версиях `starlette.testclient.TestClient` требует пакет **`httpx2`**, а не классический `httpx` (`pip install httpx2`).
- `web/server.py` уже без авторизации слушает `127.0.0.1` по умолчанию — осознанно, закрывать будем через Tailscale, не через смену `WEB_HOST`.

---

## 8. Bot-intents — свободный текст → действие (дизайн)

Свободный текст пользователя сначала проходит через парсер намерений (Gemini → JSON), и только если намерения нет — падает в обычный chat/memory pipeline. Это превращает бота из «болталки с памятью» в управление задачами и платежами обычным языком, без запоминания команд.

### Парсер: промпт Gemini → JSON

Gemini получает сообщение + текущую дату и возвращает один JSON-объект с полем `intent` — одно из:

`create_task` · `complete_task` · `delete_task` · `query_tasks` · `mark_bill_paid` · `query_bills` · `none`

Дополнительные поля:

- **`title_hint` / `name_hint`** — нечёткий текст пользователя для поиска по подстроке, **НЕ точный ID**. Парсер не знает id в базе; он передаёт слова, по которым роутер ищет совпадение.
- **`confidence`** — `high` | `low`.
- для `create_task` — `title`, `due_date` (`YYYY-MM-DD` или null), `due_time` (`HH:MM` или null), `priority`. Относительные даты («завтра», «в пятницу») разрешаются относительно «сегодня».

### Маппинг intent → действие

| intent | действие |
|---|---|
| `create_task` | `POST /api/tasks` сразу |
| `complete_task` | найти по `title_hint`, `PATCH status=done` сразу |
| `delete_task` | найти, запросить подтверждение кнопками Да/Нет, затем `DELETE` |
| `query_tasks` | `GET /api/tasks`, ответить текстом |
| `mark_bill_paid` | найти инстанс по `name_hint` в текущем месяце, `PATCH status=paid` сразу |
| `query_bills` | `GET /api/bills?month=текущий` |
| `none` | обычный chat/memory pipeline без изменений |

Создание `bill_template` через свободный текст **не делаем** — это редкое действие с финансовыми последствиями, только явной командой/через дашборд.

### Подтверждения

- **delete** — всегда требует подтверждения кнопками Да/Нет (опасное, необратимое).
- **Confidence threshold:** если `confidence=low` — **независимо от типа intent** — не выполнять сразу, переспросить кнопками Да/Нет. Это единственное исключение из правила «подтверждение только для delete»: при низкой уверенности подтверждается любое действие, включая create/complete/query.

### Ненайденная цель

Если по `title_hint`/`name_hint` ничего не нашлось — сказать прямо: «не нашёл задачу похожую на «…»», и **не делать вид, что что-то произошло**. Никаких молчаливых no-op.

---

## 9. Calendar — дизайн

Интеграция с Google Calendar: создание/перенос/удаление/просмотр встреч свободным
текстом (как tasks/bills), напоминания перед встречей и виджет «сегодня» на дашборде.

### Принцип: календарь опционален

Бот должен работать **без настроенного календаря**. Если `credentials.json`/`token.json`
не найдены, `load_calendar()` возвращает `None`, и:
- intent календаря → ответ «Календарь не подключён»;
- эндпоинт `/api/calendar/today` → `[]` (обёрнут в try/except);
- job напоминаний просто не регистрируется.

Никаких падений на старте из-за отсутствия календаря.

### OAuth: credentials.json + token.json

Google OAuth (Desktop App). Поток требует браузера, а VPS headless — поэтому
авторизация **разнесена**:

- `credentials.json` — OAuth client (скачивается из Google Cloud Console), кладётся
  рядом с кодом. **Не коммитим** (в .gitignore).
- `generate_calendar_token.py` — **standalone**-скрипт, НЕ часть бота. Запускается
  **на домашнем компе с браузером**: `InstalledAppFlow.run_local_server()` открывает
  браузер, после согласия сохраняет `token.json`. Файл переносится на VPS.
- `token.json` — рефреш-токен. **Не коммитим**. Бот молча обновляет access-токен
  по нему через `google.auth.transport.requests.Request`.

Scope: `https://www.googleapis.com/auth/calendar` (чтение и запись).

### `calendar_client.py` — обёртка над API

`google-api-python-client` + `google-auth-oauthlib`. Google-импорты ленивые (внутри
методов), чтобы модуль грузился даже без установленных либ/токена — это держит
`select_conflicts` тестируемым без сети.

```
load_calendar() -> CalendarClient | None    # None, если нет credentials/token
class CalendarClient:
    list_events(start, end) -> list[dict]    # [{id, title, start: dt, end: dt, html_link}]
    create_event(title, start, end) -> dict
    update_event(event_id, **fields) -> dict
    delete_event(event_id) -> None
    find_conflicts(start, end, ignore_event_id=None) -> list[dict]
```

`select_conflicts(events, start, end, ignore_id)` — чистая функция пересечения
(`a.start < end and start < a.end`), на ней же стоит `find_conflicts`.

Время — timezone-aware. TZ берётся из `CALENDAR_TIMEZONE` (по умолчанию
`Europe/Moscow`); встречи создаются и сравниваются в ней.

### Intents календаря

Новые intent в парсере и `IntentRouter`: `create_event`, `move_event`,
`delete_event`, `query_events`. Поля (переиспользуем `title`/`title_hint`/`filter`,
добавляем `date`/`start_time`/`end_time`):

| intent | поля | действие |
|---|---|---|
| `create_event` | `title`, `date`, `start_time`, `end_time?` | создать (end по умолчанию +1ч) |
| `move_event` | `title_hint`, `date`, `start_time` | найти встречу, перенести, длительность сохранить |
| `delete_event` | `title_hint` | найти и удалить |
| `query_events` | `filter` (`today`/`week`/null) | показать встречи диапазона |

### Подтверждения: строже, чем у tasks/bills

**ВСЕ изменения календаря идут через подтверждение Да/Нет** — не только delete, а
также create и move. Встречи имеют последствия (приглашённые, занятое время), поэтому
порог осторожности выше, чем у задач. `query_events` — read-only, выполняется сразу.

**Проверка конфликтов:** перед подтверждением create/move вызывается
`find_conflicts(start, end)` (для move — с `ignore_event_id` переносимой встречи).
Если есть пересечение — в текст подтверждения добавляется предупреждение
«⚠️ Пересекается с: «…» (ЧЧ:ММ–ЧЧ:ММ)». Решение остаётся за пользователем: бот
не блокирует, а предупреждает.

### Напоминания перед встречей

Через тот же `JobQueue`, что и у bills, но `run_repeating` (каждые
`CALENDAR_REMINDER_INTERVAL`, по умолчанию 5 мин): job смотрит события в ближайшие
`CALENDAR_REMINDER_LEAD_MINUTES` (по умолчанию 15) и шлёт напоминание. Уже
напомненные `event_id` держатся в in-memory множестве в `job.data`, чтобы не
дублировать (сбрасывается на рестарте — допустимо).

### Web

`GET /api/calendar/today` — события на сегодня для виджета дашборда. Вызов
`calendar_client` обёрнут в try/except; при отсутствии календаря или любой ошибке
API возвращает `[]`, чтобы дашборд не падал без настроенного календаря.

### Что нужно сделать руками (один раз)

1. В Google Cloud Console включить Google Calendar API, создать OAuth client типа
   **Desktop app**, скачать `credentials.json`.
2. На **домашнем компе** (с браузером): положить `credentials.json` рядом с
   `generate_calendar_token.py`, запустить его, пройти согласие — появится `token.json`.
3. Перенести `token.json` (и `credentials.json`) на VPS **в каталог репозитория**
   (рядом с `docker-compose.yml`/`tasks.db`) → внутри контейнера это `/app/token.json`
   и `/app/credentials.json`. Файлы НЕ коммитить.
4. `docker compose up -d --build`.

### Хранение секретов в Docker

`credentials.json`/`token.json` — в `.gitignore` (не коммитим) и в `.dockerignore`
(не запекаются в образ). На рантайме отдаются только через **bind-mount** в
`docker-compose.yml` — как `tasks.db`:

```yaml
- ./credentials.json:/app/credentials.json:ro   # только чтение
- ./token.json:/app/token.json                  # rw: бот перезаписывает при рефреше
```

Это важно по двум причинам: (1) обновлённый `token.json` (рефреш access-токена)
переживает пересборку образа; (2) секреты не попадают в слои образа. Те же грабли с
одиночными файлами, что у `.db`: хостовые файлы должны существовать **до**
`docker compose up`, иначе Docker создаст на их месте каталоги (тогда `load_calendar`
по `is_file()` просто отключит календарь). Не используешь календарь — закомментируй
эти две строки volume.

---

## 10. Action Log & Undo (дизайн)

Каждое изменение данных через бота журналируется, чтобы его можно было отменить
голосом/текстом («отмени», «отмени последнее»). Реализовано: `logger.py`
(`ActionLog`, SQLite — в стиле `tasks.py`/`bills.py`), логирование внутри
`IntentRouter.execute`, intent `undo_last`.

### Таблица `action_log`

| поле | назначение |
|---|---|
| `id` | автоинкремент, порядок = свежесть |
| `timestamp` | UTC ISO, когда выполнено |
| `source` | откуда (`telegram` и т.п.) |
| `entity_type` | `task` / `bill` / `calendar_event` |
| `entity_id` | id затронутой сущности (TEXT: id задач/платежей — int, событий — str) |
| `action` | `create` / `update` / `delete` / `mark_paid` |
| `before_state` | состояние ДО (JSON, `null` для create) |
| `after_state` | состояние ПОСЛЕ (JSON, `null` для delete) |
| `raw_message` | исходный текст пользователя |
| `status` | `active` / `undone` |

### Где снимается состояние

Логирование живёт в **одном месте — обёртке `IntentRouter.execute`** (не размазано
по веткам). `execute` делит работу с `_apply` (само действие → возвращает
`(ответ, entity_id, after_state, ok)`): до мутации снимается `before_state`
(`_read_entity`), после — `after_state`, затем `log_action`. Таблица `_LOGGED`
сопоставляет `action.type` → `(entity_type, action)`; `query_*` и реверсы отмены в
ней отсутствуют и не журналируются. Для календаря `before_state` берётся через
`CalendarClient.get_event` (datetime → ISO в `_event_state`).

### intent `undo_last`

`_resolve_undo`: берём самую свежую запись `status='active'`, строим обратное
действие (`_build_reverse`) и помечаем запись `undone`. Реверс по `action`:

| было | реверс |
|---|---|
| `create` | удалить сущность (`delete_task` / `delete_event`) |
| `update` | восстановить `before_state` (`restore_task` PATCH-ит поля; `move_event` возвращает на прежнее время) |
| `delete` | пересоздать из `before_state` (`create_task` / `create_event`; новый id — допустимо) |
| `mark_paid` | вернуть прежний статус (`set_bill_status` → `pending`) |

**Гашение записи, а не новая запись.** Обратное действие несёт служебный
`_undo_log_id`; `execute`, увидев его, выполняет реверс, помечает исходную запись
`undone` и **не пишет новую** — иначе повторный `undo_last` откатывал бы сам откат.
Так повторный `undo_last` берёт следующую по свежести `active`-запись и откатывает
**другое** действие, а не одно дважды.

**Календарь — через подтверждение.** Если `entity_type='calendar_event'`, реверс
возвращается как `Resolution('confirm', …)` — проходит через Да/Нет, как любое
изменение календаря (§9). Для `task`/`bill` отмена выполняется сразу (`execute`).

### Хранение

`actions.db` рядом с `tasks.db`/`bills.db`: путь в `config.ACTION_LOG_DB_PATH`,
bind-mount в `docker-compose.yml` (те же грабли одиночного файла — `touch actions.db`
до `docker compose up`), в `.gitignore`. Веб-дашборд правит задачи/платежи напрямую
через стора (минуя `IntentRouter`) — эти изменения **не** журналируются и отмене не
подлежат; undo покрывает только действия через бота.

---

## 11. Projects & Inbox (дизайн)

Две фичи из бэклога §1: группировка задач по теме (**Projects**) и быстрый захват
мыслей (**Inbox**). Реализовано: поле `project`, intent'ы `query_by_project`/`capture`,
модуль `inbox.py`, команда `/inbox`.

### Projects

- **Схема:** nullable `project TEXT` в `tasks` и `bill_templates`. Для существующих баз
  — миграция в `_init_db`: `PRAGMA table_info` → `ALTER TABLE … ADD COLUMN project TEXT`,
  если колонки ещё нет (по умолчанию `project=NULL`). `tasks.create`/`create_template`
  получают опциональный `project`; `update`/`update_template` уже generic.
- **Парсер:** в `create_task` добавлено опциональное поле `project` — Gemini извлекает
  тему, **только** если она явно упомянута («задача по ремонту»), иначе `null`.
  Нормализуется в именительный падеж (как `title_hint`).
- **Новый intent `query_by_project`** («что у меня по X», «покажи задачи по проекту X»),
  поле `project`. Read-only → выполняется сразу (без Да/Нет). Фильтр —
  регистронезависимый подстрочный матч в Python (`needle in (project or '').lower()`):
  Python `.lower()` корректно работает с кириллицей, в отличие от SQL `LIKE`. Склонения
  снимает нормализация парсера на обоих концах (и при создании, и при запросе).

### Inbox

- **`inbox.py` — `InboxStore`** (стиль `tasks.py`): таблица `inbox_items
  (id, text, source, created_at, status)`, новые записи `status='pending'`. Методы
  `create / get / list(status) / set_status`.
- **Новый intent `capture`** — срабатывает **только на явный триггер** в тексте
  («запиши в инбокс», «на заметку», «потом разберу», …), поле `note` (текст без
  триггера). Это сознательное ограничение: угадывать инбокс из любой расплывчатой мысли
  нельзя — это конфликтовало бы с `none` и `create_task`. Захват безвреден → авто на
  high-confidence, подтверждение на low.
- **Команда `/inbox`** — список `pending`-заметок, у каждой инлайн-кнопка «→ в задачу»
  (`callback_data=inbox2task:<id>`). Колбэк конвертирует заметку в задачу через
  **существующий create_task path** (`IntentRouter.execute` → задача журналируется и
  отменяема, см. §10), помечает `inbox_item` как `processed` и убирает нажатую кнопку
  (переиспользуется `_markup_without`, как у кнопок оплаты).

### Хранение / проводка

`inbox.db` — отдельная SQLite-база (`config.INBOX_DB_PATH`), bind-mount в
`docker-compose.yml` (те же грабли одиночного файла — `touch inbox.db` до старта),
в `.gitignore`. `InboxStore` создаётся в `main.py` и прокидывается в бот-поток →
`Handlers` → `IntentRouter`. Веб-интерфейс инбокс не использует.

## 12. Pre-meeting context bundle (дизайн)

Расширение напоминаний о встречах (§9 «Напоминания перед встречей»): когда
`remind_events` собирается напомнить о встрече через N минут, дополнительно
поднять из памяти то, что пользователь сам про эту встречу записывал, и приложить
кратким списком под основным текстом. Цель — «контекст к встрече», а не просто
будильник. Реализовано.

### Поведение

- При срабатывании напоминания делаем **семантический поиск по тому же
  `MemoryManager`**, что и обычный chat-pipeline (тот же индекс, те же эмбеддинги
  Gemini) — отдельного хранилища/индекса не заводим.
- **Запрос** = название встречи + описание события (если у Google Calendar event
  есть `description`). Описание теперь прокидывается из `calendar_client` (поле
  `description`, пустая строка если нет).
- Берём **top-3** совпадения (`config.PREMEETING_NOTES_COUNT`) и отсекаем по
  порогу релевантности (см. ниже). Если осталось хоть одно — добавляем секцию
  **«📝 Из твоих заметок:»** списком коротких фрагментов. Если не осталось ничего —
  напоминание уходит как раньше, **без секции**: пустой блок не показываем и
  нерелевантные совпадения не натягиваем.
- Каждый фрагмент сжимается в одну короткую строку (`PREMEETING_SNIPPET_MAX=120`,
  схлопывание пробелов + обрезка с `…`), чтобы напоминание оставалось компактным.

### Порог релевантности

Просьба была «взять текущий порог релевантности из `memory.manager`», но в
обычном chat его **нет**: `remember()` берёт top-N (`MAX_MEMORY_RESULTS`) вообще
без отсечки по дистанции — для системного промпта это нормально, лишний контекст
не вредит. Для напоминания так нельзя: показать пользователю натянутую заметку
хуже, чем не показать ничего. Поэтому введён явный порог по **косинусной
дистанции** (меньше = ближе): `config.MEMORY_RELEVANCE_MAX_DISTANCE` (дефолт
`0.32`, переопределяется из `.env`). Фильтрация живёт в новом
`MemoryManager.relevant_notes(query, k, max_distance)` и отделена от `remember()`.

Pre-meeting — **проактивная** вставка (не ответ на прямой вопрос), поэтому порог
строже общего chat-поиска. `0.32` подобран на реальных Gemini-эмбеддингах: честные
тематические попадания оседают ≤0.28, шумовой пол нерелевантных — ~0.39+, между ними
чистый разрыв. Прежние `0.6` пропускали в напоминание small-talk-диалоги `[я]/[бот]`
и несвязные факты; `0.32` их режет, удерживая заметки-контакты. Категорийного
фильтра по типу контента (факты vs диалоги) сознательно НЕ вводим — релевантность
решается порогом, а не категорией (содержательный `[я]/[бот]`-диалог тоже может быть
валидным совпадением).

### Реализация

- **`memory/chroma.py`:** добавлен `search_scored(query, n) -> [(текст, файл,
  distance)]` (Chroma и так возвращает `distances`); старый `search()` стал тонкой
  обёрткой над ним — обычный chat не затронут.
- **`memory/manager.py`:** `relevant_notes()` — поиск через `search_scored` +
  фильтр `dist <= max_distance`, отдаёт `[(текст, файл), ...]` (может быть пусто).
- **`bot/telegram_bot.py`:** `build_reminder_text(event, minutes, memory,
  notes_count, max_distance)` — **чистая функция** (без сети и Telegram): собирает
  текст напоминания и опциональную секцию заметок, поэтому покрыта юнит-тестами
  (`tests/test_premeeting.py`) на оба сценария. `remind_events` достаёт `memory`
  из `job.data` и вызывает её; поиск по памяти обёрнут в `try/except` — если он
  упадёт, уходит обычное напоминание без заметок (память не должна ломать
  будильник).
- **`config.py`:** `MEMORY_RELEVANCE_MAX_DISTANCE`, `PREMEETING_NOTES_COUNT`.

### Грабли: чанки-заголовки пустых секций (фикс)

Реальное утреннее напоминание показало «📝 Из твоих заметок: ## Заметки,
## Заметки, ## Факты» — в секцию попали заголовки markdown, а не контент. Причина —
**индексация, не порог**. `_chunk_markdown` резал пустые секции шаблонов
(`about_me.md`/`goals.md` и темы с пустыми `## Факты`/`## Заметки`) в чанки из
одного заголовка, которые эмбеддились и шли в ChromaDB как самостоятельные заметки.
Порогом их не отсечь: на живых Gemini-эмбеддингах `## Заметки` лежит к запросу
«Работа» на ~0.32, `## Факты` ~0.32 — **ближе реального контента** (~0.36), потому
что короткие общие слова заголовков сидят в центральной зоне пространства.

Фикс: `memory/chroma._has_content()` выкидывает чанки из одних заголовков ещё на
индексации (в индекс идёт только смысловой текст; заголовок секции остаётся вместе
с телом — для контекста эмбеддинга). Дополнительно `_snippet` в напоминании срезает
ведущий markdown-заголовок, чтобы в секции не маячил `## Заметки`. Тесты:
`tests/test_chunking.py` + `test_snippet_strips_leading_markdown_header`.

⚠️ **Разовый ререиндекс после деплоя.** `sync()` переиндексирует только изменённые
файлы (по md5), поэтому уже проиндексированные чанки-заголовки сами не исчезнут.
Чтобы вычистить их из живого индекса — однократно удалить `chroma_db/file_hashes.json`
(тогда все файлы переиндексируются заново) или весь каталог `chroma_db`, и
перезапустить. Это перечитает эмбеддинги через Gemini — для личного vault'а недорого.

## 13. Проактивные подсказки из заметок (дизайн)

Если одна и та же тема всплывает в журнале несколько раз за короткий срок — бот
сам предлагает превратить её в задачу. Это про обнаружение паттерна, не про
создание: «Да» заводит обычную задачу штатным путём (логируется, отменяема).

### Зачем

Человек по нескольку раз возвращается к одной мысли в дневнике («опять про ремонт
ванной…»), но так и не оформляет её в дело. Раз в день бот замечает такие
повторы и мягко спрашивает: оформить в задачу? Без спама — каждую тему предлагаем
не чаще раза в неделю.

### Источник данных

Только `journal/*.md` за последние `SUGGEST_WINDOW_DAYS` (14) дней. Дата берётся
из имени файла (`journal/ГГГГ-ММ-ДД.md`); topic/fact-файлы без даты в окно не
попадают. Чанки с уже посчитанными эмбеддингами отдаёт `ChromaIndex.journal_chunks()
-> [{text, file, embedding}]` — переиспользуем тот же индекс, что и обычный поиск,
ничего заново не эмбеддим.

### Кластеризация тем

`cluster_chunks(chunks, max_distance, min_size)` — single-link агломерация: чанки
i и j связаны, если косинусная дистанция ≤ `max_distance`; связность транзитивна
(union-find). Возвращаются только группы размером ≥ `min_size` (3) — порог считается
по числу записей, а не дней (интенсивное обсуждение за один день тоже ловится).

**Порог `SUGGEST_MAX_DISTANCE = 0.28` (а не 0.35).** Изначально стоял 0.35, но
прогон на реальных Gemini-эмбеддингах живого журнала показал **переслипание**:
single-link сливал все записи окна в один кластер, потому что заголовки `# Журнал
ДАТА` и форматированные ответы бота структурно похожи между днями (дистанция
заголовок↔заголовок 0.31–0.35) независимо от темы. На живых данных связанные темы
лежат ≤0.26, а шум формата — от ~0.31; порог 0.28 отделяет одно от другого. Это
тюнинг под конкретные эмбеддинги (3072-мерный Gemini, один автор, русский язык),
поэтому вынесен в config.

### Формулировка темы

Для каждого кластера тексты идут в Gemini (`propose_label(llm, texts)`), который
возвращает короткое название задачи (2–4 слова, именительный падеж). LLM сам
игнорирует журнальный шум формата — это его работа, не кластеризатора.

### Дедуп — `suggestion_log`

Отдельная SQLite-база `suggestion_log(theme_hash, label, suggested_at)`.
`theme_hash` стабилен к регистру/пробелам. Перед показом темы смотрим
`last_suggested(hash)`: если предлагали меньше `SUGGEST_REPEAT_BLOCK_DAYS` (7) дней
назад — молчим. **`mark_suggested` зовём сразу после успешной отправки** (а не
после ответа пользователя): иначе краш/рестарт бота между показом и ответом
предложил бы ту же тему повторно. `label_for(hash)` достаёт формулировку темы из
лога — чтобы кнопка «Да», пришедшая после рестарта, не потеряла текст (никакого
состояния в памяти процесса).

### Проводка

Ежедневный job `suggest_from_notes` (стиль `remind_bills`, тот же APScheduler
`run_daily`): `ProactiveSuggester.find_suggestions()` → для каждой темы отдельное
сообщение с inline-кнопками Да/Нет (`build_suggestion_text(label)`), затем
`mark_suggested`. «Да» (`suggest_to_task`) создаёт задачу обычным
`create_task`-путём через `IntentRouter.execute` (`source="suggestion"`) — значит
действие журналируется и отменяемо. «Нет» (`suggest_dismiss`) ничего не делает:
тема уже помечена показанной, `repeat_block_days` не даст спамить.

### Реализация

- **`suggestions.py`:** `cosine_distance`, `journal_date`, `theme_hash`,
  `cluster_chunks`, `build_suggestion_text`, `propose_label`, `SuggestionLog`
  (дедуп + `label_for`), `ProactiveSuggester.find_suggestions`. Всё детерминируемо
  и оффлайн-тестируемо: эмбеддинги приходят из индекса, `label_fn` инъектируется.
- **`memory/chroma.py`:** `journal_chunks()` — journal-чанки с эмбеддингами.
- **`bot/telegram_bot.py`:** job `suggest_from_notes` + callbacks
  `suggest_to_task`/`suggest_dismiss`.
- **`config.py`:** `SUGGESTIONS_DB_PATH`, `SUGGEST_WINDOW_DAYS`,
  `SUGGEST_MAX_DISTANCE`, `SUGGEST_MIN_CLUSTER`, `SUGGEST_REPEAT_BLOCK_DAYS`.
  `suggestions.db` — bind-mount в docker-compose.

## 14. CRM — контакты (дизайн)

Лёгкий персональный CRM: помнить людей, когда последний раз общались и когда у
них день рождения. Один пользователь, никакой иерархии компаний/сделок — только
люди и пара дат. Хранилище — отдельная SQLite-база (как tasks/bills), без ORM.

### Зачем

- Не забывать поздравить с днём рождения (проактивное напоминание за N дней).
- Видеть, с кем давно не общался («покажи контакты»).
- Держать короткие заметки про человека (где работает, как зовут детей и т.п.).

### Хранение — `contacts.py`

Одна таблица `contacts` (создаётся при первом обращении):

| поле                | тип   | смысл                                        |
|---------------------|-------|----------------------------------------------|
| `id`                | INT PK| автоинкремент                                |
| `name`              | TEXT  | имя (обязательно)                            |
| `last_contact_date` | TEXT? | ISO `ГГГГ-ММ-ДД`, когда последний раз общались|
| `birthday`          | TEXT? | ISO `ГГГГ-ММ-ДД`; год хранится, но для «скоро ДР» сравниваем месяц-день |
| `notes`             | TEXT? | свободные заметки                            |
| `created_at`        | TEXT  | ISO-таймстамп                                |
| `updated_at`        | TEXT  | ISO-таймстамп                                |

Методы: `create / get / list / update / delete` (как в tasks.py) + `find(name_hint)`
— подстрочный регистронезависимый матч по имени (склонения нормализует парсер
интентов, возвращая имя в именительном падеже, ровно как `title_hint`).

Дни рождения повторяются ежегодно, поэтому чистая функция `days_until_birthday(
birthday, today)` считает число дней до ближайшей годовщины (год игнорируется;
29 февраля в невисокосный год отмечаем 1 марта). `upcoming_birthdays(within_days,
today)` отдаёт контакты с ДР в окне `[0, within_days]`.

### Intents (через тот же `IntentRouter`)

- **`create_contact`** — добавить человека. Поля: `name`, `birthday?`, `note?`.
- **`update_contact`** — находит по `name_hint`; ставит `last_contact_date = сегодня`
  и/или дописывает `notes` (триггеры: «созвонился с…», «виделся с…», «заметка про…»).
- **`query_contacts`** — `filter`: `upcoming_birthdays` (ближайшие ДР) | `by_name`
  (поиск по `name`) | пусто (все). Read-only — выполняется сразу, без подтверждения.
- **`delete_contact`** — находит по `name_hint`, удаляет **через подтверждение Да/Нет**
  (как любое удаление: tasks/события).

### Проводка и Undo

Все мутации идут через `IntentRouter.execute` → попадают в Action Log с
`entity_type = "contact"` (`create_contact`→create, `update_contact`→update,
`delete_contact`→delete). Реверсы (`_build_reverse`): create→delete,
update→`restore_contact` (вернуть прежние поля), delete→`create_contact` (создать
заново; новый id — норма). Значит `undo_last` работает и для контактов.

### Напоминание о ДР — `bot/telegram_bot.py`

Ежедневный job `birthday_reminder` (стиль `remind_bills`/`remind_events`, тот же
APScheduler `run_daily`): берёт `contacts.upcoming_birthdays(within_days=
config.BIRTHDAY_REMINDER_LEAD_DAYS)` и, если есть, шлёт одно сообщение со списком
(«сегодня / завтра / через N дн»). Окно для запроса «у кого скоро ДР» шире, чем
для напоминания (по умолчанию 30 vs 3 дня).

### `config.py`

`CONTACTS_DB_PATH`, `BIRTHDAY_REMINDER_TIME`, `BIRTHDAY_REMINDER_LEAD_DAYS` (3).
`contacts.db` — bind-mount в docker-compose (как tasks/bills/actions/inbox).

## 15. Read-it-later (дизайн)

«Скинул ссылку — почитаю потом». Бот сохраняет ссылку, сам делает короткое
саммари страницы и раз в неделю присылает дайджест непрочитанного. Идея — не
копить вкладки: пара предложений по каждой ссылке помогает решить, читать или
выкинуть.

### Зачем

Ссылки «на потом» обычно тонут. Здесь они складываются в один список с готовым
саммари (не нужно открывать, чтобы вспомнить, о чём это), а еженедельный дайджест
напоминает разгрести очередь.

### Хранение — `reads.py`

Одна таблица `reads` (стиль tasks.py):

| поле         | тип   | смысл                                  |
|--------------|-------|----------------------------------------|
| `id`         | INT PK| автоинкремент                          |
| `url`        | TEXT  | ссылка (обязательно)                   |
| `title`      | TEXT? | заголовок страницы (если достали)      |
| `summary`    | TEXT? | саммари 2–3 предложения (Gemini)       |
| `status`     | TEXT  | `unread` / `read`                      |
| `created_at` | TEXT  | ISO-таймстамп                          |

Методы: `create / get / list(status) / mark_read / delete`.

### Скачивание и саммари

**Саммари считается ОДИН раз — при сохранении**, и кладётся в БД. Дайджест потом
просто читает готовый `summary`, не дёргая LLM на каждый показ.

- `fetch_article(url)` — `httpx.get` (редиректы, таймаут, User-Agent) → HTML.
- `extract_article(html, url)` — **чистая функция** (без сети): BeautifulSoup на
  `html.parser`, без `lxml`. Берём `og:title`/`<title>` и `og:description`/`meta
  description` + текст первых `<p>` (обрезаем до ~3000 символов). Полный
  ридабилити-парсинг не нужен — для саммари хватает заголовка, описания и начала.
- `summarize_article(llm, title, text)` — один вызов Gemini: 2–3 предложения
  по-русски.
- `enrich_link(llm, url)` — оркестратор: fetch → extract → summarize, возвращает
  `{url, title, summary}`. Сеть/парсинг обёрнуты в try/except: не достали
  страницу — сохраняем со `summary = "(не удалось получить превью)"`, ссылку не
  теряем.

`extract_article` отделена от сети намеренно: её логика покрыта юнит-тестом на
статичном HTML, а реальное скачивание проверяется отдельным live-прогоном (сеть в
обычные тесты не тащим — см. конвенцию «сеть не дёргаем»).

### Intents

- **`save_link`** — Gemini детектит URL **с явным контекстом «почитать позже / на
  потом / в закладки»**, а не любой URL в сообщении. Поле: `url`. Сохранение
  идёт через хендлер (там есть LLM): `enrich_link` в `to_thread`, затем
  `IntentRouter.execute({type: save_link, params: {url, title, summary}})` —
  значит мутация логируется (entity_type=read) и отменяема.
- **`query_reads`** — «что у меня в почитать» — список `unread` в любой момент,
  не только по дайджесту. Read-only.
- **`mark_read`** — отметить ссылку прочитанной (по `title_hint`/URL). Дубль —
  кнопка «✓ Прочитано» под каждой записью в дайджесте (`reads_done:<id>`).

### Проводка и Undo

`save_link` → (read, create), `mark_read` → (read, update) в Action Log. Реверсы:
create→delete, update→restore прежнего `status`. То есть `undo_last` уберёт
случайно сохранённую ссылку или вернёт ошибочно отмеченную обратно в `unread`.
`IntentRouter` остаётся без LLM: саммари ему приносят уже готовым в `params`.

### Еженедельный дайджест — `bot/telegram_bot.py`

Job `reads_digest` — **раз в неделю** (не daily): `run_repeating(interval=
timedelta(weeks=1), first=READS_DIGEST_TIME)`. Берёт `reads.list("unread")`,
шлёт список (title + summary) с кнопкой «✓ Прочитано» у каждой. Пусто —
молчит.

### `config.py`

`READS_DB_PATH`, `READS_DIGEST_TIME`. `reads.db` — bind-mount в docker-compose
(как tasks/bills/actions/inbox/contacts).

## 16. Weekly review (дизайн)

Раз в неделю бот присылает короткую сводку: что сделано, что висит, что впереди.
Ключевой принцип — **цифры считает Python, а не LLM**. LLM только оборачивает уже
готовые числа в дружелюбный абзац. Так сводка не врёт (никаких галлюцинаций в
статистике) и при этом читается по-человечески.

### Зачем

«Подведи итоги недели» без ручного копания по задачам/платежам/контактам. Сводка
агрегирует то, что уже лежит в сторах, и подсвечивает важное: просроченное,
ближайшие ДР, накопившуюся очередь «почитать».

### Два шага — `weekly_review.py`

1. **`compute_week_stats(start, end, *, tasks, bills, contacts, reads, log)`** —
   ЧИСТАЯ функция (без сети/LLM), агрегирует факты из существующих сторов и
   возвращает структурированный dict чисел/списков:
   - `tasks`: выполнено за неделю (status=done, `updated_at` в окне) + просрочено
     на сейчас (status=todo, `due_date < end`);
   - `bills`: оплачено/ожидает в месяце (`end`-месяц, `list_instances`);
   - `birthdays`: ДР в ближайшие 7 дней от `end` (`contacts.upcoming_birthdays`);
   - `reads_unread`: размер очереди «почитать»;
   - `actions`: сколько действий за неделю по типам (`ActionLog.actions_between`,
     группировка по `entity_type.action`) + `actions_total`.

   Числа гарантированно точные — это обычный Python над SQLite, без LLM.

2. **`compose_summary(llm, stats)`** — ОДИН вызов Gemini. Получает уже посчитанный
   `stats` (JSON в промпте) и пишет короткий дружелюбный абзац вокруг этих чисел.
   Промпт **строго ограничивает**: использовать только переданные числа, ничего не
   досчитывать и не выдумывать, без markdown/списков. LLM здесь — стилист, не
   аналитик.

`format_review(stats)` — детерминированный текстовый рендер тех же чисел: фолбэк,
если Gemini недоступен (сводку всё равно пришлём, просто суше).

### Intent и job

- **`query_weekly_review`** — «сводка за неделю», «что у меня было на этой неделе»
  — тот же расчёт по запросу в любой момент. Read-only. Расчёт делает
  `IntentRouter` (у него есть все сторы), а `compose_summary` зовёт хендлер (у
  него есть LLM) — `IntentRouter` остаётся без LLM. Фолбэк на `format_review`,
  если LLM упал.
- **Job `weekly_review`** — **воскресенье вечером**, отдельное время от
  `reads_digest` (чтобы не прислать два сообщения подряд): `run_repeating(interval=
  1 неделя, first=ближайшее воскресенье в WEEKLY_REVIEW_TIME)`. Окно — последние 7
  дней. Шлёт `compose_summary(compute_week_stats(...))`.

### `logger.py`

Добавлен `ActionLog.actions_between(start, end)` — записи журнала с `timestamp` в
`[start, end)` (ISO-строки сравниваются лексикографически = хронологически). Это
единственное расширение существующих сторов; остальное read-only.

### Конфиг

`WEEKLY_REVIEW_TIME` — константа в `bot/telegram_bot.py` (как и остальные
`*_REMINDER_TIME`). Своей БД у фичи нет — она только читает чужие сторы.

## 17. Confirmation policy & edit_last (дизайн)

Две связанные доработки роутера: подтверждения становятся декларативной таблицей
рисков, и появляется правка последнего действия (`edit_last`) — обе опираются на
уже существующие пути (`IntentRouter.execute`, журнал, `undo_last`), новых баз и
новых способов мутации не вводят.

### Confirmation policy — таблица рисков

Раньше решение «выполнить сразу или переспросить Да/Нет» было размазано по
`resolve()`: где-то `_auto_or_confirm(...)`, где-то точечное `Resolution("confirm")`
для `delete_*`/календаря, где-то прямой `execute` для read-only. Это работало, но
не читалось одним взглядом и плохо расширялось.

Вместо этого — **одна декларативная таблица** `RISK_LEVELS = {intent: уровень}` в
начале `intents.py`, где уровень один из трёх:

- **`safe`** — выполнить сразу **всегда** (read-only `query_*`, а также `save_link`,
  который и раньше шёл без подтверждения);
- **`medium`** — выполнить сразу, **если `confidence != "low"`**; при низкой
  уверенности — переспросить (ровно прежнее поведение `_auto_or_confirm`);
- **`dangerous`** — **всегда** подтверждение Да/Нет, независимо от уверенности
  (удаления, любые изменения календаря).

Роутер вместо разрозненных проверок зовёт один шлюз `_gate(intent, action, label,
low)`, который смотрит на `RISK_LEVELS[intent]` и возвращает `execute` или
`confirm`. Таблица покрывает **все** текущие мутирующие/запросные интенты;
поведение не меняется — рефактор чисто структурный (проверяется регрессией на
существующих тестах подтверждений и отмены). Новый интент → одна строка в таблице,
а не новая ветка с `if`. `undo_last`/`edit_last` остаются отдельными путями (у них
своя логика выбора execute/confirm по типу затронутой сущности).

### edit_last — правка последнего действия

Новый интент `edit_last` для фраз вида «не завтра, а в пятницу», «сделай приоритет
высоким», «переименуй в …», «добавь к прошлой заметке: …». Gemini возвращает
`{"intent": "edit_last", "field": "...", "value": "..."}`.

Роутер берёт `latest_active()` из журнала (**тот же метод, что у `undo_last`**) —
то есть правит ровно ту сущность, которую затронуло последнее незаотменённое
действие. По таблице допустимых полей `_EDIT_LAST_FIELDS[entity_type][field]`
строится обычное обновление и прогоняется через **существующий update-путь**
(`execute` → `edit_task`/`edit_contact`). Значит правка автоматически:

- журналируется как обычный `update` (снимаются before/after);
- **отменяема через `undo_last`** (реверс `update` уже умеет восстанавливать
  before — отдельного кода отмены не нужно).

Поддерживаемые поля (декларативно, режим `set` заменяет, `append` дописывает):

- **task**: `title`, `priority`, `due_date`, `due_time` — все `set`;
- **contact**: `name`, `birthday` (`set`), `notes`/`note` (`append` — дописывает к
  существующей заметке через `\n`, как `update_contact`).

Если поле **не применимо** к типу последней сущности (например «приоритет» к
контакту, или последнее действие — платёж/ссылка/встреча, или поле неизвестно) —
роутер честно отвечает «к последнему действию это изменение не подходит» и **не
делает молчаливый no-op** и не пытается угадать другое поле. Если отменять/править
нечего (журнал пуст) — тоже честное сообщение.

## 18. Snooze/defer, Recurring tasks, Quiet hours (дизайн)

Три независимые доработки. Snooze и recurring опираются на уже существующие пути
(`IntentRouter.execute`, журнал, `TaskStore`); quiet hours — общий слой над
job'ами планировщика, который не меняет их бизнес-логику.

### 18.1. Snooze/defer — отложить последнее действие

Новый интент `snooze` для фраз «отложи на вечер», «напомни через 2 часа», «не
сегодня», «перенеси на завтра». Gemini возвращает
`{"intent": "snooze", "offset": "...", "confidence": ...}`, где `offset` —
**относительное** смещение в канонической форме, а не абсолютная дата. Парсер
(`normalize_snooze_offset`) нормализует его в конкретные `due_date`/`due_time`
относительно «сейчас» — тем же приёмом «hint от Gemini → нормализация в коде»,
что и `title_hint`.

Канонические значения `offset` (что просим у Gemini):

- именованные: `evening` (сегодня 19:00), `afternoon` (сегодня 15:00),
  `tonight` (сегодня 21:00), `morning` (завтра 09:00), `tomorrow` (завтра, время
  не трогаем), `next_week` (+7 дней, время не трогаем);
- длительности: `"<N>m"`/`"<N>h"` (сейчас + N минут/часов → ставит и дату, и
  время), `"<N>d"` (сегодня + N дней, время не трогаем). Допускается префикс `+`.

Применяется к `latest_active()` из журнала — **тот же механизм поиска, что у
`edit_last`**. Snooze **не создаёт новую сущность**: это вызов существующего
`edit_last`-пути с предустановленным offset вместо произвольного `value`. Для
последней задачи строится обычное обновление `edit_task` с полями из
нормализации, прогоняется через `execute` → значит автоматически журналируется как
`update` и **отменяемо через `undo_last`**. Уровень риска в `RISK_LEVELS` —
`medium` (как обычный edit): сразу при `confidence != low`, иначе переспросить.

Если последнее действие — не задача (платёж/контакт/ссылка/встреча) или offset не
распознан, роутер честно отвечает сообщением и ничего не меняет (не делает
молчаливый no-op), как и `edit_last`.

### 18.2. Recurring tasks — повторяющиеся задачи

Отдельно от Bills: у платежей фиксированный день месяца, у повторяющихся задач —
гибкая повторяемость (каждый день / по дню недели / по числу месяца). Поэтому
своя таблица `recurring_task_templates` (а не переиспользование `bill_templates`):

```
recurring_task_templates(
  id, title, recurrence_type ('daily'|'weekly'|'monthly'),
  day_of_week  (0=Пн..6=Вс, nullable — для weekly),
  day_of_month (1..31,     nullable — для monthly, зажимается до конца месяца),
  time  (HH:MM, nullable), project (nullable), active)
```

Генерация по аналогии с `BillStore.ensure_month`, но «на день»:
`RecurringTaskStore.ensure_day(target_date, tasks)` создаёт task-инстансы на
конкретную дату для активных шаблонов, которые «стреляют» в этот день (daily —
всегда; weekly — если `target_date.weekday() == day_of_week`; monthly — если число
совпадает с `day_of_month`, зажатым до последнего дня месяца). Идемпотентно: задача
помечается источником `source='recurring'` и `recurring_template_id`, повторный
запуск в тот же день дубль не создаёт (`TaskStore.recurring_exists`).

Ежедневный job `generate_recurring_tasks` зовёт `ensure_day(today)`. Отдельный job
`cleanup_recurring_tasks` чистит историю: **выполненные** recurring-инстансы
старше 30 дней удаляются (`TaskStore.purge_recurring_done`). Чистятся **только**
задачи с `source='recurring'` — обычные задачи (даже выполненные и старые) не
трогаются. Оба job'а в Telegram ничего не шлют, поэтому quiet hours к ним не
применяются.

Интенты: `create_recurring_task` (поля `title`, `recurrence_type`, `day_of_week`,
`day_of_month`, `time`, `project`), `query_recurring_tasks` (показать активные
шаблоны), `delete_recurring_template` (по `title_hint`). Риски: create — `medium`,
query — `safe`, delete — `dangerous` (всегда подтверждение). Шаблоны в журнал
действий не пишутся (как и `bill_templates`).

### 18.3. Quiet hours — тихие часы

Два поля в `.env`: `QUIET_HOURS_START=23:00`, `QUIET_HOURS_END=09:00`. Функция
`is_quiet_now()` в общем модуле `scheduler_utils.py` отвечает, идут ли сейчас тихие
часы; корректно обрабатывает переход через полночь (`start > end`). Границы:
`start` включительно, `end` исключительно (23:00 ровно — тихо, 08:59 — тихо,
09:00 — уже нет). Окно нулевой длины (`start == end`) = тихих часов нет.

Проверка встроена в начало **всех** job'ов, шлющих сообщения в Telegram
(`remind_bills`, `remind_events`, `suggest_from_notes`, `birthday_reminder`,
`reads_digest`, `weekly_review`). Если сейчас тихо — сообщение **не теряется**:
`quiet_defer` перепланирует тот же job через `JobQueue.run_once` на момент конца
окна (`QUIET_HOURS_END`) и прекращает текущий вызов, ничего не отправив. Отложенный
запуск отрабатывает уже вне тихих часов и доставляет сообщение. Дублей нет: обычный
ежедневный/периодический запуск job'а в тихие часы только откладывает (не шлёт), а
доставка происходит ровно один раз — в отложенном запуске; идемпотентные job'ы
(`remind_events` через множество `reminded`) дополнительно защищены от повторов.

## 19. Obligations, Review queues, Decision log (дизайн)

Три независимые фичи, объединённые темой «отслеживать то, что висит»: внешние
обязательства (кто кому должен), очереди разбора инбокса (не всё — задача) и
журнал решений (почему сделали так). Все три переиспользуют существующие
механизмы (Action Log + undo, `_gate`/`RISK_LEVELS`, Obsidian + ChromaIndex), а не
вводят новые подсистемы.

### 19.1. Obligations — обязательства (waiting_on / i_owe)

Отдельный стор `ObligationStore` (`obligations.py`, стиль `tasks.py`/`contacts.py`,
чистый sqlite3). Это **не** задачи: у задачи есть срок и она «моя к выполнению»,
а обязательство — про отношения с человеком («жду от Пети отчёт», «я должен Маше
денег»). Своя таблица:

```
obligations(
  id, title, person,
  direction ('waiting_on' | 'i_owe'),
  since_date, follow_up_date (nullable),
  status ('open' | 'done' | 'cancelled'),
  source, related_project (nullable),
  created_at, updated_at)
```

Интенты и риски (см. §17, единый шлюз `_gate`):

- `create_obligation` — `medium`. Поля: `title`, `person`, `direction`
  (`waiting_on` — жду от кого-то; `i_owe` — я должен), `since_date` (по умолчанию
  сегодня), `follow_up_date` (когда напомнить, nullable), `related_project`
  (nullable).
- `query_obligations` — `safe`. Фильтры: `direction` (waiting_on/i_owe) и/или
  `person` (подстрочный матч). Без фильтров — все открытые.
- `complete_obligation` — `medium`. Поиск по `title_hint` (или `person`) среди
  открытых; один матч — закрываем (`status='done'`), несколько — переспрашиваем.
- `delete_obligation` — `dangerous` (всегда подтверждение).

Все мутации проходят через `IntentRouter.execute`, журналируются в Action Log
(`entity_type='obligation'`) и отменяемы через `undo_last` — ровно как
tasks/contacts: `create` ⇄ delete, `update`(complete) ⇄ restore прежнего статуса,
`delete` ⇄ создать заново. Реверсы (`restore_obligation`) — обычные апдейты, тоже
отменяемы.

Ежедневный job `follow_up_obligations` (стиль `remind_bills`): находит открытые
обязательства с `follow_up_date <= сегодня` и шлёт напоминание. Обёрнут
`quiet_defer` (тихие часы §18.3): в окно молчания доставка откладывается на конец
окна, не теряясь. Пусто — молчит.

### 19.2. Review queues — статусы инбокса

Инбокс перестаёт быть бинарным (`pending`/`processed`). Добавляются статусы
разбора: `someday` (когда-нибудь), `needs_decision` (нужно решить),
`maybe_later` (может быть потом). Схема таблицы не меняется (статус — свободный
TEXT), меняется набор допустимых значений и отображение.

Чтобы быстрый захват можно было переразобрать тем же приёмом, что `edit_last`/
`snooze` (§18.1), `capture` теперь **журналируется** (`entity_type='inbox'`,
`action='create'`) и отменяем (`undo_last` удаляет запись инбокса —
`InboxStore.delete`). Новый интент:

- `inbox_reclassify` («это не задача, отложи на подумать») — `medium` (как edit).
  Берёт `latest_active()` из Action Log; если это запись инбокса — меняет её
  статус на `someday`/`needs_decision`/`maybe_later` (поле `status` от Gemini).
  Идёт через `execute` → логируется как `update` (`entity_type='inbox'`) и
  **отменяем** через `undo_last`. Если последнее действие — не инбокс или статус
  не распознан, роутер честно отвечает сообщением (не молчаливый no-op).

`/inbox` группирует вывод по статусу (📥 на разбор / 🤔 нужно решить / 💤
когда-нибудь / ⏳ может быть потом), а не показывает только `pending`. Кнопка «→ в
задачу» остаётся на активных (не `processed`) записях.

### 19.3. Decision log — журнал решений

**Не новая таблица.** Решения — это структурированные markdown-заметки в новой
папке `decisions/` существующего Obsidian vault, индексируемые тем же
`sync()`/`ChromaIndex`, что и остальная память. Новый код для индексации не
нужен — переиспользуется `MemoryManager`/`ObsidianVault`.

Интенты (оба `safe`):

- `log_decision` («запиши решение: …») — Gemini извлекает `decision`, `reason`,
  опционально `alternatives` и `related_to`. Заметка пишется в
  `decisions/ГГГГ-ММ-ДД-<slug>.md` тем же writer'ом (`ObsidianVault`), что и
  facts/journal, и переиндексируется (`MemoryManager.add_decision` →
  `index.reindex_file`). Извлечение и запись живут в `decisions.py`
  (`DecisionLogger`), потому что `IntentRouter` не знает про LLM/память — как
  `save_link`/`weekly_review`, исполнение делает хендлер.
- `query_decisions` («почему отказались от X», «какие решения по Y») — обычный
  семантический поиск через `MemoryManager`, отфильтрованный по папке
  `decisions/`. Нет релевантных решений — честно сообщаем.

Поскольку обе операции требуют LLM/память, `IntentRouter` лишь гейтит их `safe` и
возвращает `execute`-действие с исходным текстом; саму работу (extract+write для
`log_decision`, поиск для `query_decisions`) выполняет `Handlers`, перехватывая
тип действия — тем же приёмом, что `save_link` и `query_weekly_review`.
