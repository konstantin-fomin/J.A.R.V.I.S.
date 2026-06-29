"""Тесты разбора намерений и маршрутизации после резолва (§(б)).

Цель: различать «это была команда, которую я не умею» и «это обычная болтовня».
В первом случае бот обязан честно отказать, а не уходить в chat-пайплайн —
иначе chat сконфабулирует подтверждение несделанного действия (см. кейс с
массовым созданием платежей: create_bill-интента нет, §3). Сеть/LLM не дёргаем —
llm.chat инъектируем фейком, отдающим заранее заданный «ответ модели».
"""
import config
from intents import PROMPT, guard_chat_answer, parse_intent, route_after_resolve


class FakeLLM:
    """Фейковый клиент: .chat() возвращает заранее заданную строку-ответ модели."""

    def __init__(self, response: str):
        self._response = response

    def chat(self, *_):  # содержимое промпта здесь не важно
        return self._response


TODAY = "2026-06-29"


# --- parse_intent: распознанная команда проходит как обычно ------------------

def test_valid_intent_passes_through_without_unrecognized():
    llm = FakeLLM('{"intent": "create_task", "confidence": "high", "title": "купить молоко"}')
    data = parse_intent(llm, "купи молоко", TODAY)
    assert data["intent"] == "create_task"
    assert not data.get("unrecognized")


# --- parse_intent: обычная болтовня — это none, НЕ unrecognized ---------------

def test_genuine_none_is_plain_chitchat():
    # Модель сама решила, что это разговор, и вернула none — это ожидаемый chat.
    llm = FakeLLM('{"intent": "none", "confidence": "high"}')
    data = parse_intent(llm, "как настроение?", TODAY)
    assert data["intent"] == "none"
    assert not data.get("unrecognized")


def test_unparseable_model_output_is_chitchat_not_unrecognized():
    # Модель вернула мусор (не JSON) — это сбой парсинга, не «несделанная команда».
    # Безопаснее увести в chat, а не отказывать пользователю.
    llm = FakeLLM("извините, я не поняла")
    data = parse_intent(llm, "что-то непонятное", TODAY)
    assert data["intent"] == "none"
    assert not data.get("unrecognized")


def test_missing_intent_key_is_chitchat_not_unrecognized():
    llm = FakeLLM('{"confidence": "high"}')
    data = parse_intent(llm, "привет", TODAY)
    assert data["intent"] == "none"
    assert not data.get("unrecognized")


# --- parse_intent: нераспознанное имя intent = попытка несуществующей команды -

def test_unknown_intent_name_is_flagged_unrecognized():
    # Модель попыталась выдать команду, которой нет в наборе (как create_bill для
    # массового создания платежей) — это НЕ болтовня, помечаем unrecognized.
    llm = FakeLLM('{"intent": "create_bill", "confidence": "high"}')
    data = parse_intent(llm, "запиши платежи в июле …", TODAY)
    assert data["intent"] == "none"
    assert data["unrecognized"] is True


def test_empty_intent_string_is_not_unrecognized():
    llm = FakeLLM('{"intent": "", "confidence": "high"}')
    data = parse_intent(llm, "…", TODAY)
    assert data["intent"] == "none"
    assert not data.get("unrecognized")


# --- route_after_resolve: куда направить сообщение после резолва --------------

def test_route_non_chat_kinds_go_to_handle():
    assert route_after_resolve({"intent": "create_task"}, "execute") == "handle"
    assert route_after_resolve({"intent": "create_event"}, "confirm") == "handle"
    assert route_after_resolve({"intent": "complete_task"}, "message") == "handle"


def test_route_genuine_none_goes_to_chat():
    assert route_after_resolve({"intent": "none"}, "chat") == "chat"


def test_route_unrecognized_goes_to_refuse():
    # Нераспознанная команда при kind=chat → честный отказ, не chat-пайплайн.
    assert route_after_resolve({"intent": "none", "unrecognized": True}, "chat") == "refuse"


