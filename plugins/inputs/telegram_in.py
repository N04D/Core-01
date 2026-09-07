#!/usr/bin/env python3
"""Telegram Bot API input adapter for the local event-driven content system."""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final
from uuid import uuid4

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from core.database import connect_database
from core.paths import DATABASE_PATH

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    def load_dotenv(*_args: object, **_kwargs: object) -> bool:
        return False


PROJECT_ROOT: Final = Path(__file__).resolve().parents[2]
DEFAULT_DATABASE: Final = DATABASE_PATH
PLUGIN_NAME: Final = "Telegram Inbound Hub"
PLUGIN_TYPE: Final = "input"
PLUGIN_ICON: Final = "✈️"
MEDIA_DIR: Final = PROJECT_ROOT / "vault" / "media"
CONCEPT_DIR: Final = PROJECT_ROOT / "vault" / "concepten"
OFFSET_FILE: Final = PROJECT_ROOT / "vault" / "logs" / "telegram_offset.json"
CHANNELS: Final = ("PUBLISH_LINKEDIN_PRO", "PUBLISH_SUBSTACK_PRO", "PUBLISH_MEDIUM")
LOGGER: Final = logging.getLogger("telegram_in")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Receive Telegram messages and media.")
    parser.add_argument("--register", action="store_true")
    parser.add_argument("--db", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--poll-interval", type=float, default=3.0)
    parser.add_argument("--auto-dispatch", action="store_true")
    parser.add_argument("--mock-message", help="Process one synthetic message without Telegram.")
    parser.add_argument("--mock-media-path", type=Path)
    args = parser.parse_args()
    if args.poll_interval <= 0:
        parser.error("--poll-interval must be positive")
    return args


def connect(database: Path) -> sqlite3.Connection:
    return connect_database(database)


def register_plugin(connection: sqlite3.Connection) -> None:
    with connection:
        connection.execute(
            """INSERT INTO plugin_registry(plugin_name,type,executable_path,icon,is_active)
               VALUES (?,?,?,?,1)
               ON CONFLICT(plugin_name) DO UPDATE SET type=excluded.type,
                   executable_path=excluded.executable_path,icon=excluded.icon""",
            (PLUGIN_NAME, PLUGIN_TYPE, str(Path(__file__).resolve()), PLUGIN_ICON),
        )
    LOGGER.info("Registered input plugin: %s", PLUGIN_NAME)


def atomic_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def read_offset() -> int:
    try:
        value = json.loads(OFFSET_FILE.read_text(encoding="utf-8")).get("offset", 0)
        return int(value)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return 0


def allowed_chat(chat_id: int) -> bool:
    configured = os.getenv("TELEGRAM_ALLOWED_CHAT_IDS", "").strip()
    if not configured:
        LOGGER.error(
            "TELEGRAM_ALLOWED_CHAT_IDS is empty; fail-closed policy rejects chat_id=%s",
            chat_id,
        )
        return False
    allowed = {item.strip() for item in configured.split(",") if item.strip()}
    return str(chat_id) in allowed


def api(token: str, method: str, *, params: dict[str, Any] | None = None) -> Any:
    response = requests.get(
        f"https://api.telegram.org/bot{token}/{method}",
        params=params,
        timeout=(10, 40),
    )
    response.raise_for_status()
    body = response.json()
    if not body.get("ok"):
        raise RuntimeError(f"Telegram API {method} rejected the request")
    return body.get("result")


def safe_stem(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_-]+", "-", value).strip("-").lower()
    return cleaned[:60] or "telegram"


def download_media(token: str, file_id: str, filename: str) -> Path:
    metadata = api(token, "getFile", params={"file_id": file_id})
    remote_path = metadata.get("file_path") if isinstance(metadata, dict) else None
    if not remote_path:
        raise RuntimeError("Telegram returned no media path")
    maximum = int(os.getenv("TELEGRAM_MAX_MEDIA_BYTES", str(100 * 1024 * 1024)))
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    suffix = Path(remote_path).suffix or Path(filename).suffix
    destination = MEDIA_DIR / f"telegram_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}_{uuid4().hex[:8]}{suffix}"
    with requests.get(
        f"https://api.telegram.org/file/bot{token}/{remote_path}",
        stream=True,
        timeout=(10, 120),
    ) as response:
        response.raise_for_status()
        declared = int(response.headers.get("content-length", "0") or 0)
        if declared > maximum:
            raise ValueError("Telegram media exceeds TELEGRAM_MAX_MEDIA_BYTES")
        written = 0
        with destination.open("xb") as handle:
            for chunk in response.iter_content(1024 * 256):
                written += len(chunk)
                if written > maximum:
                    handle.close()
                    destination.unlink(missing_ok=True)
                    raise ValueError("Telegram media exceeds TELEGRAM_MAX_MEDIA_BYTES")
                handle.write(chunk)
    return destination


def copy_mock_media(source: Path) -> Path:
    source = source.expanduser().resolve(strict=True)
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    destination = MEDIA_DIR / f"telegram_mock_{uuid4().hex[:8]}{source.suffix.lower()}"
    shutil.copy2(source, destination)
    return destination


def extract_message(update: dict[str, Any], token: str | None, mock_media: Path | None) -> dict[str, Any] | None:
    message = update.get("message") or update.get("channel_post")
    if not isinstance(message, dict):
        return None
    chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
    chat_id = int(chat.get("id", 0))
    if not allowed_chat(chat_id):
        LOGGER.warning("Ignored update from unauthorized chat_id=%s", chat_id)
        return None
    text = str(message.get("text") or message.get("caption") or "").strip()
    media_path: Path | None = None
    media_type: str | None = None
    if mock_media:
        media_path = copy_mock_media(mock_media)
        media_type = "video" if media_path.suffix.lower() in {".mp4", ".mov", ".webm"} else "image"
    elif isinstance(message.get("video"), dict):
        video = message["video"]
        media_path = download_media(token or "", str(video["file_id"]), str(video.get("file_name", "video.mp4")))
        media_type = "video"
    elif isinstance(message.get("photo"), list) and message["photo"]:
        photo = message["photo"][-1]
        media_path = download_media(token or "", str(photo["file_id"]), "photo.jpg")
        media_type = "image"
    if not text and media_path is None:
        LOGGER.info("Ignored unsupported Telegram message_id=%s", message.get("message_id"))
        return None
    return {
        "text": text or "Telegram media-inzending",
        "topic": text.splitlines()[0][:120] if text else "Telegram media-inzending",
        "chat_id": chat_id,
        "message_id": int(message.get("message_id", 0)),
        "media_path": str(media_path) if media_path else None,
        "media_type": media_type,
    }


def yaml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def create_concept(item: dict[str, Any], channels: list[str]) -> Path:
    CONCEPT_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc)
    path = CONCEPT_DIR / f"telegram_{safe_stem(item['topic'])}_{timestamp:%Y%m%dT%H%M%SZ}_{uuid4().hex[:6]}.md"
    media_line = f"media_path: {yaml_string(item['media_path'])}\nmedia_type: {yaml_string(item['media_type'])}\n" if item["media_path"] else ""
    content = (
        "---\n"
        "source: telegram\n"
        f"topic: {yaml_string(item['topic'])}\n"
        f"telegram_chat_id: {item['chat_id']}\n"
        f"telegram_message_id: {item['message_id']}\n"
        f"publish_channels: {json.dumps(channels, ensure_ascii=False)}\n"
        f"{media_line}"
        "---\n\n"
        f"# {item['topic']}\n\n{item['text']}\n"
    )
    path.write_text(content, encoding="utf-8")
    return path


