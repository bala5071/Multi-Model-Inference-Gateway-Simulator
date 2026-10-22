"""Create organizations and applications, and issue, rotate and revoke their API keys.

The only way to issue a key (there is no admin HTTP endpoint; A2 adds authenticated
endpoints). See docs/20-applications-api-keys.md and docs/26-organizations.md.

    uv run python scripts/manage.py migrate --dry-run
    uv run python scripts/manage.py migrate
    uv run python scripts/manage.py seed-demo
    uv run python scripts/manage.py create-organization --id north --name "North" --kind business
    uv run python scripts/manage.py list-organizations
    uv run python scripts/manage.py create-application --id shop --name "Shop" --plan pro \\
        --organization north
    uv run python scripts/manage.py organization-usage --organization north
    uv run python scripts/manage.py disable-organization --organization north
    uv run python scripts/manage.py enable-organization --organization north
    uv run python scripts/manage.py create-user --username alice --organization north
    uv run python scripts/manage.py create-user --username root --platform-admin
    uv run python scripts/manage.py reset-password --username alice
    uv run python scripts/manage.py unlock-user --username alice
    uv run python scripts/manage.py disable-user --username alice
    uv run python scripts/manage.py enable-user --username alice
    uv run python scripts/manage.py create-key --application shop --label "backend"
    uv run python scripts/manage.py rotate-key --key-id <id> --grace-hours 24
    uv run python scripts/manage.py revoke-key --key-id <id>
    uv run python scripts/manage.py list-keys --application shop
    uv run python scripts/manage.py backup --to /backups/gateway-2025-06-01.db
    uv run python scripts/manage.py restore --from /backups/gateway-2025-06-01.db

The database is GATEWAY_DATABASE (default: the user's local data folder, outside the working
tree), the one the server reads; it is created owner-only and migrated if needed. Before a
migration that rebuilds a table, the database is backed up next to itself and the backup's
path printed; if the backup fails, nothing is migrated. Organizations are named by their id.
A user's password is read from a prompt, twice, never from the command line, and never
printed. Issuing or rotating a key needs GATEWAY_KEY_PEPPER, the same value the server has.
A new key is printed once, alone, on standard output; it is not stored, logged or shown
again. Everything else goes to standard error.
"""

from __future__ import annotations

import argparse
import getpass
import io
import os
import secrets
import sqlite3
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from gateway.apikeys import ApiKeyService, load_pepper
from gateway.applications import (
    ApiKeyRecord,
    SqliteApplicationStore,
    application_from_template,
    seed_demo_applications,
)
from gateway.audit import AuditLog
from gateway.auth import AccountService
from gateway.clock import EpochSource, SystemEpoch, WallClock
from gateway.database import (
    DatabaseInUseError,
    ProcessLock,
    backup_database,
    check_backup,
    database_path,
    open_database,
    restore_database,
)
from gateway.ledger import CostLedger
from gateway.migrations import (
    MIGRATIONS,
    MigrationError,
    MigrationPlan,
    MigrationRunner,
    plan_migrations,
)
from gateway.organizations import DEFAULT_ORGANIZATION_ID, OrganizationKind, new_organization
from gateway.passwords import (
    PRODUCTION,
    InlineHashRunner,
    PasswordHasher,
    PasswordPolicyError,
    Passwords,
    parameters_from,
)
from gateway.routing import load_plans
from gateway.usage import SqliteUsageStore
from gateway.users import AccountStore

DAY_MS = 24 * 60 * 60 * 1000.0
HOUR_MS = 60 * 60 * 1000.0


def _note(message: str) -> None:
    print(message, file=sys.stderr)


def migration_backup_path(database: Path, version: int, epoch: EpochSource) -> Path:
    """Where the backup taken before migration ``version`` goes: beside the database."""
    return database.with_name(
        f"{database.name}.before-migration-{version}-{epoch.epoch_ms():.0f}.db"
    )


