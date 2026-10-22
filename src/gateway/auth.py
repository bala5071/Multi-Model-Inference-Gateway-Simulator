"""Console accounts: creating users, the bootstrap admin, login, lockout, sessions, CSRF,
password changes, and the audit entries for all of these.

See docs/27-users-and-sessions.md and docs/identity-plan.md, sections 4, 5 and 9.

- **Login takes the same steps whatever is wrong.** One user lookup (an invalid username looks
  up a name no user can have), then one verify through the injected ``HashRunner``, against
  the stored hash or a dummy one made at construction with the current parameters. Only then
  is the outcome decided, and every refusal is the same ``LoginFailedError``: an unknown
  username, a wrong password, a disabled or locked account, the in-memory limiter, a used
  bootstrap credential.
- **Lockout** is in the database (5 failures within 15 minutes lock for 15 minutes, on the
  injected epoch), so a restart does not clear it. ``LoginLimiter`` counts failures for every
  username, known or not, in memory, so an unknown username "locks" exactly like a real one.
- **Sessions:** 256 random bits; the store keeps only SHA-256 digests. The CSRF token is an
  HMAC of the session id under a key derived from the pepper for that purpose alone
  (``gateway.keyderivation``), so ``/me`` can give it again without it being stored; its
  digest is stored and compared in constant time. 30 minutes idle, 12 hours in all; ``last_seen`` is
  written at most once a minute.
- **The bootstrap admin** comes from the environment only when no platform_admin exists. Its
  password works for one login; that session can only change it.

Creating users and resetting passwords hash synchronously: they run from the CLI and at
startup, where no request is being served. Login, password change and the check of the
current password go through the runner: inline on a ``SimClock`` (no simulated time passes),
on a small thread pool in the server.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from collections import OrderedDict, deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from enum import StrEnum

from gateway.applications import ApplicationStore
from gateway.audit import (
    ORGANIZATION_ID_PATTERN,
    AuditAction,
    AuditEvent,
    AuditLog,
    Outcome,
    Reason,
    TargetType,
)
from gateway.clock import Clock, EpochSource
from gateway.keyderivation import KeyPurpose, derive_key
from gateway.organizations import DEFAULT_ORGANIZATION_ID, membership_decision
from gateway.passwords import PasswordPolicy, Passwords, load_blocklist
from gateway.users import AccountStore, Bootstrap, Session, User, normalize_username

MINUTE_MS = 60_000.0
SESSION_IDLE_MS = 30 * MINUTE_MS
SESSION_ABSOLUTE_MS = 12 * 60 * MINUTE_MS
LAST_SEEN_WRITE_MS = MINUTE_MS
LOCK_FAILURES = 5
LOCK_WINDOW_MS = 15 * MINUTE_MS
LOCK_MS = 15 * MINUTE_MS
LIMITER_MAX_ENTRIES = 10_000
SESSION_BYTES = 32
ID_BYTES = 8
SESSION_COOKIE = "gw_session"
CSRF_HEADER = "X-CSRF-Token"
ADMIN_USERNAME_ENV = "GATEWAY_ADMIN_USERNAME"
ADMIN_PASSWORD_ENV = "GATEWAY_ADMIN_PASSWORD"
COOKIE_SECURE_ENV = "GATEWAY_COOKIE_SECURE"
AUDIT_RETENTION_DAYS_ENV = "GATEWAY_AUDIT_RETENTION_DAYS"
DEFAULT_AUDIT_RETENTION_DAYS = 400
DAY_MS = 24 * 60 * MINUTE_MS
PRUNE_EVERY_MS = 60 * MINUTE_MS
PRUNE_BATCH = 1000
# Looked up when the username is not even well formed: no user can have it.
_NO_SUCH_USERNAME = ""
LOGIN_FAILED_MESSAGE = "The username or password is not correct, or the account cannot log in."


class LoginFailedError(Exception):
    """Every refused login, whatever the reason. It carries nothing about the reason."""

    def __init__(self) -> None:
        super().__init__(LOGIN_FAILED_MESSAGE)


class SessionInvalidError(Exception):
    """No session, or one that is unknown, revoked, expired, or whose user is disabled."""


class CsrfError(Exception):
    """A state-changing request with a session but without its CSRF token."""


class CurrentPasswordIncorrectError(Exception):
    """A password change whose current password is wrong."""


class PasswordUnchangedError(ValueError):
    """A password change to the same password (which would keep a used bootstrap password
    working)."""

    def __init__(self) -> None:
        super().__init__("The new password must differ from the current one.")


class BootstrapError(Exception):
    """No platform_admin exists and the environment does not say how to create one."""


class BootstrapOutcome(StrEnum):
    CREATED = "created"
    EXISTS = "exists"
    # An admin exists and the bootstrap variables are still set: worth a warning.
    EXISTS_WITH_VARIABLES = "exists_with_variables"


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _token(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def csrf_token_for(csrf_key: bytes, session_id: str) -> str:
    """The CSRF token of a session: ``HMAC-SHA256(csrf_key, session id)``, where ``csrf_key``
    is the pepper's key for CSRF tokens. It can be given again without being stored, and
    cannot be made without both the session id (which a page cannot read) and the key."""
    return _token(hmac.new(csrf_key, session_id.encode(), hashlib.sha256).digest())


class LoginLimiter:
    """Login failures per username in memory, for known and unknown usernames alike.

    Keyed by an HMAC of what was typed, so the text is not held as typed. At most
    ``max_entries`` usernames are tracked; the least recently failed are forgotten first,
    which is how a flood of unique usernames can flush it (the database lockout of real
    accounts is not affected).
    """

    def __init__(
        self,
        clock: Clock,
        secret: bytes,
        *,
        max_failures: int = LOCK_FAILURES,
        window_ms: float = LOCK_WINDOW_MS,
        max_entries: int = LIMITER_MAX_ENTRIES,
    ) -> None:
        self._clock = clock
        self._secret = secret
        self._max_failures = max_failures
        self._window_ms = window_ms
        self._max_entries = max_entries
        self._failures: OrderedDict[str, deque[float]] = OrderedDict()

    def key(self, username: str) -> str:
        return hmac.new(self._secret, username.encode(), hashlib.sha256).hexdigest()

    def _recent(self, key: str) -> deque[float]:
        times = self._failures.get(key, deque())
        while times and self._clock.now_ms() - times[0] >= self._window_ms:
            times.popleft()
        return times

    def blocked(self, key: str) -> bool:
        return len(self._recent(key)) >= self._max_failures

    def record_failure(self, key: str) -> None:
        times = self._recent(key)
        times.append(self._clock.now_ms())
        self._failures[key] = times
        self._failures.move_to_end(key)
        while len(self._failures) > self._max_entries:
            self._failures.popitem(last=False)

    def clear(self, key: str) -> None:
        self._failures.pop(key, None)

    def __len__(self) -> int:
        return len(self._failures)


@dataclass(frozen=True, slots=True)
class LoginResult:
    user: User
    session: Session
    # What the browser holds: the cookie value and the CSRF token. Never stored or logged.
    session_id: str
    csrf_token: str

    def __repr__(self) -> str:
        return f"LoginResult(user={self.user!r}, <session id and token redacted>)"


@dataclass(frozen=True, slots=True)
class SessionContext:
    user: User
    session: Session
    session_id: str

    def __repr__(self) -> str:
        return f"SessionContext(user={self.user!r}, session={self.session!r})"


class AccountService:
    """Everything about console accounts, below HTTP."""

    def __init__(
        self,
        store: AccountStore,
        audit: AuditLog,
        passwords: Passwords,
        *,
        organizations: ApplicationStore,
        clock: Clock,
        epoch: EpochSource,
        secret: bytes,
        policy: PasswordPolicy | None = None,
        random_bytes: Callable[[int], bytes] = secrets.token_bytes,
        audit_retention_ms: float | None = None,
    ) -> None:
        self.store = store
        self.audit = audit
        self._passwords = passwords
        self._organizations = organizations
        self._clock = clock
        self._epoch = epoch
        self._policy = policy if policy is not None else PasswordPolicy(load_blocklist())
        self._random_bytes = random_bytes
        # One key per purpose, derived from ``secret`` (the pepper on the server); neither
        # is the pepper itself, and neither equals the other.
        self._csrf_key = derive_key(secret, KeyPurpose.CSRF_TOKEN)
        self.limiter = LoginLimiter(clock, derive_key(secret, KeyPurpose.LOGIN_LIMITER))
        self.audit_retention_ms = audit_retention_ms
        # A real argon2 hash at the current parameters, of a password nobody knows: what an
        # unknown username is verified against, so it costs what a real verify costs.
        self._dummy_hash = passwords.hasher.hash(_token(random_bytes(SESSION_BYTES)))

    def __repr__(self) -> str:
        return "AccountService(<secrets redacted>)"

    def _now(self) -> float:
        return self._epoch.epoch_ms()

    def _record(self, event: AuditEvent) -> None:
        self.audit.write(event, self._now())

    # --- users ---------------------------------------------------------------------------

    def create_user(
        self,
        username: str,
        password: str,
        organization_id: str,
        *,
        platform_admin: bool = False,
        must_change_password: bool = False,
        bootstrap: Bootstrap = Bootstrap.NONE,
        reason: Reason = Reason.CLI,
    ) -> User:
        """Create an account: the organization must exist and be enabled, its kind must take
        another member (an individual organization takes one), the username must be free and
        the password must pass the policy. Hashes synchronously (CLI and startup only)."""
        name = normalize_username(username)
        organization = self._organizations.get_organization(organization_id)
        if organization is None:
            raise ValueError(f"no organization {organization_id!r}")
        if organization.disabled:
            raise ValueError(f"organization {organization_id!r} is disabled")
        decision = membership_decision(organization.kind, self.store.count_members(organization.id))
        if not decision.allowed:
            raise ValueError(
                f"organization {organization_id!r} is an individual organization and already "
                "has its one member"
            )
        if self.store.find_user(name) is not None:
            raise ValueError(f"username {name!r} is taken")
        self._policy.check(password, name)
        user = User(
            id="usr_" + self._random_bytes(ID_BYTES).hex(),
            username=name,
            password_hash=self._passwords.hasher.hash(password),
            organization_id=organization.id,
            created_at_ms=self._now(),
            must_change_password=must_change_password,
            bootstrap=bootstrap,
            platform_admin=platform_admin,
        )
        details: dict[str, object] = {"platform_admin": platform_admin, "reason": reason}
        if ORGANIZATION_ID_PATTERN.fullmatch(organization.id):
            details["organization_id"] = organization.id
        event = AuditEvent(
            AuditAction.USER_CREATED, Outcome.SUCCESS, TargetType.USER, user.id, details=details
        )
        self.store.add_user(user)
        self._record(event)
        return user

    def bootstrap_admin(self, environ: Mapping[str, str]) -> BootstrapOutcome:
        """Create the first platform_admin from the environment if there is none. Never
        touches an existing one."""
        username = environ.get(ADMIN_USERNAME_ENV)
        password = environ.get(ADMIN_PASSWORD_ENV)
        if self.store.platform_admin_exists():
            if username is not None or password is not None:
                return BootstrapOutcome.EXISTS_WITH_VARIABLES
            return BootstrapOutcome.EXISTS
        if not username or not password:
            raise BootstrapError(
                f"no platform_admin exists. Set {ADMIN_USERNAME_ENV} and {ADMIN_PASSWORD_ENV} "
                "to create the first one when the server starts (it must change the password "
                "at its first login), or create one with `uv run python scripts/manage.py "
                "create-user --platform-admin`."
            )
        try:
            self.create_user(
                username,
                password,
                DEFAULT_ORGANIZATION_ID,
                platform_admin=True,
                must_change_password=True,
                bootstrap=Bootstrap.UNUSED,
                reason=Reason.BOOTSTRAP,
            )
        except ValueError as exc:  # the policy's message names the rule, never the password
            raise BootstrapError(
                f"{ADMIN_USERNAME_ENV}/{ADMIN_PASSWORD_ENV} cannot create the admin: {exc}"
            ) from None
        return BootstrapOutcome.CREATED

    def _user_by_name(self, username: str) -> User:
        user = self.store.find_user(normalize_username(username))
        if user is None:
            raise ValueError(f"no user {normalize_username(username)!r}")
        return user

    def _revoke_all(self, user: User, reason: Reason, keep: str | None = None) -> int:
        revoked = self.store.revoke_sessions(user.id, keep_id_hash=keep)
        if revoked:
            self._record(
                AuditEvent(
                    AuditAction.SESSIONS_REVOKED,
                    Outcome.SUCCESS,
                    TargetType.USER,
                    user.id,
                    details={"sessions_revoked": revoked, "reason": reason},
                )
            )
        return revoked

    def disable_user(self, username: str) -> int:
        """Disable an account and revoke all its sessions. Returns how many were revoked."""
        user = self._user_by_name(username)
        self.store.set_disabled(user.id, True)
        self._record(
            AuditEvent(AuditAction.USER_DISABLED, Outcome.SUCCESS, TargetType.USER, user.id)
        )
        return self._revoke_all(user, Reason.USER_DISABLED)

    def enable_user(self, username: str) -> None:
        user = self._user_by_name(username)
        self.store.set_disabled(user.id, False)
        self._record(
            AuditEvent(AuditAction.USER_ENABLED, Outcome.SUCCESS, TargetType.USER, user.id)
        )

    def reset_password(self, username: str, new_password: str) -> int:
        """Set a temporary password the user must change, clear any lock and revoke every
        session. Hashes synchronously (CLI only). Returns how many sessions were revoked."""
        user = self._user_by_name(username)
        self._policy.check(new_password, user.username)
        self.store.set_password(
            user.id,
            self._passwords.hasher.hash(new_password),
            must_change=True,
            bootstrap=Bootstrap.NONE,
        )
        self.store.set_login_state(user.id, 0, None, None)
        self._record(
            AuditEvent(AuditAction.PASSWORD_RESET, Outcome.SUCCESS, TargetType.USER, user.id)
        )
        return self._revoke_all(user, Reason.PASSWORD_RESET)

    def unlock_user(self, username: str) -> None:
        user = self._user_by_name(username)
        self.store.set_login_state(user.id, 0, None, None)
        self._record(
            AuditEvent(AuditAction.USER_UNLOCKED, Outcome.SUCCESS, TargetType.USER, user.id)
        )

    # --- login ---------------------------------------------------------------------------

    async def login(self, username: str, password: str) -> LoginResult:
        """A new session, or ``LoginFailedError`` through the same steps whatever is wrong."""
        try:
            name: str | None = normalize_username(username)
        except ValueError:
            name = None
        user = self.store.find_user(name if name is not None else _NO_SUCH_USERNAME)
        stored = user.password_hash if user is not None else self._dummy_hash
        verification = await self._passwords.check(stored, password)
        now = self._now()
        key = self.limiter.key(name if name is not None else username)
        reason: Reason | None = None
        if user is None:
            reason = Reason.UNKNOWN_USER
        elif user.disabled:
            reason = Reason.DISABLED
        elif user.locked_until_ms is not None and now < user.locked_until_ms:
            reason = Reason.LOCKED
        elif not verification.valid:
            reason = Reason.WRONG_PASSWORD
        elif self.limiter.blocked(key):
            reason = Reason.RATE_LIMITED
        elif user.bootstrap is Bootstrap.USED and user.must_change_password:
            reason = Reason.BOOTSTRAP_USED
        if reason is not None:
            self._failed(user, reason, key, now)
            raise LoginFailedError()
        assert user is not None
        self.limiter.clear(key)
        if user.failed_logins or user.locked_until_ms is not None:
            self.store.set_login_state(user.id, 0, None, None)
        if verification.upgraded_hash is not None:
            self.store.set_password_hash(user.id, verification.upgraded_hash)
        if user.bootstrap is Bootstrap.UNUSED:
            self.store.set_bootstrap(user.id, Bootstrap.USED)
        session_id, session = self._new_session(user, now)
        self._record(
            AuditEvent(
                AuditAction.LOGIN,
                Outcome.SUCCESS,
                TargetType.SESSION,
                session.handle,
                user.id,
                details={"password_rehashed": verification.upgraded_hash is not None},
            )
        )
        current = self.store.get_user(user.id)
        assert current is not None
        return LoginResult(current, session, session_id, csrf_token_for(self._csrf_key, session_id))

    def _failed(self, user: User | None, reason: Reason, key: str, now: float) -> None:
        self.limiter.record_failure(key)
        if user is None:
            self._record(
                AuditEvent(
                    AuditAction.LOGIN_FAILED,
                    Outcome.FAILURE,
                    TargetType.UNKNOWN_USER,
                    details={"reason": reason},
                )
            )
            return
        details: dict[str, object] = {"reason": reason}
        if reason is Reason.WRONG_PASSWORD:
            details["failures"] = self._count_failure(user, now)
        self._record(
            AuditEvent(
                AuditAction.LOGIN_FAILED, Outcome.FAILURE, TargetType.USER, user.id, details=details
            )
        )

    def _count_failure(self, user: User, now: float) -> int:
        """Count a wrong password against the account; lock it at the fifth within the
        window. Returns the count."""
        first = user.first_failure_at_ms
        if first is None or now - first >= LOCK_WINDOW_MS:
            failures, first = 1, now
        else:
            failures = user.failed_logins + 1
        locked_until = now + LOCK_MS if failures >= LOCK_FAILURES else None
        if locked_until is not None:
            self.store.set_login_state(user.id, 0, None, locked_until)
            self._record(
                AuditEvent(
                    AuditAction.LOGIN_LOCKED,
                    Outcome.INFO,
                    TargetType.USER,
                    user.id,
                    details={"failures": failures, "locked_until_ms": locked_until},
                )
            )
        else:
            self.store.set_login_state(user.id, failures, first, user.locked_until_ms)
        return failures

    def _new_session(self, user: User, now: float) -> tuple[str, Session]:
        session_id = _token(self._random_bytes(SESSION_BYTES))
        session = Session(
            id_hash=_digest(session_id),
            handle="ses_" + self._random_bytes(ID_BYTES).hex(),
            user_id=user.id,
            csrf_hash=_digest(csrf_token_for(self._csrf_key, session_id)),
            created_at_ms=now,
            last_seen_at_ms=now,
            expires_at_ms=now + SESSION_ABSOLUTE_MS,
        )
        self.store.add_session(session)
        return session_id, session

    # --- sessions ------------------------------------------------------------------------

    def _live(self, session: Session | None, now: float) -> bool:
        return (
            session is not None
            and not session.revoked
            and now < session.expires_at_ms
            and now - session.last_seen_at_ms < SESSION_IDLE_MS
        )

    def authenticate(self, session_id: str | None) -> SessionContext:
        """The session and its user, or ``SessionInvalidError``. Notes the time it was seen,
        writing it at most once a minute."""
        if not session_id:
            raise SessionInvalidError()
        session = self.store.get_session(_digest(session_id))
        now = self._now()
        if session is None or not self._live(session, now):
            raise SessionInvalidError()
        user = self.store.get_user(session.user_id)
        if user is None or user.disabled:
            raise SessionInvalidError()
        if now - session.last_seen_at_ms >= LAST_SEEN_WRITE_MS:
            self.store.touch_session(session.id_hash, now)
            session = replace(session, last_seen_at_ms=now)
        return SessionContext(user, session, session_id)

    def csrf_token(self, context: SessionContext) -> str:
        """The session's CSRF token, made again from its id (it is never stored)."""
        return csrf_token_for(self._csrf_key, context.session_id)

    @staticmethod
    def check_csrf(context: SessionContext, token: str | None) -> None:
        if not hmac.compare_digest(_digest(token or ""), context.session.csrf_hash):
            raise CsrfError()

    def logout(self, context: SessionContext) -> None:
        self.store.revoke_session(context.session.id_hash)
        self._record(
            AuditEvent(
                AuditAction.LOGOUT,
                Outcome.SUCCESS,
                TargetType.SESSION,
                context.session.handle,
                context.user.id,
                details={"reason": Reason.LOGOUT},
            )
        )

    def live_sessions(self, context: SessionContext) -> list[Session]:
        now = self._now()
        return [s for s in self.store.list_sessions(context.user.id) if self._live(s, now)]

    async def change_password(self, context: SessionContext, current: str, new: str) -> int:
        """Change the password: the current one must be right (a wrong one counts towards
        the lockout), the new one must differ and pass the policy. Revokes every other
        session of the user. Returns how many."""
        user = context.user
        if not await self._passwords.verify(user.password_hash, current):
            now = self._now()
            failures = self._count_failure(user, now)
            self._record(
                AuditEvent(
                    AuditAction.PASSWORD_CHANGE_FAILED,
                    Outcome.FAILURE,
                    TargetType.USER,
                    user.id,
                    user.id,
                    details={"reason": Reason.WRONG_PASSWORD, "failures": failures},
                )
            )
            raise CurrentPasswordIncorrectError()
        if new == current:
            raise PasswordUnchangedError()
        self._policy.check(new, user.username)
        new_hash = await self._passwords.hash(new)
        self.store.set_password(user.id, new_hash, must_change=False, bootstrap=Bootstrap.NONE)
        self.store.set_login_state(user.id, 0, None, None)
        self._record(
            AuditEvent(
                AuditAction.PASSWORD_CHANGED, Outcome.SUCCESS, TargetType.USER, user.id, user.id
            )
        )
        return self._revoke_all(user, Reason.PASSWORD_CHANGED, keep=context.session.id_hash)

    # --- retention -----------------------------------------------------------------------

    def prune(self, limit: int | None = None) -> int:
        """Delete dead sessions and (with a retention) old audit entries, at most ``limit``
        of each; write any pending count of dropped audit rows. Returns how many rows went."""
        now = self._now()
        self.audit.flush_dropped(now)
        removed = self.store.prune_sessions(now, SESSION_IDLE_MS, limit)
        if self.audit_retention_ms is not None:
            removed += self.audit.prune(now - self.audit_retention_ms, limit)
        return removed


