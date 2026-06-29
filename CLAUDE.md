# CLAUDE.md

Guidance for Claude Code working in this repository. Keep it short and current —
this file is loaded into context every session. Full design lives in
[`JARVIS_SPEC.md`](JARVIS_SPEC.md); the user-facing readme is [`README.md`](README.md).

## Что это

J.A.R.V.I.S. — личный AI-ассистент: Telegram-бот + FastAPI веб-интерфейс в одном
процессе. Память — Markdown-файлы (Obsidian-совместимые) + семантический поиск
через ChromaDB. LLM-ответы через облачного провайдера (по факту на VPS — Gemini
2.5 Flash), эмбеддинги всегда через Gemini.

Один пользователь, один VPS, один процесс — это монолит по дизайну, не недоделка.
Не разноси на сервисы (см. JARVIS_SPEC.md §0).

## Архитектура

`main.py` — точка входа. Telegram-polling крутится в отдельном daemon-потоке,
FastAPI/uvicorn — в главном.

```
main.py        точка входа: проверки env → sync памяти → бот-поток + веб
config.py      всё из .env (пути, токены, модели, провайдер)
bot/           Telegram: telegram_bot.py (polling + daily-jobs) + handlers.py (команды/кнопки)
llm/           ollama_client.py — роутер провайдеров (LLMClient.get_response) + gemini_embed
memory/        obsidian.py (файлы) + chroma.py (индекс) + manager.py + facts.py (автоэкстракция)
web/           server.py — FastAPI (чат + просмотр памяти)
intents.py     свободный текст → JSON-намерение → IntentRouter → действие над сторами
tasks.py       TaskStore (tasks.db); bills.py BillStore (bills.db); inbox.py InboxStore (inbox.db)
recurring.py   RecurringTaskStore (recurring.db) — повторяющиеся задачи (§18.2), отдельно от Bills
contacts.py    ContactStore (contacts.db) — лёгкий CRM (§14)
obligations.py ObligationStore (obligations.db) — обязательства waiting_on/i_owe (§19.1)
decisions.py   DecisionLogger — журнал решений в decisions/ Obsidian-vault (§19.3), исполняет Handlers
suggestions.py проактивные подсказки из заметок (§13): кластеризация journal-тем
logger.py      ActionLog (actions.db) — журнал мутаций + undo_last (§10)
scheduler_utils.py  тихие часы (§18.3): is_quiet_now + quiet_defer (отложить job до конца окна)
calendar_client.py  Google Calendar (опц., token.json); voice.py — голос → Gemini-транскрипция
```

Все мутации (tasks/bills/calendar/contacts/obligations/inbox) проходят через
`IntentRouter.execute`, логируются в `ActionLog` и отменяемы через `undo_last`
(`edit_last` правит одно поле последнего действия — тоже как обычный update,
отменяем; `inbox_reclassify` (§19.2) тем же путём меняет статус разбора последней
записи инбокса; `capture` теперь журналируется и отменяем). Выполнить сразу или
переспросить — решает декларативная таблица `RISK_LEVELS` (safe/medium/dangerous) в
начале `intents.py` через единый шлюз `_gate`, а не разрозненные `if` (§17). `snooze`
(§18.1) — отложить последнюю задачу: переиспользует `edit_last`-путь, нормализуя
относительный offset в due_date/due_time. Ежедневные job'ы (платежи, ДР, проактивные
подсказки, генерация/очистка повторяющихся задач §18.2) и напоминания о встречах — в
`bot/telegram_bot.py`. Все job'ы, шлющие в Telegram, обёрнуты `quiet_defer` (тихие
часы §18.3): в окно молчания доставка откладывается на его конец, не теряясь.

## Команды

```bash
# Docker (как на VPS)
docker compose up -d --build      # собрать и поднять
docker compose logs -f bot        # логи
docker compose restart bot        # после правки .env
docker compose down               # остановить

# Локально (venv уже есть в .venv)
.venv/bin/python main.py          # запуск
.venv/bin/pip install -r requirements.txt

# Тесты (pytest, ~117 тестов в tests/)
.venv/bin/python -m pytest tests/ -q
```

