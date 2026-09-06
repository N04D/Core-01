"""OS-backed secret storage for dashboard and plugin credentials.

Secrets are stored through the platform keyring (Secret Service, macOS
Keychain, or Windows Credential Manager) and never persisted in SQLite or
project files.
"""
from __future__ import annotations

import os
from typing import Final

SERVICE: Final[str] = os.getenv("SOCIAL_ENGINE_KEYRING_SERVICE", "social-engine")

try:
    import keyring  # type: ignore
    # Prefer the Linux Secret Service explicitly when a user D-Bus session is
    # present; automatic backend discovery can select the fail-safe null
    # backend in virtual environments.
    if os.getenv("DBUS_SESSION_BUS_ADDRESS"):
        from keyring.backends.SecretService import Keyring as _SecretServiceKeyring  # type: ignore
        keyring.set_keyring(_SecretServiceKeyring())
except ImportError:  # pragma: no cover - exercised on minimal installs
    keyring = None


def _require() -> object:
    if keyring is None:
        raise RuntimeError("keyring ontbreekt; installeer dit in de venv met: pip install keyring")
    return keyring


def set_secret(account: str, secret: str) -> None:
    if not account or not secret:
        raise ValueError("account en secret zijn verplicht")
    _require().set_password(SERVICE, account, secret)  # type: ignore[union-attr]


def get_secret(account: str) -> str | None:
    if not account:
        raise ValueError("account is verplicht")
    return _require().get_password(SERVICE, account)  # type: ignore[union-attr]


def delete_secret(account: str) -> None:
    try:
        _require().delete_password(SERVICE, account)  # type: ignore[union-attr]
    except Exception as exc:
        # keyring implementations differ when an item does not exist.
        if "not found" not in str(exc).casefold() and "no such" not in str(exc).casefold():
            raise
