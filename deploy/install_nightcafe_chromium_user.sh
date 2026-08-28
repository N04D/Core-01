#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="/home/infra/dev"
UNIT_DIR="${XDG_CONFIG_HOME:-${HOME}/.config}/systemd/user"
mkdir -p "${UNIT_DIR}"
install -m 0644 "${PROJECT_ROOT}/deploy/nightcafe-chromium.service" "${UNIT_DIR}/nightcafe-chromium.service"
systemctl --user daemon-reload
systemctl --user enable --now nightcafe-chromium.service
systemctl --user --no-pager --full status nightcafe-chromium.service
