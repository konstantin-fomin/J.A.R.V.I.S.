"""Weekly review (§16): сводка за неделю.

Принцип: цифры считает Python (compute_week_stats — чистая агрегация над сторами,
без сети/LLM), а Gemini (compose_summary) только оборачивает уже готовые числа в
дружелюбный абзац. Так статистика гарантированно точная, без галлюцинаций.
"""
from __future__ import annotations

import json
from datetime import date, timedelta

from contacts import days_until_birthday


def compute_week_stats(start: date, end: date, *, tasks, bills, contacts, reads, log) -> dict:
    """Агрегирует факты из существующих сторов за неделю [start, end]. Чистый
    Python над SQLite — никакого LLM, числа точные. Возвращает структуру чисел и
    коротких списков."""
    # --- задачи: выполнено за неделю + просрочено на сейчас (end) ---
    completed_titles, overdue_titles = [], []
    if tasks is not None:
        for t in tasks.list(status="done"):
            done_day = (t["updated_at"] or "")[:10]
            if start.isoformat() <= done_day <= end.isoformat():
                completed_titles.append(t["title"])
        for t in tasks.list(status="todo"):
            if t["due_date"] and t["due_date"] < end.isoformat():
                overdue_titles.append(t["title"])

    # --- платежи: оплачено/ожидает в месяце end ---
    month = end.strftime("%Y-%m")
    paid = pending = 0
    pending_names: list[str] = []
    if bills is not None:
        bills.ensure_month(month)
        for b in bills.list_instances(month):
            if b["status"] == "paid":
                paid += 1
            else:
                pending += 1
                pending_names.append(b["name"])

    # --- дни рождения в ближайшие 7 дней от end ---
    birthdays = []
    if contacts is not None:
        for c in contacts.upcoming_birthdays(within_days=7, today=end):
            birthdays.append({
                "name": c["name"],
                "birthday": c["birthday"],
                "in_days": days_until_birthday(date.fromisoformat(c["birthday"]), end),
            })

    # --- очередь «почитать» ---
    reads_unread = len(reads.list("unread")) if reads is not None else 0

    # --- действия за неделю по типам ---
    actions: dict[str, int] = {}
    if log is not None:
        window_end = (end + timedelta(days=1)).isoformat()  # включаем весь день end
        for a in log.actions_between(start.isoformat(), window_end):
            key = f"{a['entity_type']}.{a['action']}"
            actions[key] = actions.get(key, 0) + 1

    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "tasks": {
            "completed": len(completed_titles), "completed_titles": completed_titles,
            "overdue": len(overdue_titles), "overdue_titles": overdue_titles,
        },
        "bills": {"month": month, "paid": paid, "pending": pending, "pending_names": pending_names},
        "birthdays": birthdays,
        "reads_unread": reads_unread,
        "actions": actions,
        "actions_total": sum(actions.values()),
    }


_SUMMARY_PROMPT = """Ты пишешь короткое еженедельное ревью для пользователя. Ниже —
УЖЕ ПОСЧИТАННЫЕ цифры за неделю в формате JSON. Напиши дружелюбный связный абзац
(3–5 предложений, по-русски), который обыгрывает эти числа и слегка подбадривает.

СТРОГО:
- Используй ТОЛЬКО числа и факты из JSON. Ничего не добавляй, не выдумывай и не
  досчитывай самостоятельно.
- Не упоминай данных, которых нет в JSON.
- Без markdown, без списков и заголовков — просто абзац текста.

Данные за неделю ({start} — {end}):
{stats_json}"""


def compose_summary(llm, stats: dict) -> str:
    """ОДИН вызов LLM: оборачивает готовый stats в дружелюбный абзац. llm — объект
    с .chat(messages). Промпт ограничивает модель только переданными числами."""
    prompt = _SUMMARY_PROMPT.format(
        start=stats["start"], end=stats["end"],
        stats_json=json.dumps(stats, ensure_ascii=False, indent=2),
    )
    return llm.chat([{"role": "user", "content": prompt}]).strip()


def format_review(stats: dict) -> str:
    """Детерминированный рендер тех же чисел — фолбэк, если LLM недоступен."""
    t, b = stats["tasks"], stats["bills"]
    lines = [f"🗓 Итоги недели {stats['start']} — {stats['end']}", ""]
    lines.append(f"✅ Выполнено задач: {t['completed']}")
    if t["overdue"]:
        lines.append(f"⏰ Просрочено: {t['overdue']}")
    lines.append(f"💳 Платежи {b['month']}: оплачено {b['paid']}, ждёт {b['pending']}")
    if stats["birthdays"]:
        names = ", ".join(f"{x['name']} (через {x['in_days']} дн)" for x in stats["birthdays"])
        lines.append(f"🎂 Скоро дни рождения: {names}")
    lines.append(f"📑 В «почитать»: {stats['reads_unread']}")
    if stats["actions_total"]:
        lines.append(f"✍️ Действий за неделю: {stats['actions_total']}")
    return "\n".join(lines)
