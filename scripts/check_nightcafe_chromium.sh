#!/usr/bin/env bash
set -euo pipefail

URL="${NIGHTCAFE_CDP_HEALTH_URL:-http://127.0.0.1:${NIGHTCAFE_CDP_PORT:-9222}/json/version}"
ATTEMPTS="${NIGHTCAFE_CDP_HEALTH_ATTEMPTS:-30}"
DELAY="${NIGHTCAFE_CDP_HEALTH_DELAY:-1}"

for ((attempt=1; attempt<=ATTEMPTS; attempt++)); do
    if response="$(curl --fail --silent --show-error --max-time 2 "${URL}")"; then
        if grep -q '"Browser"' <<<"${response}"; then
            printf 'NightCafe Chromium CDP healthy: %s (attempt %d/%d)\n' "${URL}" "${attempt}" "${ATTEMPTS}"
            exit 0
        fi
    fi
    sleep "${DELAY}"
done

echo "NightCafe Chromium CDP health check failed: ${URL}" >&2
exit 1
