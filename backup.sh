#!/usr/bin/env bash
#
# backup.sh — суточный бэкап критичных данных VPS (раздел 4 JARVIS_SPEC.md).
#
# Складывает tar.gz в /root/backups/ с именем по дате и копией latest.tar.gz,
# чтобы внешнему скрипту (rsync/scp по Tailscale) не нужно было угадывать имя.
# Хранит последние 7 архивов, старые удаляет.
#
# Состав архива (в порядке приоритета, см. спеку §4):
#   1. vaultwarden  — данные пароль-менеджера (самое критичное на VPS)
#   2. amnezia      — VPN: awg2 (ключи WireGuard) + dns-forwarder (конфиг)
#   3. jarvis       — bot-memory/, tasks.db, bills.db, token.json,
#                     credentials.json, .env
#
# chroma_db НЕ бэкапим: это пересоздаваемый индекс эмбеддингов из bot-memory/.
#
# Запуск вручную:  /root/J.A.R.V.I.S./backup.sh
# Cron (раз в сутки ночью) — см. хвост этого файла.

set -uo pipefail

# --- настройки ----------------------------------------------------------------
BACKUP_DIR="/root/backups"
JARVIS_DIR="/root/J.A.R.V.I.S."
KEEP=7                                   # сколько архивов хранить
DATE="$(date +%F)"                       # YYYY-MM-DD
BACKUP_NAME="jarvis-backup-${DATE}"
ARCHIVE="${BACKUP_DIR}/${BACKUP_NAME}.tar.gz"
LATEST="${BACKUP_DIR}/latest.tar.gz"

WARNINGS=0

# Временный каталог для сборки дерева архива; чистится при любом выходе.
TMP="$(mktemp -d)"
STAGE="${TMP}/${BACKUP_NAME}"
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$STAGE"

# --- хелперы ------------------------------------------------------------------

# stage SRC DST — кладёт файл/каталог SRC в дерево архива под именем DST.
# Если SRC нет — предупреждает, но не валит весь бэкап.
stage() {
    local src="$1" dst="$2"
    if [ -e "$src" ]; then
        mkdir -p "$STAGE/$(dirname "$dst")"
        cp -a "$src" "$STAGE/$dst"
        echo "  + $dst"
    else
        echo "  ! пропущено (нет на диске): $src" >&2
        WARNINGS=$((WARNINGS + 1))
    fi
}

echo "=== Бэкап ${DATE} ==="

# --- 1. vaultwarden -----------------------------------------------------------
# Путь к данным резолвим через docker inspect (как с bind-mount'ами jarvis-bot-1),
# с фолбэком на стандартное расположение.
VW_DATA="$(docker inspect vaultwarden \
    --format '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Source}}{{end}}{{end}}' \
    2>/dev/null)"
[ -n "$VW_DATA" ] || VW_DATA="/opt/vaultwarden/vw-data"
stage "$VW_DATA" "vaultwarden/vw-data"

# --- 2. amnezia ---------------------------------------------------------------
# awg2: у контейнера нет host-bind, реальные конфиги и ключи WireGuard лежат
# ВНУТРИ контейнера в /opt/amnezia/awg — вытаскиваем через docker cp.
# (docker cp требует, чтобы родитель назначения уже существовал.)
mkdir -p "$STAGE/amnezia"
if docker cp amnezia-awg2:/opt/amnezia/awg "$STAGE/amnezia/awg2" 2>/dev/null; then
    echo "  + amnezia/awg2 (из контейнера)"
else
    echo "  ! пропущено: docker cp amnezia-awg2:/opt/amnezia/awg" >&2
    WARNINGS=$((WARNINGS + 1))
fi
# dns-forwarder: конфиг лежит на хосте (compose + dnsmasq).
stage "/opt/amnezia-dns-forwarder" "amnezia/dns-forwarder"

# --- 3. jarvis ----------------------------------------------------------------
stage "${JARVIS_DIR}/bot-memory"        "jarvis/bot-memory"
stage "${JARVIS_DIR}/tasks.db"          "jarvis/tasks.db"
stage "${JARVIS_DIR}/bills.db"          "jarvis/bills.db"
stage "${JARVIS_DIR}/token.json"        "jarvis/token.json"
stage "${JARVIS_DIR}/credentials.json"  "jarvis/credentials.json"
stage "${JARVIS_DIR}/.env"              "jarvis/.env"

# --- сборка архива ------------------------------------------------------------
mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"

tar -czf "$ARCHIVE" -C "$TMP" "$BACKUP_NAME"
chmod 600 "$ARCHIVE"

# Стабильное имя для внешнего потребителя (копия, не симлинк — переживает
# любой rsync/scp без -L и гарантированно отдаёт реальные данные).
cp -f "$ARCHIVE" "$LATEST"
chmod 600 "$LATEST"

SIZE="$(du -h "$ARCHIVE" | cut -f1)"
echo "Готово: $ARCHIVE ($SIZE) -> latest.tar.gz"

# --- ретеншн: оставить последние $KEEP ----------------------------------------
ls -1t "${BACKUP_DIR}"/jarvis-backup-*.tar.gz 2>/dev/null \
    | tail -n +$((KEEP + 1)) \
    | while read -r old; do
        echo "  - удаляю старый: $(basename "$old")"
        rm -f "$old"
    done

if [ "$WARNINGS" -gt 0 ]; then
    echo "ВНИМАНИЕ: $WARNINGS пропущенных источника(ов) — проверь вывод выше." >&2
    exit 1
fi
echo "=== Бэкап ${DATE} завершён без ошибок ==="

# --- Установка cron (один раз, вручную) ---------------------------------------
# Раз в сутки в 03:30, лог в /root/backups/backup.log:
#
#   ( crontab -l 2>/dev/null; \
#     echo '30 3 * * * /root/J.A.R.V.I.S./backup.sh >> /root/backups/backup.log 2>&1' \
#   ) | crontab -