# --- (2) is_action_request: явный сигнal «просьба о действии без подходящего intent»
# Реальный Gemini для «запиши платежи …» отдаёт чистый none (НЕ выдуманное имя),
# поэтому unrecognized не срабатывает. Просим модель явно поднять флаг.

def test_action_request_none_is_surfaced():
    llm = FakeLLM('{"intent": "none", "is_action_request": true}')
    data = parse_intent(llm, "запиши платежи в июле …", TODAY)
    assert data["intent"] == "none"
    assert data["is_action_request"] is True


def test_plain_none_has_no_action_request():
    llm = FakeLLM('{"intent": "none"}')
    data = parse_intent(llm, "как настроение?", TODAY)
    assert data["is_action_request"] is False


def test_action_request_string_false_is_normalized():
    # Модель могла вернуть строку "false" — это не действие.
    llm = FakeLLM('{"intent": "none", "is_action_request": "false"}')
    data = parse_intent(llm, "привет", TODAY)
    assert data["is_action_request"] is False


def test_route_action_request_goes_to_refuse():
    assert route_after_resolve({"intent": "none", "is_action_request": True}, "chat") == "refuse"


def test_intent_prompt_requests_action_flag():
    # Замок: промпт парсера должен запрашивать сигнал is_action_request.
    prompt = " ".join(PROMPT.lower().split())
    assert "is_action_request" in prompt


# --- (1) guard_chat_answer: детерминированная последняя линия защиты ----------
# Заменяет ответ честным отказом, если модель УТВЕРЖДАЕТ, что выполнила действие
# со стором, которого на chat-пути быть не может. Срабатывает независимо от (2).

def test_guard_replaces_record_claim():
    answer = "Отлично, я записал твои платежи на июль. Я напомню тебе о них ближе к срокам."
    out = guard_chat_answer(answer)
    assert out != answer
    assert "не могу" in out.lower()


def test_guard_catches_various_action_claims():
    claims = [
        "Я создал задачу купить молоко.",
        "Готово, добавил встречу в календарь.",
        "Я отметил платёж оплаченным.",
        "Занёс это в список дел.",
        "Сохранил в заметки, не потеряешь.",
        "Я запланировал напоминание на завтра.",
        "Перенёс встречу на пятницу.",
        "Удалил задачу, как просил.",
        "Я напомню тебе о платеже ближе к сроку.",
    ]
    for c in claims:
        assert guard_chat_answer(c) != c, f"guard пропустил имитацию: {c!r}"


def test_guard_passes_honest_refusal_with_infinitives():
    # «не могу записать/создать» — инфинитивы, это честный отказ, НЕ имитация.
    txt = ("Я не могу записать сразу несколько платежей. "
           "Могу помочь записать их по одному или внести вручную.")
    assert guard_chat_answer(txt) == txt


def test_guard_passes_legit_memory_talk():
    # Болтовня про память/предпочтения: бот реально мог сохранить через facts-экстрактор
    # (другой путь). Не режем. Также рhеторическое «напомню, что …» — это не обещание.
    legit = [
        "Я запомнил, что тебе нравится джаз.",
        "Буду иметь в виду, что ты не пьёшь кофе.",
        "Хорошо, учту это на будущее.",
        "Напомню, что вчера ты говорил про отпуск в июле.",
        "Ты уже добавил это в свой список — отличная идея.",
    ]
    for ok in legit:
        assert guard_chat_answer(ok) == ok, f"guard ложно сработал: {ok!r}"


# --- b1: системный промпт chat-пайплайна запрещает имитацию действий ----------

def test_system_prompt_forbids_claiming_actions():
    # Регрессионный замок: правило «не имитируй выполнение действий» должно
    # присутствовать в промпте chat-пайплайна (нормализуем пробелы — текст переносится).
    prompt = " ".join(config.SYSTEM_PROMPT.lower().split())
    assert "не имитируй выполнение" in prompt
    assert "не утверждай, что выполнил" in prompt
