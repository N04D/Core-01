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

"${SUDO[@]}" systemctl daemon-reload
"${SUDO[@]}" systemctl enable --now social-worker.service
"${SUDO[@]}" systemctl enable --now social-watchdog.service

"${SUDO[@]}" systemctl --no-pager --full status \
    social-worker.service social-watchdog.service
