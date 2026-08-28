#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="/home/infra/dev"
PROFILE_DIR="${NIGHTCAFE_CHROME_PROFILE:-${PROJECT_ROOT}/vault/chrome_profile}"
CDP_PORT="${NIGHTCAFE_CDP_PORT:-9222}"
HEADLESS="${NIGHTCAFE_HEADLESS:-new}"

HEADLESS_ARGS=()
case "${HEADLESS}" in
    0|false|no|off) ;;
    1|true|yes|on|new) HEADLESS_ARGS=(--headless=new) ;;
    *) HEADLESS_ARGS=("--headless=${HEADLESS}") ;;
esac

mkdir -p "${PROFILE_DIR}"
chmod 700 "${PROFILE_DIR}"

if [[ -n "${NIGHTCAFE_CHROMIUM_BIN:-}" ]]; then
    CHROMIUM_BIN="${NIGHTCAFE_CHROMIUM_BIN}"
elif command -v chromium >/dev/null 2>&1; then
    CHROMIUM_BIN="$(command -v chromium)"
elif command -v chromium-browser >/dev/null 2>&1; then
    CHROMIUM_BIN="$(command -v chromium-browser)"
elif command -v google-chrome >/dev/null 2>&1; then
    CHROMIUM_BIN="$(command -v google-chrome)"
elif [[ -x "${PROJECT_ROOT}/venv/bin/python3" ]]; then
    CHROMIUM_BIN="$(${PROJECT_ROOT}/venv/bin/python3 -c 'from playwright.sync_api import sync_playwright; p=sync_playwright().start(); print(p.chromium.executable_path); p.stop()')"
else
    echo "No Chromium executable found" >&2
    exit 127
fi

[[ -x "${CHROMIUM_BIN}" ]] || { echo "Chromium is not executable: ${CHROMIUM_BIN}" >&2; exit 126; }

exec "${CHROMIUM_BIN}" \
    --remote-debugging-address=127.0.0.1 \
    "--remote-debugging-port=${CDP_PORT}" \
    --user-data-dir="${PROFILE_DIR}" \
    --no-first-run \
    --no-default-browser-check \
    --disable-session-crashed-bubble \
    --disable-dev-shm-usage \
    --enable-gpu \
    --ignore-gpu-blocklist \
    --use-gl=egl \
    --window-size=1365,768 \
    "${HEADLESS_ARGS[@]}" \
    "${NIGHTCAFE_CHROMIUM_EXTRA_ARGS:-about:blank}"
