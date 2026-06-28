"""Настройки бота: пути, токены, модели."""
import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

# Путь к папке памяти (создаётся автоматически). По умолчанию ./bot-memory
# рядом с кодом; локально можно указать папку внутри Obsidian vault через .env
OBSIDIAN_VAULT_PATH = Path(os.getenv("OBSIDIAN_VAULT_PATH") or (BASE_DIR / "bot-memory"))

# Провайдер LLM: ollama | groq | gemini | openrouter | openai | anthropic
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "ollama").strip().lower()
# Потолок длины ответа для облачных провайдеров (в токенах)
LLM_MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS") or 4096)

# Ollama (только если LLM_PROVIDER=ollama — для облачных провайдеров не нужен)
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:7b")

# Облачные провайдеры
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.1-70b-versatile")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-pro")

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "openrouter/auto")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o")

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")

# Веб-интерфейс. Хост по умолчанию локальный: на веб нет авторизации,
# наружу его можно открывать только осознанно (в Docker — 0.0.0.0 внутри сети)
WEB_HOST = os.getenv("WEB_HOST", "127.0.0.1")
WEB_PORT = int(os.getenv("WEB_PORT") or 8000)

# Telegram
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
# Если задан — бот отвечает только этому пользователю (его Telegram user id)
ALLOWED_USER_ID = int(os.getenv("ALLOWED_USER_ID") or 0) or None

# ChromaDB
CHROMA_PERSIST_DIR = str(BASE_DIR / "chroma_db")

# Задачи (SQLite, отдельно от памяти)
TASKS_DB_PATH = Path(os.getenv("TASKS_DB_PATH") or (BASE_DIR / "tasks.db"))

# Платежи (SQLite, отдельно от памяти и задач)
BILLS_DB_PATH = Path(os.getenv("BILLS_DB_PATH") or (BASE_DIR / "bills.db"))

# Журнал действий для отмены (undo_last). SQLite, отдельно от tasks/bills.
ACTION_LOG_DB_PATH = Path(os.getenv("ACTION_LOG_DB_PATH") or (BASE_DIR / "actions.db"))

# Инбокс (быстрый захват). SQLite, отдельно от остальных.
INBOX_DB_PATH = Path(os.getenv("INBOX_DB_PATH") or (BASE_DIR / "inbox.db"))

# Google Calendar (опционально). Бот работает и без настроенного календаря:
# нет credentials/token → load_calendar() возвращает None, фичи просто отключены.
# token.json генерируется отдельным скриптом generate_calendar_token.py на машине
# с браузером (см. JARVIS_SPEC.md §9) — не на этом headless VPS.
CALENDAR_CREDENTIALS_PATH = Path(os.getenv("CALENDAR_CREDENTIALS_PATH") or (BASE_DIR / "credentials.json"))
CALENDAR_TOKEN_PATH = Path(os.getenv("CALENDAR_TOKEN_PATH") or (BASE_DIR / "token.json"))
# Таймзона встреч: в ней создаются и сравниваются события
CALENDAR_TIMEZONE = os.getenv("CALENDAR_TIMEZONE", "Europe/Moscow")
# За сколько минут до встречи напоминать и как часто проверять календарь (секунды)
CALENDAR_REMINDER_LEAD_MINUTES = int(os.getenv("CALENDAR_REMINDER_LEAD_MINUTES") or 15)
CALENDAR_REMINDER_INTERVAL = int(os.getenv("CALENDAR_REMINDER_INTERVAL") or 300)

# Память
MAX_MEMORY_RESULTS = 5       # сколько воспоминаний подгружать
MAX_HISTORY_MESSAGES = 10    # сколько последних сообщений хранить в контексте

SYSTEM_PROMPT = """Ты — личный AI-ассистент. Говоришь только на русском языке.
Ты помнишь пользователя и его жизнь благодаря записям в памяти.
Отвечай как умный, дружелюбный помощник который хорошо знает пользователя.
Будь краток если вопрос простой. Развёрнуто — если нужно подумать.

Текущая дата: {date}

Что я знаю о тебе из памяти:
{memory_context}"""