def migrate(connection: sqlite3.Connection, database: Path, epoch: EpochSource) -> tuple[int, ...]:
    """Apply the pending migrations. Refuses, changing nothing, if a migration reports a
    conflict. Before a migration that rebuilds a table (on a database that has any data),
    takes the server's lock (so no gateway runs on the old schema meanwhile) and a backup,
    and prints the backup's path; a failed backup stops everything."""
    plan = plan_migrations(connection, MIGRATIONS)
    if plan.conflicts:
        raise MigrationError("refused, nothing was changed: " + "; ".join(plan.conflicts))
    runner = MigrationRunner(connection, MIGRATIONS, epoch)
    if not (plan.rebuilds_tables and plan.current_version > 0):
        return runner.migrate()
    lock = ProcessLock(database)
    try:
        first = next(m.version for m, _ in plan.pending if m.rebuilds_tables)
        target = migration_backup_path(database, first, epoch)
        try:
            backup_database(connection, target)
            check_backup(target)
        except (OSError, ValueError, sqlite3.Error) as exc:
            raise MigrationError(
                f"the backup before migration {first} failed, so nothing was migrated: {exc}"
            ) from exc
        _note(f"backed up {database} to {target} before migration {first} (rebuilds a table)")
        return runner.migrate()
    finally:
        lock.release()


def _describe_plan(plan: MigrationPlan, database: Path, exists: bool) -> None:
    if not exists:
        _note(f"no database at {database}: migrate would create it")
    _note(f"database is at version {plan.current_version}; this code knows {len(MIGRATIONS)}")
    if not plan.pending:
        _note("nothing to do")
    for migration, preview in plan.pending:
        rebuild = " (rebuilds a table)" if migration.rebuilds_tables else ""
        _note(f"migration {migration.version} {migration.name}{rebuild}:")
        for change in preview.changes:
            _note(f"  - {change}")
        for conflict in preview.conflicts:
            _note(f"  CONFLICT: {conflict}")
    if plan.rebuilds_tables and plan.current_version > 0:
        _note("a backup would be taken first, beside the database")
    if plan.conflicts:
        _note("migrate would refuse and change nothing until the conflicts are fixed")


def dry_run(database: Path) -> int:
    """Report what ``migrate`` would do without touching the database: it is opened
    read-only and no file is created. Exit 1 if migrate would refuse.

    With no write-ahead log beside it (the state after a clean shutdown) the file is opened
    ``immutable``, so SQLite creates no ``-wal`` or ``-shm`` file. If a log exists, a
    connection is or was open and its shared-memory file exists already; the database is
    then opened read-only without ``immutable``, so the log's newest rows are seen."""
    if not database.exists():
        connection = sqlite3.connect(":memory:")
        exists = False
    else:
        wal = database.with_name(database.name + "-wal")
        mode = "ro" if wal.exists() else "ro&immutable=1"
        connection = sqlite3.connect(f"file:{database.as_posix()}?mode={mode}", uri=True)
        exists = True
    try:
        plan = plan_migrations(connection, MIGRATIONS)
    finally:
        connection.close()
    _describe_plan(plan, database, exists)
    return 1 if plan.conflicts else 0


def _open(environ: dict[str, str]) -> tuple[sqlite3.Connection, SqliteApplicationStore]:
    path = database_path(environ)
    connection = open_database(path)
    try:
        migrate(connection, path, SystemEpoch())
    except BaseException:
        connection.close()
        raise
    return connection, SqliteApplicationStore(connection)


def _describe(key: ApiKeyRecord) -> str:
    state = "revoked" if key.revoked else "active"
    expires = f"expires_at_ms={key.expires_at_ms:.0f}" if key.expires_at_ms else "no expiry"
    used = f"last_used_at_ms={key.last_used_at_ms:.0f}" if key.last_used_at_ms else "never used"
    return f"{key.key_id}  {state}  {key.label!r}  {expires}  {used}"


def _organization_usage(
    connection: sqlite3.Connection, store: SqliteApplicationStore, organization_id: str
) -> None:
    if store.get_organization(organization_id) is None:
        raise ValueError(f"no organization {organization_id!r}")
    budgets = {
        a.id: a.plan.monthly_token_budget
        for a in store.list_applications(organization_id=organization_id)
    }
    epoch = SystemEpoch()
    ledger = CostLedger(WallClock(), store=SqliteUsageStore(connection), epoch=epoch)
    usage = ledger.organization_usage(organization_id, budgets)
    print(
        f"organization {organization_id}  period_start_ms={usage.period_start_ms:.0f}  "
        f"period_end_ms={usage.period_end_ms:.0f}"
    )
    for share in usage.applications:
        print(f"  {share.application_id}  used={share.used_tokens}  budget={share.budget_tokens}")
    print(
        f"  total  requests={usage.total.requests}  used={usage.total.billed_tokens}  "
        f"billed_cost_usd={usage.total.billed_cost_usd:.6f}  "
        f"sum_of_budgets={usage.budget_tokens}"
    )


