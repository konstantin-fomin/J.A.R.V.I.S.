"""§20 write-путь для contacts.email: задать почту контакту через интент.

Раньше email только читался (матч attendees↔контакт), но ни create_contact, ни
update_contact его не писали — фича была недостижима в реальном использовании.
Эти тесты фиксируют, что почту можно задать словами боту, и что после этого
секция контакта в pre-meeting напоминании реально срабатывает (end-to-end).

Сеть/Telegram/LLM не дёргаем — строим intent-data руками, как их эмитит парсер.
"""
import datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Moscow")


def make_router(tmp_path):
    from contacts import ContactStore
    from intents import IntentRouter
    from logger import ActionLog
    from obligations import ObligationStore
    from tasks import TaskStore

    cs = ContactStore(tmp_path / "contacts.db")
    obs = ObligationStore(tmp_path / "obl.db")
    ts = TaskStore(tmp_path / "tasks.db")
    log = ActionLog(tmp_path / "log.db")
    router = IntentRouter(tasks=ts, bills=None, action_log=log,
                          contacts=cs, obligations=obs)
    return router, cs


def run(router, intent_data):
    """resolve -> execute, как в bot/handlers.py."""
    res = router.resolve(intent_data)
    assert res.kind == "execute", f"ожидали execute, получили {res.kind}: {getattr(res, 'text', '')}"
    return router.execute(res.action)


# ── промпт обязан рассказать LLM про email ───────────────────────────────────

def test_prompt_documents_email_field():
    """Парсер не извлечёт то, о чём не знает: PROMPT должен упоминать email."""
    from intents import PROMPT
    assert "email" in PROMPT.lower()


# ── create_contact с email ───────────────────────────────────────────────────

def test_create_contact_stores_email(tmp_path):
    router, cs = make_router(tmp_path)
    run(router, {"intent": "create_contact", "name": "Аня",
                 "email": "anya@example.com"})
    contact = cs.find("Аня")[0]
    assert contact["email"] == "anya@example.com"
    assert cs.find_by_email("anya@example.com") is not None


def test_create_contact_response_shows_email(tmp_path):
    router, cs = make_router(tmp_path)
    reply = run(router, {"intent": "create_contact", "name": "Аня",
                         "email": "anya@example.com"})
    assert "anya@example.com" in reply


# ── update_contact задаёт/меняет email существующему ─────────────────────────

def test_update_contact_sets_email(tmp_path):
    router, cs = make_router(tmp_path)
    cs.create(name="Пётр")  # без email
    run(router, {"intent": "update_contact", "name_hint": "Пётр",
                 "email": "petr@example.com"})
    assert cs.find("Пётр")[0]["email"] == "petr@example.com"


# ── невалидный email: контакт создаётся, email не пишется, ответ помечает ─────

def test_invalid_email_creates_contact_without_email_and_flags(tmp_path):
    router, cs = make_router(tmp_path)
    reply = run(router, {"intent": "create_contact", "name": "Аня",
                         "email": "не-почта"})
    contact = cs.find("Аня")[0]
    assert contact["email"] is None          # мусор не сохранили
    assert "распозна" in reply.lower()       # но честно сказали
    assert "не-почта" in reply               # и показали что именно


# ── end-to-end: то, что раньше было недостижимо ──────────────────────────────

def test_email_via_intent_enables_premeeting_section(tmp_path):
    """Контакт с email, заданным ЧЕРЕЗ ИНТЕНТ → секция контакта в напоминании."""
    from bot.telegram_bot import build_reminder_text

    router, cs = make_router(tmp_path)
    run(router, {"intent": "create_contact", "name": "Аня",
                 "email": "anya@example.com"})

    now = datetime.datetime(2026, 6, 29, 15, 0, tzinfo=TZ)
    event = {
        "id": "e1", "title": "Созвон с Аней",
        "start": now, "end": now + datetime.timedelta(hours=1),
        "description": "", "attendees": ["anya@example.com"],
    }
    text = build_reminder_text(event, 15, memory=None, contacts=cs)
    assert "Аня" in text
    assert "Контакт" in text


# ── детерминированный фолбэк: реальный Gemini Flash нестабильно извлекает email,
#    поэтому parse_intent достаёт его из текста регуляркой, если модель промолчала ──

class FakeLLM:
    """LLM-заглушка: возвращает заранее заданный JSON, сеть не трогаем."""
    def __init__(self, payload: dict):
        self._payload = payload

    def chat(self, messages):
        import json
        return json.dumps(self._payload)


def test_parse_intent_recovers_email_from_text_when_llm_omits():
    from intents import parse_intent
    llm = FakeLLM({"intent": "create_contact", "name": "Пётр"})  # без email
    data = parse_intent(llm, "добавь контакт Пётр, email petr@work.io")
    assert data.get("email") == "petr@work.io"


def test_parse_intent_strips_trailing_punctuation_from_found_email():
    from intents import parse_intent
    llm = FakeLLM({"intent": "create_contact", "name": "Пётр"})
    data = parse_intent(llm, "запомни: Пётр, email petr.ivanov@work.io, др 1990-03-12")
    assert data.get("email") == "petr.ivanov@work.io"


def test_parse_intent_keeps_llm_email_over_text():
    from intents import parse_intent
    llm = FakeLLM({"intent": "create_contact", "name": "Аня", "email": "real@x.com"})
    data = parse_intent(llm, "добавь Аня, почта other@y.com")
    assert data.get("email") == "real@x.com"  # явное от модели приоритетнее


def test_parse_intent_no_email_fallback_for_non_contact_intent():
    from intents import parse_intent
    llm = FakeLLM({"intent": "create_task", "title": "написать на a@b.com"})
    data = parse_intent(llm, "напомни написать на a@b.com")
    assert data.get("email") is None  # для не-контактных интентов почту не вытаскиваем


def test_parse_intent_no_email_when_text_has_none():
    from intents import parse_intent
    llm = FakeLLM({"intent": "create_contact", "name": "Вася"})
    data = parse_intent(llm, "добавь контакт Вася")
    assert data.get("email") in (None, "")