def active_channels(connection: sqlite3.Connection) -> list[str]:
    placeholders = ",".join("?" for _ in CHANNELS)
    rows = connection.execute(
        f"""SELECT er.event_type FROM event_routes er
              JOIN plugin_registry pr ON pr.plugin_name=er.target_plugin_name
             WHERE er.event_type IN ({placeholders}) AND pr.is_active=1
             ORDER BY er.event_type""",
        CHANNELS,
    ).fetchall()
    return [str(row[0]) for row in rows]


def persist(
    connection: sqlite3.Connection,
    item: dict[str, Any],
    auto_dispatch: bool,
    update_id: int,
) -> tuple[Path, int, list[int]] | None:
    """Persist one Telegram update exactly once by update and message identity."""
    try:
        with connection:
            connection.execute(
                """INSERT INTO telegram_updates(update_id,chat_id,message_id)
                   VALUES (?,?,?)""",
                (update_id, item["chat_id"], item["message_id"]),
            )
    except sqlite3.IntegrityError:
        LOGGER.info(
            "Ignored duplicate Telegram update_id=%s chat_id=%s message_id=%s",
            update_id, item["chat_id"], item["message_id"],
        )
        return None

    channels = active_channels(connection) if auto_dispatch else []
    try:
        concept = create_concept(item, channels)
    except Exception:
        with connection:
            connection.execute("DELETE FROM telegram_updates WHERE update_id=?", (update_id,))
        raise
    payload = {
        "source": "telegram",
        "topic": item["topic"],
        "content": item["text"],
        "draft_file": str(concept),
        "media_path": item["media_path"],
        "media_type": item["media_type"],
        "publish_channels": channels,
        "playbook": "master_syndication_suite",
    }
    event_ids: list[int] = []
    with connection:
        audit = connection.execute(
            "INSERT INTO events_queue(event_type,payload,status) VALUES (?,?, 'COMPLETED')",
            ("TELEGRAM_INBOUND", json.dumps(payload, ensure_ascii=False)),
        )
        for channel in channels:
            channel_payload = dict(payload)
            if item["media_path"]:
                field = "video_path" if item["media_type"] == "video" else "image_path"
                channel_payload[field] = item["media_path"]
            cursor = connection.execute(
                "INSERT INTO events_queue(event_type,payload) VALUES (?,?)",
                (channel, json.dumps(channel_payload, ensure_ascii=False)),
            )
            event_ids.append(int(cursor.lastrowid))
        connection.execute(
            """UPDATE telegram_updates
                  SET concept_path=?, inbound_event_id=? WHERE update_id=?""",
            (str(concept), int(audit.lastrowid), update_id),
        )
    return concept, int(audit.lastrowid), event_ids


