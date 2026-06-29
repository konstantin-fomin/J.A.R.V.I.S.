"""Тесты разбора намерений и маршрутизации после резолва (§(б)).

Цель: различать «это была команда, которую я не умею» и «это обычная болтовня».
В первом случае бот обязан честно отказать, а не уходить в chat-пайплайн —
иначе chat сконфабулирует подтверждение несделанного действия (см. кейс с
массовым созданием платежей: create_bill-интента нет, §3). Сеть/LLM не дёргаем —
llm.chat инъектируем фейком, отдающим заранее заданный «ответ модели».
"""
import config
from intents import parse_intent, route_after_resolve


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


# --- b1: системный промпт chat-пайплайна запрещает имитацию действий ----------

def test_system_prompt_forbids_claiming_actions():
    # Регрессионный замок: правило «не имитируй выполнение действий» должно
    # присутствовать в промпте chat-пайплайна (нормализуем пробелы — текст переносится).
    prompt = " ".join(config.SYSTEM_PROMPT.lower().split())
    assert "не имитируй выполнение" in prompt
    assert "не утверждай, что выполнил" in prompt