@dataclass(frozen=True, slots=True)
class AccountAccess:
    """What ``create_app`` needs to serve console logins: the account service and whether the
    session cookie is marked ``Secure`` (``GATEWAY_COOKIE_SECURE=1``; off by default, because
    the server is plain HTTP on 127.0.0.1)."""

    service: AccountService
    cookie_secure: bool = False


def session_cookie(value: str, *, secure: bool, clear: bool = False) -> str:
    """The ``Set-Cookie`` header for the session: HttpOnly, SameSite=Strict, Path=/, and
    Secure only when asked. A session cookie (no Expires): the server enforces the expiry."""
    parts = [f"{SESSION_COOKIE}={value}", "HttpOnly", "Path=/", "SameSite=Strict"]
    if secure:
        parts.append("Secure")
    if clear:
        parts.append("Max-Age=0")
    return "; ".join(parts)


def audit_retention_ms(environ: Mapping[str, str]) -> float:
    """``GATEWAY_AUDIT_RETENTION_DAYS`` (default 400) in milliseconds."""
    raw = environ.get(AUDIT_RETENTION_DAYS_ENV, str(DEFAULT_AUDIT_RETENTION_DAYS))
    try:
        days = float(raw)
    except ValueError:
        raise ValueError(f"{AUDIT_RETENTION_DAYS_ENV} must be a number of days") from None
    if not days > 0:
        raise ValueError(f"{AUDIT_RETENTION_DAYS_ENV} must be positive")
    return days * DAY_MS


async def run_account_retention(
    service: AccountService,
    clock: Clock,
    *,
    batch: int = PRUNE_BATCH,
    every_ms: float = PRUNE_EVERY_MS,
) -> None:
    """Prune sessions and audit entries now and then every ``every_ms``, in batches, until
    cancelled. Beside the server; nothing on the request path prunes."""
    if batch < 1:
        raise ValueError("batch must be at least 1")
    while True:
        while service.prune(batch) >= batch:
            await clock.sleep_ms(0)
        await clock.sleep_ms(every_ms)
