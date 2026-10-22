"""The gateway's schema migrations, in order. See docs/19-usage-store.md."""

from __future__ import annotations

from gateway.migrations import (
    m0001_usage_events,
    m0002_applications,
    m0003_usage_retention,
    m0004_organizations,
    m0005_users,
    m0006_sessions,
    m0007_audit_log,
)
from gateway.migrations.runner import (
    Migration,
    MigrationError,
    MigrationPlan,
    MigrationPreview,
    MigrationRunner,
    plan_migrations,
)

MIGRATIONS: tuple[Migration, ...] = (
    m0001_usage_events.MIGRATION,
    m0002_applications.MIGRATION,
    m0003_usage_retention.MIGRATION,
    m0004_organizations.MIGRATION,
    m0005_users.MIGRATION,
    m0006_sessions.MIGRATION,
    m0007_audit_log.MIGRATION,
)

__all__ = [
    "MIGRATIONS",
    "Migration",
    "MigrationError",
    "MigrationPlan",
    "MigrationPreview",
    "MigrationRunner",
    "plan_migrations",
]