def mock_update(message: str) -> dict[str, Any]:
    return {"update_id": int(time.time()), "message": {"message_id": int(time.time()), "chat": {"id": 0}, "text": message}}


def process_updates(connection: sqlite3.Connection, updates: list[dict[str, Any]], token: str | None, auto_dispatch: bool, mock_media: Path | None = None) -> int:
    processed = 0
    for update in updates:
        item = extract_message(update, token, mock_media)
        if item:
            update_id = update.get("update_id")
            if not isinstance(update_id, int):
                raise ValueError("Telegram update_id must be an integer")
            stored = persist(connection, item, auto_dispatch, update_id)
            if stored is not None:
                concept, audit_id, publication_ids = stored
                LOGGER.info("Inbound stored concept=%s audit_event=%s publication_events=%s", concept, audit_id, publication_ids)
                processed += 1
        if isinstance(update.get("update_id"), int) and token:
            atomic_json(OFFSET_FILE, {"offset": update["update_id"] + 1, "updated_at": datetime.now(timezone.utc).isoformat()})
    return processed


def main() -> int:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    load_dotenv(PROJECT_ROOT / ".env")
    args = parse_args()
    try:
        with connect(args.db) as connection:
            if args.register:
                register_plugin(connection)
                if not (args.mock_message or args.once):
                    return 0
            if args.mock_message:
                count = process_updates(connection, [mock_update(args.mock_message)], None, args.auto_dispatch, args.mock_media_path)
                LOGGER.info("Mock inbound scan complete; processed=%s", count)
                return 0
            token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
            if not token:
                LOGGER.error("TELEGRAM_BOT_TOKEN is required for live polling; use --mock-message for an offline test")
                return 2
            while True:
                updates = api(token, "getUpdates", params={"offset": read_offset(), "timeout": 30, "allowed_updates": json.dumps(["message", "channel_post"])})
                count = process_updates(connection, updates if isinstance(updates, list) else [], token, args.auto_dispatch)
                LOGGER.info("Telegram scan complete; processed=%s", count)
                if args.once:
                    return 0
                time.sleep(args.poll_interval)
    except (OSError, ValueError, sqlite3.Error, requests.RequestException, RuntimeError):
        LOGGER.exception("Telegram inbound adapter failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
