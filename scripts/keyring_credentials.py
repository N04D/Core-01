#!/usr/bin/env python3
"""Store application credentials in the operating-system keyring."""
from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.keyring_store import delete_secret, get_secret, set_secret


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("account", help="Keyring-accountnaam, bijvoorbeeld linkedin")
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--set", action="store_true", help="Wachtwoord interactief instellen")
    actions.add_argument("--get", action="store_true", help="Alleen controleren of een secret bestaat")
    actions.add_argument("--delete", action="store_true", help="Secret verwijderen")
    args = parser.parse_args()
    try:
        if args.delete:
            delete_secret(args.account)
            print("Secret verwijderd.")
        elif args.get:
            print("Secret aanwezig." if get_secret(args.account) else "Geen secret gevonden.")
        else:
            first = getpass.getpass("Wachtwoord (niet zichtbaar): ")
            second = getpass.getpass("Herhaal wachtwoord: ")
            if first != second:
                raise ValueError("wachtwoorden komen niet overeen")
            set_secret(args.account, first)
            print("Secret opgeslagen in de OS-keyring.")
        return 0
    except Exception as exc:
        print(f"Keyring-fout: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
