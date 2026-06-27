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
bot/           Telegram: telegram_bot.py (polling) + handlers.py (команды)
llm/           ollama_client.py — роутер провайдеров (LLMClient.get_response) + gemini_embed
memory/        obsidian.py (файлы) + chroma.py (индекс) + manager.py + facts.py (автоэкстракция)
web/           server.py — FastAPI (чат + просмотр памяти)
intents.py     свободный текст → JSON-намерение через Gemini → действие
tasks.py       TaskStore (SQLite tasks.db)
bills.py       BillStore (SQLite bills.db)
```

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

# Тестов в репозитории сейчас нет. Если добавляешь фичу — пиши тесты (TDD-скилл).
```

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
- **`.db`-файлы монтируются как bind-mount** (tasks.db, bills.db): хостовые файлы
  должны существовать ДО `docker compose up`, иначе Docker создаст на их месте каталоги.
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
| **commit-commands** | `/commit`, `/commit-push-pr`, `/clean_gone` | git-воркфлоу (коммитить только по просьбе) |
| **claude-md-management** | `/revise-claude-md`, скилл `claude-md-improver` | поддерживать этот файл в актуальном виде |
| **hookify** | `/hookify`, `/configure`, `/list` | автоматизировать поведение хуками |

**semgrep — намеренно отключён** пользователем (мешал в работе). Не включай обратно
без явной просьбы.

Правила работы со скиллами: при ≥1% шанса, что скилл подходит — вызывай его до
ответа. Процессные скиллы (brainstorming, systematic-debugging) идут первыми,
реализационные — вторыми. Инструкции пользователя важнее скиллов.

## Git

- Коммить/пушь только по явной просьбе. На default-ветке (`main`) — сначала ветка.
- Сообщения коммитов завершай строкой:
  `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`
