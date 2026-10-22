"""The gateway's schema migrations, in order. See docs/19-usage-store.md."""

from __future__ import annotations

from gateway.migrations import m0001_usage_events, m0002_applications
from gateway.migrations.runner import Migration, MigrationError, MigrationRunner

MIGRATIONS: tuple[Migration, ...] = (
    m0001_usage_events.MIGRATION,
    m0002_applications.MIGRATION,
)

__all__ = ["MIGRATIONS", "Migration", "MigrationError", "MigrationRunner"]