Тесты есть (`tests/test_*.py`) — новую фичу пиши через TDD (см. TDD-скилл): сначала
красный тест, потом реализация. Сторы тестируются как самостоятельные SQLite-базы
на `tmp_path`, интеграция intent→действие/undo — через реальный `IntentRouter`;
сеть/Telegram/LLM не дёргаем (эмбеддинги задаём руками, `label_fn` инъектируем).

## Конвенции

- **Комментарии и docstring — на русском.** Системные сообщения бота тоже русские.
- Чистый `sqlite3` для tasks/bills (не SQLAlchemy). Pydantic-схемы планируются для
  intent-объектов — см. JARVIS_SPEC.md и скилл `pydantic-ai`.
- Тип-чек: Pyright LSP включён — следи за тем, чтобы правки проходили типизацию.
- Новый код держи в том же стиле, что окружающий (плотность комментариев, нейминг).

## Грабли (важно)

- **`GEMINI_API_KEY` нужен ВСЕГДА** — эмбеддинги памяти считаются через Gemini,
  даже если `LLM_PROVIDER` другой. Без ключа `main.py` падает на старте.
- **Не переключай `LLM_PROVIDER=ollama` на VPS** — 1GB RAM, локальная модель не влезет.
- **`.db`-файлы монтируются как bind-mount** (tasks/bills/actions/inbox/suggestions/
  contacts): хостовые файлы должны существовать ДО `docker compose up`, иначе Docker
  создаст на их месте каталоги. Новый стор → `touch <name>.db` + добавь mount в
  `docker-compose.yml` (иначе база эфемерна и теряется на пересборке).
- **У веб-интерфейса нет авторизации.** Слушает localhost; наружу — только через
  reverse-proxy с auth или SSH-туннель.
- `.env` не коммитим (в .gitignore). Шаблон — `.env.example`. Дефолт
  `GEMINI_MODEL=gemini-2.5-pro` в config.py — это просто дефолт репо, на VPS Flash.

## Установленный тулинг Claude Code (плагины)

Включены в этом окружении — используй когда подходит:

| Плагин | Что даёт | Когда |
|--------|----------|-------|
| **superpowers** | brainstorming, TDD, systematic-debugging, code-review и др. скиллы | процессные скиллы перед любой реализацией/отладкой |
| **pyright-lsp** | LSP-типизация Python | автоматически при правках .py |
| **pydantic-ai** | скилл `building-pydantic-ai-agents` | когда делаем intent-схемы / Pydantic AI агентов |
| **commit-commands** | `/commit`, `/commit-push-pr`, `/clean_gone` | git-воркфлоу (см. раздел Git) |
| **claude-md-management** | `/revise-claude-md`, скилл `claude-md-improver` | поддерживать этот файл в актуальном виде |
| **hookify** | `/hookify`, `/configure`, `/list` | автоматизировать поведение хуками |

**semgrep — намеренно отключён** пользователем (мешал в работе). Не включай обратно
без явной просьбы.

Правила работы со скиллами: при ≥1% шанса, что скилл подходит — вызывай его до
ответа. Процессные скиллы (brainstorming, systematic-debugging) идут первыми,
реализационные — вторыми. Инструкции пользователя важнее скиллов.

## Git

- На default-ветке (`main`) — сначала фича-ветка.
- **Авто-мердж (дефолт):** после реализации и зелёных тестов сразу commit → push →
  `merge --ff-only` в main → push, без ожидания явного ОК. Тесты красные или нет
  уверенности — остановись и спроси. Чтобы две ветки от одного main влились по
  `--ff-only`, стекай их: влить первую в main, вторую перебазировать на свежий main.
  Разовое «подожди» в чате на конкретном шаге перекрывает дефолт.
- Сообщения коммитов завершай строкой:
  `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`
