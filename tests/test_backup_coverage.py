"""Системная защита (§4): backup.sh обязан бэкапить КАЖДУЮ .db, смонтированную
в docker-compose.yml, и не выдумывать лишних.

Завели новый стор — добавили bind-mount в docker-compose.yml, но забыли строку
stage в backup.sh? Этот тест краснеет, превращая тихую человеческую ошибку
(«стор есть, в бэкапе нет — данные невосстановимы») в красный CI на том же коммите.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _dbs_in_compose() -> set[str]:
    """Имена *.db, реально смонтированных в контейнер (host-side bind-mount).

    Матчим только строки-маунты вида `- ./tasks.db:/app/tasks.db`, поэтому
    упоминания .db в комментариях (например, подсказка `touch …`) не цепляются."""
    text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    return set(re.findall(r"\./([\w-]+\.db):", text))


def _dbs_in_backup() -> set[str]:
    """Имена *.db, которые backup.sh кладёт в архив. Берём только строки `stage …`,
    чтобы .db из комментариев в шапке скрипта не давали ложного совпадения."""
    text = (ROOT / "backup.sh").read_text(encoding="utf-8")
    staged: set[str] = set()
    for line in text.splitlines():
        if line.strip().startswith("stage "):
            staged.update(re.findall(r"([\w-]+\.db)", line))
    return staged


def test_backup_covers_all_mounted_dbs():
    mounted = _dbs_in_compose()
    backed = _dbs_in_backup()
    assert mounted, "не нашли ни одной .db в docker-compose.yml — сломан парсер?"

    missing = mounted - backed   # смонтировано, но не бэкапится — главная опасность
    extra = backed - mounted     # бэкапим то, чего нет среди маунтов — рассинхрон
    assert not missing, (
        f"backup.sh не бэкапит смонтированные базы: {sorted(missing)}. "
        "Добавь для них строки `stage` в backup.sh (§4)."
    )
    assert not extra, (
        f"backup.sh бэкапит .db, которых нет среди bind-mount'ов: {sorted(extra)}. "
        "Либо стор больше не монтируется, либо опечатка в имени."
    )
