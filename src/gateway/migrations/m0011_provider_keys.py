"""Migration 11: providers' keys, sealed. See docs/32-provider-settings-and-vault.md.

One row per provider that has a key: which master key sealed it (``key_id``), the nonce, the
AES-GCM ciphertext and, for keys of 20 or more characters, the last four characters. Never the
key. In a table of its own so deleting a key removes the row, ciphertext and all. A new table:
no rebuild.
"""

from __future__ import annotations

import sqlite3

from gateway.migrations.runner import Migration


def _up(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE provider_keys ("
        " provider_id TEXT PRIMARY KEY REFERENCES provider_configs (id),"
        " key_id TEXT NOT NULL,"
        " nonce BLOB NOT NULL,"
        " ciphertext BLOB NOT NULL,"
        " last4 TEXT,"
        " created_at_ms REAL NOT NULL,"
        " updated_at_ms REAL NOT NULL)"
    )


def _down(connection: sqlite3.Connection) -> None:
    connection.execute("DROP TABLE provider_keys")


MIGRATION = Migration(11, "provider_keys", _up, _down)
