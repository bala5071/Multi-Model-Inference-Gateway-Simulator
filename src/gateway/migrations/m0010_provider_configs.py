"""Migration 10: an organization's model providers. See docs/32-provider-settings-and-vault.md.

One row per provider an organization configured: its type (``openai_compatible`` or
``anthropic``: an organization never configures Ollama, which is the operator's), base URL,
model, the plan tier it can serve, its role, the organization's and the platform's switches,
optional prices per million tokens the organization entered, and its probed capabilities (JSON,
each flag with its source, when and with which model it was probed). Its key is in migration 11.
A new table: no rebuild.
"""

from __future__ import annotations

import sqlite3

from gateway.migrations.runner import Migration


def _up(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE provider_configs ("
        " id TEXT PRIMARY KEY,"
        " organization_id TEXT NOT NULL REFERENCES organizations (id),"
        " type TEXT NOT NULL CHECK (type IN ('openai_compatible', 'anthropic')),"
        " base_url TEXT NOT NULL,"
        " model TEXT NOT NULL,"
        " serves_tier TEXT NOT NULL CHECK (serves_tier IN ('economy', 'standard', 'premium')),"
        " role TEXT NOT NULL CHECK (role IN ('primary', 'fallback')),"
        " enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),"
        " platform_enabled INTEGER NOT NULL DEFAULT 1 CHECK (platform_enabled IN (0, 1)),"
        " price_input_per_mtok REAL,"
        " price_output_per_mtok REAL,"
        " capabilities TEXT NOT NULL DEFAULT '{}',"
        " first_token_budget_ms REAL,"
        " created_at_ms REAL NOT NULL,"
        " UNIQUE (organization_id, type, base_url, model))"
    )
    connection.execute(
        "CREATE INDEX provider_configs_by_organization ON provider_configs (organization_id)"
    )


def _down(connection: sqlite3.Connection) -> None:
    connection.execute("DROP INDEX provider_configs_by_organization")
    connection.execute("DROP TABLE provider_configs")


MIGRATION = Migration(10, "provider_configs", _up, _down)
