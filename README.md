# J.A.R.V.I.S. — личный AI-ассистент с памятью

Telegram-бот с долгосрочной памятью: мозги — локальная модель через Ollama
или облачная (Groq / Gemini / OpenRouter / OpenAI / Anthropic),
память — Markdown-файлы (совместимы с Obsidian), семантический поиск — ChromaDB.

## Что умеет

- Отвечает в Telegram через локальную модель или облачного провайдера
- Перед каждым ответом ищет похожие воспоминания в ChromaDB (top-5) и подмешивает их в контекст
- Все сообщения пишутся в журнал `journal/YYYY-MM-DD.md`
- **Сам извлекает факты из разговора** (фоном) и раскладывает по темам в `topics/*.md`
- `/plan` — план дня с учётом `goals.md` и журнала за последние 3 дня
- **Веб-интерфейс** на порту 8000: чат + просмотр файлов памяти
- Помнит последние 10 сообщений диалога
- `📓 текст` — запись в дневник без ответа модели
- `/memory` — список файлов памяти, `/forget <тема>` — удалить файл

## Веб-интерфейс

Поднимается вместе с ботом на `http://127.0.0.1:8000` (хост/порт — `WEB_HOST`/`WEB_PORT` в `.env`).
Чат работает с той же памятью, что и Telegram. Боковая панель показывает файлы памяти,
клик по файлу открывает его содержимое.

⚠️ На вебе нет авторизации. По умолчанию он слушает только localhost;
на VPS открывайте его наружу только через reverse-proxy с авторизацией,
либо ходите через SSH-туннель: `ssh -L 8000:localhost:8000 user@vps`.

## Деплой на VPS (Docker)

```bash
git clone https://github.com/konstantin-fomin/J.A.R.V.I.S.git
cd J.A.R.V.I.S
cp .env.example .env
nano .env          # вписать TELEGRAM_BOT_TOKEN, ALLOWED_USER_ID, провайдера и его ключ
docker compose up -d --build
```

Память бота появится в `./bot-memory`, индекс — в `./chroma_db`.

Полезные команды:

```bash
docker compose logs -f bot     # логи бота
docker compose restart bot     # перезапуск (например, после правки .env)
docker compose down            # остановить всё
```

**Про провайдера на VPS:** без GPU локальная модель будет очень медленной,
поэтому на сервере ставьте облачного провайдера (`LLM_PROVIDER=gemini` и т.п.).
Эмбеддинги для поиска по памяти считаются через Gemini API (`GEMINI_API_KEY`
нужен всегда — даже если для ответов используется другой провайдер).

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
2. Для `LLM_PROVIDER=ollama`: Ollama запущен и `ollama pull qwen2.5:7b` скачан
3. `.env` из `.env.example`; путь к памяти можно указать в свой Obsidian vault:
   `OBSIDIAN_VAULT_PATH=K:\OBSIDIAN\bot-memory`
4. Запуск: `.venv\Scripts\python main.py` (или `start_bot.bat`)

## Структура проекта

```
main.py                  # точка входа: проверки, запуск веба + Telegram-потока
config.py                # настройки из .env
bot/                     # Telegram: polling и обработчики команд
memory/                  # Obsidian-файлы + ChromaDB + менеджер + извлечение фактов
llm/                     # роутер провайдеров, клиент Ollama, эмбеддинги Gemini
web/                     # FastAPI-сервер + страница чата
Dockerfile               # образ бота
docker-compose.yml       # один сервис: bot (веб на 8000)
```
