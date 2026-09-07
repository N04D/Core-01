#!/usr/bin/env bash
set -e

# Platform-neutral bootstrap: ARM64, x86_64, CPU-only and NVIDIA hosts share
# the base install; optional GPU/model capabilities are detected below.
ARCH="$(uname -m)"
case "$ARCH" in
    aarch64|arm64|x86_64|amd64) echo "Detected supported architecture: $ARCH" ;;
    *) echo "Warning: untested architecture $ARCH; continuing with generic Linux setup." >&2 ;;
esac

if [[ "$(id -u)" -eq 0 ]]; then
    SUDO=()
else
    SUDO=(sudo)
fi

export DEBIAN_FRONTEND=noninteractive
"${SUDO[@]}" apt-get update
"${SUDO[@]}" apt-get install -y \
    flatpak \
    ca-certificates \
    python3-pip \
    python3-yaml \
    python3-requests \
    python3-dotenv \
    python3-venv

flatpak remote-add --user --if-not-exists \
    flathub \
    https://flathub.org/repo/flathub.flatpakrepo
flatpak install --user --noninteractive --or-update \
    flathub \
    md.obsidian.Obsidian

# Runtime data defaults to a repository-local directory. Production installs
# should export CORE_DATA=/var/lib/core-01 and CORE_HOME=/opt/core-01.
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
CORE_DATA="${CORE_DATA:-${SCRIPT_DIR}/runtime}"
mkdir -p \
    "${CORE_DATA}/db" "${CORE_DATA}/media" "${CORE_DATA}/analytics" \
    "${CORE_DATA}/research" "${CORE_DATA}/concepts" "${CORE_DATA}/published" \
    "${CORE_DATA}/logs" "${CORE_DATA}/sessions" "${CORE_DATA}/tmp" \
    "${SCRIPT_DIR}/vault/skills" \
    "${SCRIPT_DIR}/vault/concepten" \
    "${SCRIPT_DIR}/vault/uitgaand" \
    "${SCRIPT_DIR}/vault/gepubliceerd" \
    "${SCRIPT_DIR}/vault/research" \
    "${SCRIPT_DIR}/vault/logs/screenshots"

TARGET_USER="${SUDO_USER:-${USER}}"
TARGET_GROUP="$(id -gn "${TARGET_USER}")"
"${SUDO[@]}" chown -R "${TARGET_USER}:${TARGET_GROUP}" "${SCRIPT_DIR}/vault" "${CORE_DATA}"
chmod -R u+rwX "${SCRIPT_DIR}/vault" "${CORE_DATA}"

if command -v nvidia-smi >/dev/null 2>&1; then
    echo "Optional NVIDIA GPU detected: $(nvidia-smi --query-gpu=name --format=csv,noheader | head -1)"
else
    echo "Optional NVIDIA GPU unavailable; CPU/browser operation remains supported."
fi
if command -v ollama >/dev/null 2>&1; then echo "Optional local LLM runtime: ollama available"; else echo "Optional local LLM runtime unavailable; use LOCAL_LLM_MOCK=1 for tests."; fi
