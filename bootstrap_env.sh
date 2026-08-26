#!/usr/bin/env bash
set -e

# Obsidian via Flathub installeren voor Linux ARM64.
if [[ "$(uname -m)" != "aarch64" && "$(uname -m)" != "arm64" ]]; then
    echo "Fout: dit installatiescript vereist een ARM64-systeem." >&2
    exit 1
fi

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
    python3-dotenv

flatpak remote-add --user --if-not-exists \
    flathub \
    https://flathub.org/repo/flathub.flatpakrepo
flatpak install --user --noninteractive --or-update \
    flathub \
    md.obsidian.Obsidian

# Vault-structuur relatief aan de locatie van dit script aanmaken.
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
mkdir -p \
    "${SCRIPT_DIR}/vault/skills" \
    "${SCRIPT_DIR}/vault/concepten" \
    "${SCRIPT_DIR}/vault/uitgaand" \
    "${SCRIPT_DIR}/vault/gepubliceerd" \
    "${SCRIPT_DIR}/vault/research" \
    "${SCRIPT_DIR}/vault/logs/screenshots"

TARGET_USER="${SUDO_USER:-${USER}}"
TARGET_GROUP="$(id -gn "${TARGET_USER}")"
"${SUDO[@]}" chown -R "${TARGET_USER}:${TARGET_GROUP}" "${SCRIPT_DIR}/vault"
chmod -R u+rwX "${SCRIPT_DIR}/vault"
