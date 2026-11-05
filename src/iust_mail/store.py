"""SQLite store: per-user accounts, sessions/tokens, small state, and short email keys."""

import json
import os
import sqlite3
import threading
from pathlib import Path

_lock = threading.RLock()


def _db_path() -> Path:
    return Path(os.environ.get("DB_FILE", "/data/bot.db"))


def _connect() -> sqlite3.Connection:
    path = _db_path()
    new = not path.exists()
    conn = sqlite3.connect(path, timeout=10)
    if new:
        os.chmod(path, 0o600)  # holds passwords and login tokens
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS kv (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS accounts (
            chat_id INTEGER NOT NULL,
            service TEXT NOT NULL,          -- 'samad' | 'email' | 'lms'
            data TEXT NOT NULL,             -- JSON: username, password, (email)
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (chat_id, service)
        );
        CREATE TABLE IF NOT EXISTS mail_keys (
            key INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id TEXT NOT NULL UNIQUE
        );
    """)
    return conn


# ---------- key/value (sessions, tokens, per-user state) ----------

def get(key: str, default=None):
    with _lock, _connect() as conn:
        row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return json.loads(row[0]) if row else default


def put(key: str, value) -> None:
    with _lock, _connect() as conn:
        conn.execute(
            "INSERT INTO kv (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = datetime('now')",
            (key, json.dumps(value)),
        )


def delete(key: str) -> None:
    with _lock, _connect() as conn:
        conn.execute("DELETE FROM kv WHERE key = ?", (key,))


# ---------- accounts ----------

def get_account(chat_id: int, service: str) -> dict | None:
    with _lock, _connect() as conn:
        row = conn.execute("SELECT data FROM accounts WHERE chat_id = ? AND service = ?",
                           (chat_id, service)).fetchone()
    return json.loads(row[0]) if row else None


def set_account(chat_id: int, service: str, data: dict) -> None:
    with _lock, _connect() as conn:
        conn.execute(
            "INSERT INTO accounts (chat_id, service, data) VALUES (?, ?, ?) "
            "ON CONFLICT(chat_id, service) DO UPDATE SET data = excluded.data, updated_at = datetime('now')",
            (chat_id, service, json.dumps(data)),
        )


def delete_account(chat_id: int, service: str) -> None:
    with _lock, _connect() as conn:
        conn.execute("DELETE FROM accounts WHERE chat_id = ? AND service = ?", (chat_id, service))
        conn.execute("DELETE FROM kv WHERE key = ?", (f"{service}_session:{chat_id}",))


def users_with(service: str) -> list[tuple[int, dict]]:
    with _lock, _connect() as conn:
        rows = conn.execute("SELECT chat_id, data FROM accounts WHERE service = ?", (service,)).fetchall()
    return [(chat_id, json.loads(data)) for chat_id, data in rows]


# ---------- short keys for email ids (callback_data is limited to 64 bytes) ----------

def mail_key_for(item_id: str) -> str:
    with _lock, _connect() as conn:
        conn.execute("INSERT OR IGNORE INTO mail_keys (item_id) VALUES (?)", (item_id,))
        row = conn.execute("SELECT key FROM mail_keys WHERE item_id = ?", (item_id,)).fetchone()
        conn.execute("DELETE FROM mail_keys WHERE key <= (SELECT MAX(key) FROM mail_keys) - 2000")
    return str(row[0])


def mail_item_id(key: str) -> str | None:
    try:
        k = int(key)
    except ValueError:
        return None
    with _lock, _connect() as conn:
        row = conn.execute("SELECT item_id FROM mail_keys WHERE key = ?", (k,)).fetchone()
    return row[0] if row else None
