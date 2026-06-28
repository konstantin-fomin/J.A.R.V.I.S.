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
