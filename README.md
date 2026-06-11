# J.A.R.V.I.S. — личный AI-ассистент с памятью

Telegram-бот с долгосрочной памятью: мозги — локальная модель через Ollama
или облачная (Groq / Gemini / OpenRouter / OpenAI / Anthropic),
память — Markdown-файлы (совместимы с Obsidian), семантический поиск — ChromaDB.

## Что умеет

- Отвечает в Telegram через локальную модель или облачного провайдера
- Перед каждым ответом ищет похожие воспоминания в ChromaDB (top-5) и подмешивает их в контекст
- Все сообщения пишутся в журнал `journal/YYYY-MM-DD.md`
- Помнит последние 10 сообщений диалога
- `📓 текст` — запись в дневник без ответа модели
- `/memory` — список файлов памяти, `/forget <тема>` — удалить файл

## Деплой на VPS (Docker)

```bash
git clone https://github.com/konstantin-fomin/J.A.R.V.I.S.git
cd J.A.R.V.I.S
cp .env.example .env
nano .env          # вписать TELEGRAM_BOT_TOKEN, ALLOWED_USER_ID, провайдера и его ключ
docker compose up -d --build
```

При первом запуске compose сам скачает модель эмбеддингов bge-m3 (~1.2 ГБ)
в контейнер Ollama. Память бота появится в `./bot-memory`, индекс — в `./chroma_db`.

Полезные команды:

```bash
docker compose logs -f bot     # логи бота
docker compose restart bot     # перезапуск (например, после правки .env)
docker compose down            # остановить всё
```

**Про провайдера на VPS:** без GPU локальная модель будет очень медленной,
поэтому на сервере ставьте облачного провайдера (`LLM_PROVIDER=gemini` и т.п.).
Ollama в compose нужен всегда — он считает эмбеддинги (bge-m3) для поиска
по памяти, это быстро и на CPU. Если всё же хотите локальные ответы:

```bash
docker compose exec ollama ollama pull qwen2.5:7b
```

## Переключение LLM-провайдера

В `.env` задаётся `LLM_PROVIDER`: `ollama` | `groq` | `gemini` | `openrouter` | `openai` | `anthropic`.
Для облачного провайдера нужен его API-ключ (`GROQ_API_KEY`, `GEMINI_API_KEY`, ...)
и опционально модель (`GROQ_MODEL`, `GEMINI_MODEL`, ...). Пример:

```
LLM_PROVIDER=gemini
GEMINI_API_KEY=...
GEMINI_MODEL=gemini-2.5-flash
```

После правки `.env` достаточно перезапустить бота — код менять не нужно.
Весь роутинг — в `llm/ollama_client.py` (`LLMClient.get_response`).

## Локальный запуск (Windows, без Docker)

1. Зависимости: `python -m venv .venv && .venv\Scripts\pip install -r requirements.txt`
2. Ollama запущен, модели скачаны: `ollama pull bge-m3` (+ `qwen2.5:7b` для локальных ответов)
3. `.env` из `.env.example`; путь к памяти можно указать в свой Obsidian vault:
   `OBSIDIAN_VAULT_PATH=K:\OBSIDIAN\bot-memory`
4. Запуск: `.venv\Scripts\python main.py` (или `start_bot.bat`)

## Структура проекта

```
main.py                  # точка входа: проверки, синхронизация памяти, запуск бота
config.py                # настройки из .env
bot/                     # Telegram: polling и обработчики команд
memory/                  # Obsidian-файлы + ChromaDB + менеджер памяти
llm/                     # роутер провайдеров и клиент Ollama
Dockerfile               # образ бота
docker-compose.yml       # bot + ollama (эмбеддинги)
```

## v2 (планы)

- Автоматическое извлечение фактов после разговора
- Веб-интерфейс (FastAPI + HTML)
- Команда `/plan`
- Умное создание тематических файлов в `topics/`
