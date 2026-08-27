#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

if [[ "$(id -u)" -eq 0 ]]; then
    SUDO=()
else
    SUDO=(sudo)
fi

"${SUDO[@]}" install -o root -g root -m 0644 \
    "${SCRIPT_DIR}/social-worker.service" \
    /etc/systemd/system/social-worker.service
"${SUDO[@]}" install -o root -g root -m 0644 \
    "${SCRIPT_DIR}/social-watchdog.service" \
    /etc/systemd/system/social-watchdog.service
"${SUDO[@]}" install -o root -g root -m 0644 \
    "${SCRIPT_DIR}/social-scheduler.service" \
    /etc/systemd/system/social-scheduler.service
"${SUDO[@]}" install -o root -g root -m 0644 \
    "${SCRIPT_DIR}/social-telegram-in.service" \
    /etc/systemd/system/social-telegram-in.service
"${SUDO[@]}" install -o root -g root -m 0644 \
    "${SCRIPT_DIR}/social-health-check.service" \
    /etc/systemd/system/social-health-check.service

"${SUDO[@]}" systemctl daemon-reload
"${SUDO[@]}" systemctl enable --now social-worker.service
"${SUDO[@]}" systemctl enable --now social-watchdog.service
"${SUDO[@]}" systemctl enable --now social-scheduler.service
"${SUDO[@]}" systemctl enable --now social-health-check.service
if grep -Eq '^TELEGRAM_BOT_TOKEN=.+$' "${SCRIPT_DIR}/../.env" 2>/dev/null; then
    "${SUDO[@]}" systemctl enable --now social-telegram-in.service
else
    echo "Telegram listener niet gestart: stel TELEGRAM_BOT_TOKEN in .env in."
fi

"${SUDO[@]}" systemctl --no-pager --full status \
    social-worker.service social-watchdog.service social-scheduler.service \
    social-health-check.service
