"""Users and console sessions, and the store that keeps them.

See docs/27-users-and-sessions.md. A user belongs to one organization. Only an argon2id hash
of the password is stored; only SHA-256 digests of a session id and its CSRF token. Store
calls are synchronous, for the reason given in ``gateway.usage``.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import IntEnum
from typing import Any

USERNAME_PATTERN = re.compile(r"[a-z0-9._-]{3,64}")


def normalize_username(raw: str) -> str:
    """The username as stored: case-folded, then 3 to 64 of ``a-z0-9._-``. Only ASCII is
    accepted, and it is checked before case folding (so a Kelvin sign cannot fold into a
    ``k``)."""
    if not raw.isascii():
        raise ValueError("a username may contain only ASCII letters, digits, '.', '-' and '_'")
    folded = raw.casefold()
    if not USERNAME_PATTERN.fullmatch(folded):
        raise ValueError("a username needs 3 to 64 characters: letters, digits, '.', '-' and '_'")
    return folded


class Bootstrap(IntEnum):
    NONE = 0
    # The bootstrap admin before its first login: the environment password logs in once.
    UNUSED = 1
    # After it: until the password is changed, that password no longer logs in.
    USED = 2


@dataclass(frozen=True, slots=True)
class User:
    id: str
    username: str
    password_hash: str
    organization_id: str
    created_at_ms: float
    disabled: bool = False
    must_change_password: bool = False
    bootstrap: Bootstrap = Bootstrap.NONE
    failed_logins: int = 0
    first_failure_at_ms: float | None = None
    locked_until_ms: float | None = None

    def __repr__(self) -> str:
        return f"User(id={self.id!r}, username={self.username!r}, <hash redacted>)"


@dataclass(frozen=True, slots=True)
class Session:
    """A stored session: digests only, never the id or the token a browser holds."""

    id_hash: str
    handle: str
    user_id: str
    csrf_hash: str
    created_at_ms: float
    last_seen_at_ms: float
    expires_at_ms: float
    revoked: bool = False
    # Until when this session may perform flagged actions (step-up); None when it has none.
    step_up_until_ms: float | None = None

    def __repr__(self) -> str:
        return f"Session(handle={self.handle!r}, user_id={self.user_id!r}, <digests redacted>)"


_USER_COLUMNS = (
    "id, username, password_hash, organization_id, created_at_ms, disabled, "
    "must_change_password, bootstrap, failed_logins, first_failure_at_ms, locked_until_ms"
)
_SESSION_COLUMNS = (
    "id_hash, handle, user_id, csrf_hash, created_at_ms, last_seen_at_ms, expires_at_ms, revoked, "
    "step_up_until_ms"
)


def _user(row: tuple[Any, ...]) -> User:
    (uid, name, pw, org, created, disabled, must, boot, fails, first, locked) = row
    return User(
        uid,
        name,
        pw,
        org,
        created,
        bool(disabled),
        bool(must),
        Bootstrap(boot),
        fails,
        first,
        locked,
    )


def _session(row: tuple[Any, ...]) -> Session:
    id_hash, handle, user_id, csrf_hash, created, seen, expires, revoked, step_up = row
    return Session(
        id_hash, handle, user_id, csrf_hash, created, seen, expires, bool(revoked), step_up
    )


class AccountStore:
    """Users and sessions in the tables of migrations 5, 6 and 8."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        found = connection.execute(
            "SELECT count(*) FROM sqlite_master WHERE type = 'table'"
            " AND name IN ('users', 'sessions')"
        ).fetchone()
        if found[0] != 2:
            raise RuntimeError("users and sessions do not exist; run the migrations first")
        self._connection = connection

    @contextmanager
    def immediate(self) -> Iterator[None]:
        """One write transaction, taken at once (``BEGIN IMMEDIATE``): another connection
        cannot write until it ends. Rolled back if the block raises."""
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._connection.execute("ROLLBACK")
            raise
        self._connection.execute("COMMIT")

    # --- users ---------------------------------------------------------------------------

    def add_user(self, user: User) -> None:
        u = user
        try:
            self._connection.execute(
                f"INSERT INTO users ({_USER_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    u.id,
                    u.username,
                    u.password_hash,
                    u.organization_id,
                    u.created_at_ms,
                    int(u.disabled),
                    int(u.must_change_password),
                    int(u.bootstrap),
                    u.failed_logins,
                    u.first_failure_at_ms,
                    u.locked_until_ms,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError(f"username {u.username!r} is taken, or no such organization") from exc

    def get_user(self, user_id: str) -> User | None:
        row = self._connection.execute(
            f"SELECT {_USER_COLUMNS} FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        return _user(row) if row is not None else None

    def find_user(self, username: str) -> User | None:
        row = self._connection.execute(
            f"SELECT {_USER_COLUMNS} FROM users WHERE username = ?", (username,)
        ).fetchone()
        return _user(row) if row is not None else None

    def count_members(self, organization_id: str) -> int:
        row = self._connection.execute(
            "SELECT count(*) FROM users WHERE organization_id = ?", (organization_id,)
        ).fetchone()
        return int(row[0])

    def _set(self, user_id: str, sql: str, params: tuple[Any, ...]) -> None:
        cursor = self._connection.execute(
            f"UPDATE users SET {sql} WHERE id = ?", (*params, user_id)
        )
        if cursor.rowcount != 1:
            raise KeyError(user_id)

    def set_login_state(
        self,
        user_id: str,
        failed_logins: int,
        first_failure_at_ms: float | None,
        locked_until_ms: float | None,
    ) -> None:
        self._set(
            user_id,
            "failed_logins = ?, first_failure_at_ms = ?, locked_until_ms = ?",
            (failed_logins, first_failure_at_ms, locked_until_ms),
        )

    def set_password(
        self, user_id: str, password_hash: str, *, must_change: bool, bootstrap: Bootstrap
    ) -> None:
        self._set(
            user_id,
            "password_hash = ?, must_change_password = ?, bootstrap = ?",
            (password_hash, int(must_change), int(bootstrap)),
        )

    def set_password_hash(self, user_id: str, password_hash: str) -> None:
        """Replace the hash only (an upgrade to the current parameters)."""
        self._set(user_id, "password_hash = ?", (password_hash,))

    def set_bootstrap(self, user_id: str, bootstrap: Bootstrap) -> None:
        self._set(user_id, "bootstrap = ?", (int(bootstrap),))

    def set_disabled(self, user_id: str, disabled: bool) -> None:
        self._set(user_id, "disabled = ?", (int(disabled),))

    # --- sessions ------------------------------------------------------------------------

    def add_session(self, session: Session) -> None:
        s = session
        self._connection.execute(
            f"INSERT INTO sessions ({_SESSION_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                s.id_hash,
                s.handle,
                s.user_id,
                s.csrf_hash,
                s.created_at_ms,
                s.last_seen_at_ms,
                s.expires_at_ms,
                int(s.revoked),
                s.step_up_until_ms,
            ),
        )

    def get_session(self, id_hash: str) -> Session | None:
        row = self._connection.execute(
            f"SELECT {_SESSION_COLUMNS} FROM sessions WHERE id_hash = ?", (id_hash,)
        ).fetchone()
        return _session(row) if row is not None else None

    def touch_session(self, id_hash: str, at_ms: float) -> None:
        self._connection.execute(
            "UPDATE sessions SET last_seen_at_ms = ? WHERE id_hash = ?", (at_ms, id_hash)
        )

    def revoke_session(self, id_hash: str) -> bool:
        cursor = self._connection.execute(
            "UPDATE sessions SET revoked = 1, step_up_until_ms = NULL WHERE id_hash = ?"
            " AND revoked = 0",
            (id_hash,),
        )
        return cursor.rowcount == 1

    def revoke_sessions(self, user_id: str, *, keep_id_hash: str | None = None) -> int:
        """Revoke every live session of ``user_id`` except ``keep_id_hash``. Returns how many."""
        cursor = self._connection.execute(
            "UPDATE sessions SET revoked = 1, step_up_until_ms = NULL WHERE user_id = ?"
            " AND revoked = 0"
            " AND id_hash IS NOT ?",
            (user_id, keep_id_hash),
        )
        return cursor.rowcount

    def set_step_up(self, id_hash: str, until_ms: float | None) -> None:
        """Open (or, with None, close) this one session's step-up window."""
        self._connection.execute(
            "UPDATE sessions SET step_up_until_ms = ? WHERE id_hash = ?", (until_ms, id_hash)
        )

    def clear_step_up(self, user_id: str) -> int:
        """Close the step-up window of every session of ``user_id``. Returns how many."""
        cursor = self._connection.execute(
            "UPDATE sessions SET step_up_until_ms = NULL WHERE user_id = ?"
            " AND step_up_until_ms IS NOT NULL",
            (user_id,),
        )
        return cursor.rowcount

    def list_sessions(self, user_id: str) -> list[Session]:
        rows = self._connection.execute(
            f"SELECT {_SESSION_COLUMNS} FROM sessions WHERE user_id = ? AND revoked = 0"
            " ORDER BY created_at_ms, rowid",
            (user_id,),
        ).fetchall()
        return [_session(row) for row in rows]

    def prune_sessions(self, now_ms: float, idle_ms: float, limit: int | None = None) -> int:
        """Delete sessions that are revoked, past their absolute expiry, or idle for
        ``idle_ms`` or more: none of them can be used again. Returns how many."""
        where = "revoked = 1 OR expires_at_ms <= ? OR last_seen_at_ms <= ?"
        params: tuple[float, ...] = (now_ms, now_ms - idle_ms)
        if limit is None:
            cursor = self._connection.execute(f"DELETE FROM sessions WHERE {where}", params)
        else:
            cursor = self._connection.execute(
                "DELETE FROM sessions WHERE rowid IN (SELECT rowid FROM sessions"
                f" WHERE {where} LIMIT ?)",
                (*params, limit),
            )
        return cursor.rowcount
