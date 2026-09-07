#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
CORE_HOME="${CORE_HOME:-$(cd "$SCRIPT_DIR/.." && pwd)}"
CORE_DATA="${CORE_DATA:-${CORE_HOME}/runtime}"
CORE_USER="${CORE_USER:-${SUDO_USER:-${USER}}}"
CORE_GROUP="${CORE_GROUP:-$(id -gn "$CORE_USER")}"
CORE_PYTHON="${CORE_PYTHON:-${CORE_HOME}/venv/bin/python3}"

if [[ "$(id -u)" -eq 0 ]]; then
    SUDO=()
else
    SUDO=(sudo)
fi

render_unit() {
    local source="$1" target="$2" rendered
    rendered="$(mktemp)"
    sed -e "s#@CORE_HOME@#${CORE_HOME}#g" \
        -e "s#@CORE_DATA@#${CORE_DATA}#g" \
        -e "s#@CORE_USER@#${CORE_USER}#g" \
        -e "s#@CORE_GROUP@#${CORE_GROUP}#g" \
        -e "s#@CORE_PYTHON@#${CORE_PYTHON}#g" "$source" > "$rendered"
    "${SUDO[@]}" install -o root -g root -m 0644 "$rendered" "$target"
    rm -f "$rendered"
}

render_unit "${SCRIPT_DIR}/social-worker.service" /etc/systemd/system/social-worker.service
render_unit "${SCRIPT_DIR}/social-watchdog.service" /etc/systemd/system/social-watchdog.service
for unit in social-scheduler social-telegram-in social-health-check social-rag-indexer social-nightcafe social-gdrive-sync; do
    render_unit "${SCRIPT_DIR}/${unit}.service" "/etc/systemd/system/${unit}.service"
done
for timer in social-nightcafe social-gdrive-sync; do
    render_unit "${SCRIPT_DIR}/${timer}.timer" "/etc/systemd/system/${timer}.timer"
done

"${SUDO[@]}" systemctl daemon-reload
"${SUDO[@]}" systemctl enable --now social-worker.service
"${SUDO[@]}" systemctl enable --now social-watchdog.service
"${SUDO[@]}" systemctl enable --now social-scheduler.service
"${SUDO[@]}" systemctl enable --now social-health-check.service
"${SUDO[@]}" systemctl enable --now social-rag-indexer.service
"${SUDO[@]}" systemctl enable --now social-nightcafe.timer
"${SUDO[@]}" systemctl enable --now social-gdrive-sync.timer
if grep -Eq '^TELEGRAM_BOT_TOKEN=.+$' "${SCRIPT_DIR}/../.env" 2>/dev/null; then
    "${SUDO[@]}" systemctl enable --now social-telegram-in.service
else
    echo "Telegram listener niet gestart: stel TELEGRAM_BOT_TOKEN in .env in."
fi

"${SUDO[@]}" systemctl --no-pager --full status \
    social-worker.service social-watchdog.service social-scheduler.service \
    social-health-check.service social-rag-indexer.service