ReadPassword = Callable[[str], str]


def _new_password(read_password: ReadPassword) -> str:
    """A password from the prompt, entered twice. Never echoed or printed."""
    first = read_password("New password: ")
    if read_password("Again: ") != first:
        raise ValueError("the two passwords differ; nothing was changed")
    return first


def _accounts(
    connection: sqlite3.Connection, store: SqliteApplicationStore, env: dict[str, str]
) -> AccountService:
    # The CLI never logs anyone in, so its login limiter needs no lasting secret; hashing
    # is synchronous here (no event loop, nothing else waiting).
    hasher = PasswordHasher(parameters_from(env, PRODUCTION))
    return AccountService(
        AccountStore(connection),
        AuditLog(connection),
        Passwords(hasher, InlineHashRunner()),
        organizations=store,
        clock=WallClock(),
        epoch=SystemEpoch(),
        limiter_secret=secrets.token_bytes(32),
    )


def main(
    argv: Sequence[str] | None = None,
    environ: dict[str, str] | None = None,
    read_password: ReadPassword = getpass.getpass,
) -> int:
    env = dict(os.environ) if environ is None else environ
    parser = argparse.ArgumentParser(prog="manage.py", description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    migrate_cmd = commands.add_parser("migrate", help="create or upgrade the database")
    migrate_cmd.add_argument(
        "--dry-run", action="store_true", help="say what would change; touch nothing"
    )
    commands.add_parser("seed-demo", help="create the plans file's demo applications")
    org = commands.add_parser("create-organization")
    org.add_argument("--id", required=True)
    org.add_argument("--name", required=True)
    org.add_argument("--kind", required=True, choices=[k.value for k in OrganizationKind])
    commands.add_parser("list-organizations")
    for name, help_text in (
        ("disable-organization", "its keys stop working; no new applications in it"),
        ("enable-organization", "its keys work again"),
        ("organization-usage", "its applications' usage this period, added up (read-only)"),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--organization", required=True, help="an organization id")
    apps = commands.add_parser("list-applications")
    apps.add_argument("--organization", help="only this organization's")
    create = commands.add_parser("create-application", help="create one from a plan template")
    create.add_argument("--id", required=True)
    create.add_argument("--name", required=True)
    create.add_argument("--plan", required=True, help="a plan in the plans file")
    create.add_argument(
        "--organization", default=DEFAULT_ORGANIZATION_ID, help="an organization id"
    )
    user = commands.add_parser("create-user", help="a console account; asks for its password")
    user.add_argument("--username", required=True)
    user.add_argument("--organization", default=DEFAULT_ORGANIZATION_ID, help="an organization id")
    user.add_argument("--platform-admin", action="store_true")
    for name, help_text in (
        ("disable-user", "it cannot log in; all its sessions are revoked"),
        ("enable-user", "it can log in again"),
        ("reset-password", "a temporary password it must change; revokes its sessions"),
        ("unlock-user", "clear a lockout after failed logins"),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--username", required=True)
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
    backup = commands.add_parser(
        "backup", help="a consistent copy of the database, safe while the server runs"
    )
    backup.add_argument("--to", type=Path, required=True, help="a new file; never overwritten")
    restore = commands.add_parser(
        "restore", help="replace the database with a backup; the server must be stopped"
    )
    restore.add_argument("--from", dest="source", type=Path, required=True)
    args = parser.parse_args(argv)

    if args.command == "restore":
        # Before opening the database: restore takes the server's lock, so it refuses while
        # a gateway process is running on it.
        try:
            version = check_backup(args.source)
            restore_database(args.source, database_path(env))
        except (ValueError, DatabaseInUseError) as exc:
            _note(f"error: {exc}")
            return 1
        _note(
            f"restored {database_path(env)} from {args.source} (at migration {version}). "
            "Run `manage.py migrate` before starting the server if it is behind (the server "
            "refuses a migration that rebuilds a table). The pepper is not in a backup."
        )
        return 0
    if args.command == "backup":
        # At any version, without migrating: the copy to take before fixing whatever stops a
        # migration (docs/26-organizations.md).
        connection = open_database(database_path(env))
        try:
            backup_database(connection, args.to)
        except FileExistsError as exc:
            _note(f"error: {exc}")
            return 1
        finally:
            connection.close()
        _note(
            f"backed up to {args.to}. It holds key HMACs and usage, not the pepper: back the "
            "pepper up separately, or the copy cannot check any key."
        )
        return 0
    if args.command == "migrate" and args.dry_run:
        try:
            return dry_run(database_path(env))
        except (MigrationError, sqlite3.Error) as exc:
            _note(f"error: {exc}")
            return 1

    plans_path = env.get("GATEWAY_PLANS")
    templates = load_plans(Path(plans_path) if plans_path else None)
    epoch = SystemEpoch()
    try:
        connection, store = _open(env)
    except (MigrationError, DatabaseInUseError) as exc:
        _note(f"error: {exc}")
        return 1
    try:
        if args.command == "migrate":
            _note(f"database is at version {len(MIGRATIONS)}")
        elif args.command == "seed-demo":
            made = seed_demo_applications(store, templates, created_at_ms=epoch.epoch_ms())
            _note(f"created {len(made)} demo application(s): {', '.join(a.id for a in made)}")
        elif args.command == "create-organization":
            organization = new_organization(
                args.id, args.name, args.kind, created_at_ms=epoch.epoch_ms()
            )
            store.add_organization(organization)
            _note(f"created organization {args.id!r} ({args.kind})")
        elif args.command == "list-organizations":
            for organization in store.list_organizations():
                count = len(store.list_applications(organization_id=organization.id))
                state = "disabled" if organization.disabled else "enabled"
                print(
                    f"{organization.id}  {organization.name!r}  kind={organization.kind.value}  "
                    f"{state}  applications={count}"
                )
        elif args.command in ("disable-organization", "enable-organization"):
            disabled = args.command == "disable-organization"
            if store.get_organization(args.organization) is None:
                raise ValueError(f"no organization {args.organization!r}")
            store.set_organization_disabled(args.organization, disabled)
            _note(f"organization {args.organization!r} {'disabled' if disabled else 'enabled'}")
        elif args.command == "create-user":
            accounts = _accounts(connection, store, env)
            new_user = accounts.create_user(
                args.username,
                _new_password(read_password),
                args.organization,
                platform_admin=args.platform_admin,
            )
            role = " (platform_admin)" if new_user.platform_admin else ""
            where = new_user.organization_id
            _note(f"created user {new_user.username!r}{role} in organization {where!r}")
        elif args.command == "disable-user":
            revoked = _accounts(connection, store, env).disable_user(args.username)
            _note(f"disabled user {args.username!r}; revoked {revoked} session(s)")
        elif args.command == "enable-user":
            _accounts(connection, store, env).enable_user(args.username)
            _note(f"enabled user {args.username!r}")
        elif args.command == "reset-password":
            accounts = _accounts(connection, store, env)
            revoked = accounts.reset_password(args.username, _new_password(read_password))
            _note(
                f"reset the password of {args.username!r}: it must be changed at the next "
                f"login; revoked {revoked} session(s)"
            )
        elif args.command == "unlock-user":
            _accounts(connection, store, env).unlock_user(args.username)
            _note(f"unlocked user {args.username!r}")
        elif args.command == "organization-usage":
            _organization_usage(connection, store, args.organization)
        elif args.command == "list-applications":
            for application in store.list_applications(organization_id=args.organization):
                print(
                    f"{application.id}  {application.name!r}  plan={application.plan.name}  "
                    f"organization={application.organization_id}"
                )
        elif args.command == "create-application":
            store.add_application(
                application_from_template(
                    args.id,
                    args.name,
                    templates,
                    args.plan,
                    organization_id=args.organization,
                    created_at_ms=epoch.epoch_ms(),
                )
            )
            _note(
                f"created application {args.id!r} on plan {args.plan!r} in organization "
                f"{args.organization!r}"
            )
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
            # A bare "\n" even on Windows, so `KEY=$(... create-key ...)` in bash gets the key
            # without a trailing carriage return.
            if isinstance(sys.stdout, io.TextIOWrapper):
                sys.stdout.reconfigure(newline="\n")
            print(issued.key)
    except (ValueError, KeyError, FileExistsError, PasswordPolicyError) as exc:
        _note(f"error: {exc}")
        return 1
    finally:
        connection.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
