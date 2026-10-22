"""Create applications and issue, rotate and revoke their API keys.

The only way to issue a key (there is no admin HTTP endpoint; A2 adds authenticated
endpoints). See docs/20-applications-api-keys.md.

    uv run python scripts/manage.py seed-demo
    uv run python scripts/manage.py create-application --id shop --name "Shop" --plan pro
    uv run python scripts/manage.py create-key --application shop --label "backend"
    uv run python scripts/manage.py rotate-key --key-id <id> --grace-hours 24
    uv run python scripts/manage.py revoke-key --key-id <id>
    uv run python scripts/manage.py list-keys --application shop

The database is GATEWAY_DATABASE (default: the user's local data folder, outside the working
tree), the one the server reads; it is created owner-only and migrated if needed. Issuing or
rotating a key needs GATEWAY_KEY_PEPPER, the same value the server has. A new key is printed
once, alone, on standard output; it is not stored, logged or shown again. Everything else
goes to standard error.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from collections.abc import Sequence
from pathlib import Path

from gateway.apikeys import ApiKeyService, load_pepper
from gateway.applications import (
    ApiKeyRecord,
    SqliteApplicationStore,
    application_from_template,
    seed_demo_applications,
)
from gateway.clock import SystemEpoch
from gateway.database import database_path, open_database
from gateway.migrations import MIGRATIONS, MigrationRunner
from gateway.routing import load_plans

DAY_MS = 24 * 60 * 60 * 1000.0
HOUR_MS = 60 * 60 * 1000.0


def _note(message: str) -> None:
    print(message, file=sys.stderr)


def _open(environ: dict[str, str]) -> tuple[sqlite3.Connection, SqliteApplicationStore]:
    connection = open_database(database_path(environ))
    MigrationRunner(connection, MIGRATIONS, SystemEpoch()).migrate()
    return connection, SqliteApplicationStore(connection)


def _describe(key: ApiKeyRecord) -> str:
    state = "revoked" if key.revoked else "active"
    expires = f"expires_at_ms={key.expires_at_ms:.0f}" if key.expires_at_ms else "no expiry"
    used = f"last_used_at_ms={key.last_used_at_ms:.0f}" if key.last_used_at_ms else "never used"
    return f"{key.key_id}  {state}  {key.label!r}  {expires}  {used}"


def main(argv: Sequence[str] | None = None, environ: dict[str, str] | None = None) -> int:
    env = dict(os.environ) if environ is None else environ
    parser = argparse.ArgumentParser(prog="manage.py", description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("migrate", help="create or upgrade the database")
    commands.add_parser("seed-demo", help="create the plans file's demo applications")
    commands.add_parser("list-applications")
    create = commands.add_parser("create-application", help="create one from a plan template")
    create.add_argument("--id", required=True)
    create.add_argument("--name", required=True)
    create.add_argument("--plan", required=True, help="a plan in the plans file")
    issue = commands.add_parser("create-key", help="issue a key; it is printed once")
    issue.add_argument("--application", required=True)
    issue.add_argument("--label", required=True)
    issue.add_argument("--expires-in-days", type=float)
    rotate = commands.add_parser(
        "rotate-key", help="issue a replacement; the old key stops working after the grace period"
    )
    rotate.add_argument("--key-id", required=True)
    rotate.add_argument("--grace-hours", type=float, default=24.0)
    revoke = commands.add_parser("revoke-key", help="stop a key working now")
    revoke.add_argument("--key-id", required=True)
    keys = commands.add_parser("list-keys", help="key ids, labels and state; never the keys")
    keys.add_argument("--application", required=True)
    args = parser.parse_args(argv)

    plans_path = env.get("GATEWAY_PLANS")
    templates = load_plans(Path(plans_path) if plans_path else None)
    epoch = SystemEpoch()
    connection, store = _open(env)
    try:
        if args.command == "migrate":
            _note(f"database is at version {len(MIGRATIONS)}")
        elif args.command == "seed-demo":
            made = seed_demo_applications(store, templates, created_at_ms=epoch.epoch_ms())
            _note(f"created {len(made)} demo application(s): {', '.join(a.id for a in made)}")
        elif args.command == "list-applications":
            for application in store.list_applications():
                print(f"{application.id}  {application.name!r}  plan={application.plan.name}")
        elif args.command == "create-application":
            store.add_application(
                application_from_template(
                    args.id, args.name, templates, args.plan, created_at_ms=epoch.epoch_ms()
                )
            )
            _note(f"created application {args.id!r} on plan {args.plan!r}")
        elif args.command == "list-keys":
            for key in store.list_keys(args.application):
                print(_describe(key))
        elif args.command == "revoke-key":
            store.revoke_key(args.key_id)
            _note(f"revoked key {args.key_id}")
        else:
            service = ApiKeyService(store, load_pepper(env), epoch)
            if args.command == "create-key":
                expires = (
                    epoch.epoch_ms() + args.expires_in_days * DAY_MS
                    if args.expires_in_days is not None
                    else None
                )
                issued = service.issue(args.application, args.label, expires_at_ms=expires)
            else:
                issued = service.rotate(args.key_id, grace_ms=args.grace_hours * HOUR_MS)
                _note(f"key {args.key_id} stops working in {args.grace_hours:g} hours")
            _note(
                f"key id {issued.record.key_id} for application {issued.record.application_id!r}. "
                "The key is printed below, once; store it now, it cannot be shown again."
            )
            print(issued.key)
    except (ValueError, KeyError) as exc:
        _note(f"error: {exc}")
        return 1
    finally:
        connection.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
