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
from gateway.management import (
    ManagementRefusal,
    NotPermittedError,
    Page,
    check_page_size,
    page_of,
)
from gateway.organizations import (
    DEFAULT_ORGANIZATION_ID,
    Organization,
    OrganizationKind,
    membership_decision,
)
from gateway.passwords import PasswordPolicy, PasswordPolicyError, Passwords, load_blocklist
from gateway.permissions import (
    PERMISSIONS,
    PLATFORM_SCOPE_ID,
    ROLE_SCOPES,
    Actor,
    Assignment,
    Resource,
    Role,
    ScopeType,
    authorize,
)
from gateway.roles import RoleStore
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
STEP_UP_MINUTES_ENV = "GATEWAY_STEP_UP_MINUTES"
DEFAULT_STEP_UP_MINUTES = 5.0
STEP_UP_SESSION_FAILURES_ENV = "GATEWAY_STEP_UP_SESSION_FAILURES"
DEFAULT_STEP_UP_SESSION_FAILURES = 3
STEP_UP_SESSION_WINDOW_MS = 5 * MINUTE_MS
STEP_UP_FAILED_MESSAGE = "Step-up was refused: the password was not accepted."
STEP_UP_REQUIRED_MESSAGE = "This action needs a recent password: POST /v1/auth/step-up."
# User-creation attempts a session may make in an hour (docs/30): a cap on probing which
# usernames exist, since usernames are unique across organizations.
USER_CREATE_ATTEMPTS_ENV = "GATEWAY_USER_CREATE_ATTEMPTS"
DEFAULT_USER_CREATE_ATTEMPTS = 10
USER_CREATE_WINDOW_MS = 60 * MINUTE_MS
# A temporary password: 18 random bytes, 24 characters of base64url.
TEMPORARY_PASSWORD_BYTES = 18
TEMPORARY_PASSWORD_DRAWS = 100


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


class StepUpFailedError(Exception):
    """Every refused step-up, whatever the reason. It carries nothing about the reason."""


class StepUpRequiredError(Exception):
    """An action flagged for step-up, on a session whose window is not open."""


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


class LastAdminError(ManagementRefusal):
    """Disabling or revoking would leave an organization without an org_admin, or the platform
    without a platform_admin."""

    def __init__(self, message: str) -> None:
        super().__init__(Reason.LAST_ADMIN, message)


class AccountService:
    """Everything about console accounts, below HTTP.

    Methods that change accounts or roles take an ``actor``: the user acting, checked with
    ``authorize``; or None for the operator at the CLI, who has the database anyway and is a
    member of no organization.
    """

    def __init__(
        self,
        store: AccountStore,
        audit: AuditLog,
        passwords: Passwords,
        *,
        roles: RoleStore,
        organizations: ApplicationStore,
        clock: Clock,
        epoch: EpochSource,
        secret: bytes,
        policy: PasswordPolicy | None = None,
        random_bytes: Callable[[int], bytes] = secrets.token_bytes,
        audit_retention_ms: float | None = None,
        step_up_minutes: float = DEFAULT_STEP_UP_MINUTES,
        step_up_session_failures: int = DEFAULT_STEP_UP_SESSION_FAILURES,
        user_create_attempts: int = DEFAULT_USER_CREATE_ATTEMPTS,
    ) -> None:
        if not 1 <= step_up_minutes <= 60:
            raise ValueError("step-up lasts from 1 to 60 minutes")
        if step_up_session_failures < 1:
            raise ValueError("step_up_session_failures must be at least 1")
        if not 1 <= user_create_attempts <= 1000:
            raise ValueError("user_create_attempts must be from 1 to 1000")
        self.store = store
        self.audit = audit
        self.roles = roles
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
        self.step_up_ms = step_up_minutes * MINUTE_MS
        # Failed step-ups per session (by its digest), in memory, on the injected clock:
        # past the cap, the endpoint refuses without verifying.
        self._step_up_failures = LoginLimiter(
            clock,
            derive_key(secret, KeyPurpose.LOGIN_LIMITER),
            max_failures=step_up_session_failures,
            window_ms=STEP_UP_SESSION_WINDOW_MS,
        )
        # User-creation attempts per session (by its digest), in memory: every attempt counts,
        # whatever its outcome, so the cap bounds how many usernames a session can probe.
        self._creation_attempts = LoginLimiter(
            clock,
            derive_key(secret, KeyPurpose.LOGIN_LIMITER),
            max_failures=user_create_attempts,
            window_ms=USER_CREATE_WINDOW_MS,
        )
        # A real argon2 hash at the current parameters, of a password nobody knows: what an
        # unknown username is verified against, so it costs what a real verify costs.
        self._dummy_hash = passwords.hasher.hash(_token(random_bytes(SESSION_BYTES)))

    def __repr__(self) -> str:
        return "AccountService(<secrets redacted>)"

    @property
    def organizations(self) -> ApplicationStore:
        return self._organizations

    def _now(self) -> float:
        return self._epoch.epoch_ms()

    def _record(self, event: AuditEvent) -> None:
        self.audit.write(event, self._now())

    # --- actors --------------------------------------------------------------------------

    def actor_for(self, user: User) -> Actor:
        """The user as ``authorize`` sees them, read now: a removed role is already gone."""
        organization = self._organizations.get_organization(user.organization_id)
        return Actor(
            user.id,
            user.organization_id,
            self.roles.assignments_for(user.id),
            disabled=user.disabled,
            organization_disabled=organization is None or organization.disabled,
        )

    def actor_named(self, username: str) -> Actor:
        return self.actor_for(self._user_by_name(username))

    @staticmethod
    def _who(actor: Actor | None) -> tuple[str | None, dict[str, object]]:
        """Whom an audit entry names as its actor, and any ``acting_as``: a user acting
        for itself is the actor; the operator at the CLI is recorded as no user, and when it
        acts within a user's roles (--as) that user is ``acting_as``, never the actor."""
        if actor is None:
            return None, {}
        if actor.via_operator:
            return None, {"acting_as": actor.user_id}
        return actor.user_id, {}

    def _user_event(
        self, action: AuditAction, user: User, actor: Actor | None, **details: object
    ) -> AuditEvent:
        who, acting = self._who(actor)
        return AuditEvent(
            action, Outcome.SUCCESS, TargetType.USER, user.id, who, {**details, **acting}
        )

    @staticmethod
    def _require(actor: Actor | None, action: str, resource: Resource) -> None:
        if actor is None:
            return  # the operator at the CLI
        if not authorize(actor, action, resource).allowed:
            raise NotPermittedError(f"not permitted: {action}")

    # --- users ---------------------------------------------------------------------------

    def _check_new_user(
        self, name: str, organization_id: str, actor: Actor | None, platform_admin: bool
    ) -> Organization:
        """Whether ``name`` may be created in the organization by ``actor``: the actor may
        create users there, the organization exists and is enabled, its kind takes another
        member, and the username is free (in every organization)."""
        organization = self._organizations.get_organization(organization_id)
        if organization is None:
            raise ValueError(f"no organization {organization_id!r}")
        self._require(actor, "users.create", Resource.organization(organization.id))
        if platform_admin and actor is not None:
            raise NotPermittedError("platform_admin is granted only by the operator, at the CLI")
        if organization.disabled:
            raise ManagementRefusal(
                Reason.ORGANIZATION_DISABLED, f"organization {organization_id!r} is disabled"
            )
        decision = membership_decision(organization.kind, self.store.count_members(organization.id))
        if not decision.allowed:
            raise ManagementRefusal(
                Reason.ORGANIZATION_FULL,
                f"organization {organization_id!r} is an individual organization and already "
                "has its one member",
            )
        if self.store.find_user(name) is not None:
            raise ManagementRefusal(Reason.USERNAME_TAKEN, f"username {name!r} is taken")
        return organization

    def create_user(
        self,
        username: str,
        password: str,
        organization_id: str,
        *,
        actor: Actor | None = None,
        platform_admin: bool = False,
        must_change_password: bool = False,
        bootstrap: Bootstrap = Bootstrap.NONE,
        reason: Reason = Reason.CLI,
    ) -> User:
        """Create an account: the checks of ``_check_new_user`` (an individual organization
        takes one member, who becomes its org_admin), and the password passes the policy.

        An account made by a platform_admin who is not a member of its organization, or by
        the operator at the CLI, must change its password at first login: whoever made it
        knows that password. Hashes synchronously (CLI and startup only)."""
        name = normalize_username(username)
        organization = self._check_new_user(name, organization_id, actor, platform_admin)
        self._policy.check(password, name)
        outsider = actor is None or (
            actor.holds(Role.PLATFORM_ADMIN) and actor.organization_id != organization.id
        )
        return self._add_user(
            name,
            self._passwords.hasher.hash(password),
            organization,
            actor,
            platform_admin=platform_admin,
            must_change_password=must_change_password or outsider,
            bootstrap=bootstrap,
            reason=reason,
        )

    async def create_user_with_temporary_password(
        self, username: str, organization_id: str, *, actor: Actor
    ) -> tuple[User, str]:
        """Create an account with a temporary password the server makes (the management
        endpoint, docs/30): the same checks as ``create_user``, hashing through the runner, and
        the account must change the password at first login. Returns the user and the
        password, which the caller shows once and nowhere else."""
        name = normalize_username(username)
        self._check_new_user(name, organization_id, actor, False)
        temporary = self._temporary_password(name)
        password_hash = await self._passwords.hash(temporary)
        # Checked again with the insert, in one transaction: the hash may have let another
        # request in (the thread runner yields), and it may have taken the name or the seat.
        with self.store.immediate():
            organization = self._check_new_user(name, organization_id, actor, False)
            user = self._add_user(
                name,
                password_hash,
                organization,
                actor,
                platform_admin=False,
                must_change_password=True,
                bootstrap=Bootstrap.NONE,
                reason=Reason.TEMPORARY_PASSWORD,
            )
        return user, temporary

    def _temporary_password(self, username: str) -> str:
        """A password nobody chose: random, drawn again until the policy accepts it."""
        for _ in range(TEMPORARY_PASSWORD_DRAWS):
            candidate = _token(self._random_bytes(TEMPORARY_PASSWORD_BYTES))
            try:
                self._policy.check(candidate, username)
            except PasswordPolicyError:
                continue
            return candidate
        raise RuntimeError("no temporary password the policy accepts was drawn")

    def _add_user(
        self,
        name: str,
        password_hash: str,
        organization: Organization,
        actor: Actor | None,
        *,
        platform_admin: bool,
        must_change_password: bool,
        bootstrap: Bootstrap,
        reason: Reason,
    ) -> User:
        user = User(
            id="usr_" + self._random_bytes(ID_BYTES).hex(),
            username=name,
            password_hash=password_hash,
            organization_id=organization.id,
            created_at_ms=self._now(),
            must_change_password=must_change_password,
            bootstrap=bootstrap,
        )
        details: dict[str, object] = {
            "platform_admin": platform_admin,
            "must_change_password": user.must_change_password,
            "reason": reason,
        }
        if ORGANIZATION_ID_PATTERN.fullmatch(organization.id):
            details["organization_id"] = organization.id
        event = self._user_event(AuditAction.USER_CREATED, user, actor, **details)
        self.store.add_user(user)
        self._record(event)
        if platform_admin:
            platform = Assignment(Role.PLATFORM_ADMIN.value, ScopeType.PLATFORM, "*")
            self._grant(user, platform, None)
        if organization.kind is OrganizationKind.INDIVIDUAL:
            # The one member of an individual organization is its org_admin.
            org_admin = Assignment(Role.ORG_ADMIN.value, ScopeType.ORGANIZATION, organization.id)
            self._grant(user, org_admin, actor)
        return user

    def bootstrap_admin(self, environ: Mapping[str, str]) -> BootstrapOutcome:
        """Create the first platform_admin from the environment if there is none. Never
        touches an existing one."""
        username = environ.get(ADMIN_USERNAME_ENV)
        password = environ.get(ADMIN_PASSWORD_ENV)
        if self.roles.platform_admin_exists():
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

    def _guard_last_admin(self, user: User, losing: tuple[Assignment, ...]) -> None:
        """Refuse if ``user`` losing ``losing`` leaves an organization with no enabled
        org_admin, or the platform with no enabled platform_admin."""
        for assignment in losing:
            if assignment.role not in (Role.ORG_ADMIN.value, Role.PLATFORM_ADMIN.value):
                continue
            holders = self.roles.enabled_holders(assignment)
            if user.id in holders and len(holders) == 1:
                where = (
                    "the platform"
                    if assignment.scope_type is ScopeType.PLATFORM
                    else f"organization {assignment.scope_id!r}"
                )
                raise LastAdminError(
                    f"{user.username!r} is the last {assignment.role} of {where}: grant the role "
                    "to someone else first"
                )

    @staticmethod
    def _not_own_account(actor: Actor | None, user: User) -> None:
        """A user acting for itself may not disable or reset its own account (docs/30); the
        operator at the CLI, also with --as, may."""
        if actor is not None and not actor.via_operator and actor.user_id == user.id:
            raise ManagementRefusal(
                Reason.OWN_ACCOUNT,
                "this cannot be done to your own account: ask another admin, or change your "
                "password at POST /v1/auth/change-password",
            )

    def disable_user(self, username: str, *, actor: Actor | None = None) -> int:
        """Disable an account and revoke all its sessions. Returns how many were revoked.
        Refused for the last org_admin of an organization or the last platform_admin, and for
        an actor's own account."""
        user = self._user_by_name(username)
        self._require(actor, "users.disable", Resource.organization(user.organization_id))
        self._not_own_account(actor, user)
        # The count of the other admins and the change are one transaction: no other
        # connection can remove the last other admin in between.
        with self.store.immediate():
            self._guard_last_admin(user, self.roles.assignments_for(user.id))
            self.store.set_disabled(user.id, True)
            self.store.clear_step_up(user.id)
            self._record(self._user_event(AuditAction.USER_DISABLED, user, actor))
            return self._revoke_all(user, Reason.USER_DISABLED)

    def enable_user(self, username: str, *, actor: Actor | None = None) -> None:
        user = self._user_by_name(username)
        self._require(actor, "users.enable", Resource.organization(user.organization_id))
        self.store.set_disabled(user.id, False)
        self._record(self._user_event(AuditAction.USER_ENABLED, user, actor))

    def reset_password(
        self, username: str, new_password: str, *, actor: Actor | None = None
    ) -> int:
        """Set a temporary password the user must change, clear any lock and revoke every
        session. Hashes synchronously (CLI only). Returns how many sessions were revoked."""
        user = self._user_by_name(username)
        self._require(actor, "users.reset_password", Resource.organization(user.organization_id))
        self._not_own_account(actor, user)
        self._policy.check(new_password, user.username)
        return self._apply_reset(user, self._passwords.hasher.hash(new_password), actor)

    async def reset_to_temporary_password(self, username: str, *, actor: Actor) -> tuple[int, str]:
        """``reset_password`` with a password the server makes (the management endpoint,
        docs/30), hashed through the runner. Returns how many sessions were revoked and the
        password, which the caller shows once and nowhere else."""
        user = self._user_by_name(username)
        self._require(actor, "users.reset_password", Resource.organization(user.organization_id))
        self._not_own_account(actor, user)
        temporary = self._temporary_password(user.username)
        password_hash = await self._passwords.hash(temporary)
        return self._apply_reset(user, password_hash, actor), temporary

    def _apply_reset(self, user: User, password_hash: str, actor: Actor | None) -> int:
        """The new hash, to be changed at the next login; no lock; every session revoked, which
        closes every step-up window with it (``revoke_sessions`` clears them in the same
        update)."""
        self.store.set_password(user.id, password_hash, must_change=True, bootstrap=Bootstrap.NONE)
        self.store.set_login_state(user.id, 0, None, None)
        self._record(self._user_event(AuditAction.PASSWORD_RESET, user, actor))
        return self._revoke_all(user, Reason.PASSWORD_RESET)

    def unlock_user(self, username: str, *, actor: Actor | None = None) -> None:
        user = self._user_by_name(username)
        self._require(actor, "users.unlock", Resource.organization(user.organization_id))
        self.store.set_login_state(user.id, 0, None, None)
        self._record(self._user_event(AuditAction.USER_UNLOCKED, user, actor))

    def page_users(
        self, organization_id: str, after: str | None, limit: int, *, actor: Actor | None = None
    ) -> Page[User]:
        """One page of an organization's users, by username."""
        self._require(actor, "users.list", Resource.organization(organization_id))
        found = self.store.page_users(organization_id, after, check_page_size(limit) + 1)
        return page_of(found, limit, lambda u: u.username)

    def is_locked(self, user: User) -> bool:
        return user.locked_until_ms is not None and self._now() < user.locked_until_ms

    # --- management requests -------------------------------------------------------------

    def count_user_creation(self, context: SessionContext) -> None:
        """Count one user-creation attempt by this session, or refuse it past the cap
        (``GATEWAY_USER_CREATE_ATTEMPTS`` an hour): every attempt counts, whatever its outcome,
        so a session cannot probe many usernames."""
        key = context.session.id_hash
        if self._creation_attempts.blocked(key):
            raise ManagementRefusal(
                Reason.USER_CREATION_LIMITED,
                "this session has tried to create too many users this hour; try again later",
            )
        self._creation_attempts.record_failure(key)

    def record_refusal(
        self, actor: Actor, action: str, organization_id: str | None, reason: Reason
    ) -> None:
        """Audit a management request refused for a conflict or a cap: who, which action,
        which organization, and why; never anything that was typed."""
        who, acting = self._who(actor)
        target = organization_id
        if target is not None and not ORGANIZATION_ID_PATTERN.fullmatch(target):
            target = None
        self._record(
            AuditEvent(
                AuditAction.MANAGEMENT_REFUSED,
                Outcome.FAILURE,
                TargetType.ORGANIZATION,
                target,
                who,
                {"reason": reason, "action": action, **acting},
            )
        )

    # --- roles ---------------------------------------------------------------------------

    def _assignment(
        self, user: User, role: str, scope_type: str, scope_id: str, *, defined: bool
    ) -> Assignment:
        """The assignment, checked: the role may be held in that scope, and the scope lies in
        the user's own organization (the platform scope aside). With ``defined``, an
        application-defined role must be defined by that application."""
        kind = ScopeType(scope_type)
        if kind is ScopeType.PLATFORM:
            if scope_id != PLATFORM_SCOPE_ID:
                raise ValueError("the platform scope has no id")
        elif kind is ScopeType.ORGANIZATION:
            if scope_id != user.organization_id:
                raise ValueError("a role is held only within the user's own organization")
        else:
            application = self._organizations.get_application(scope_id)
            if application is None or application.organization_id != user.organization_id:
                raise ValueError("a role is held only within the user's own organization")
        if role in {r.value for r in Role}:
            if kind not in ROLE_SCOPES[Role(role)]:
                raise ValueError(f"{role} is not held at {kind} scope")
        else:
            if kind is not ScopeType.APPLICATION:
                raise ValueError("an application-defined role is held at application scope")
            if defined and role not in self.roles.app_roles(scope_id):
                raise ValueError(f"application {scope_id!r} defines no role {role!r}")
        return Assignment(role, kind, scope_id)

    def _grant(self, user: User, assignment: Assignment, actor: Actor | None) -> None:
        granted_by, _ = self._who(actor)
        self.roles.grant(user.id, assignment, granted_by, self._now())
        self.store.clear_step_up(user.id)  # roles changed: a new step-up is needed
        self._record(self._role_event(AuditAction.ROLE_GRANTED, user, assignment, actor))

    def _role_event(
        self, action: AuditAction, user: User, assignment: Assignment, actor: Actor | None
    ) -> AuditEvent:
        details: dict[str, object] = {"scope_type": assignment.scope_type.value}
        for key, value in (("role", assignment.role), ("scope_id", assignment.scope_id)):
            if ORGANIZATION_ID_PATTERN.fullmatch(value) or value == PLATFORM_SCOPE_ID:
                details[key] = value
        return self._user_event(action, user, actor, **details)

    def _check_granting(self, actor: Actor | None, role: str) -> None:
        """Which roles an actor may grant or revoke, beyond the table's ``roles.*`` rows."""
        if actor is None:
            return  # the operator at the CLI may grant anything, platform_admin included
        if role == Role.PLATFORM_ADMIN.value:
            raise NotPermittedError("platform_admin is granted and revoked only by the operator")
        org_admin_here = any(
            a.role == Role.ORG_ADMIN.value and a.scope_type is ScopeType.ORGANIZATION
            for a in actor.assignments
        )
        if not org_admin_here and role != Role.ORG_ADMIN.value:
            # A platform_admin outside an organization only appoints its org_admin.
            raise NotPermittedError("a platform_admin may grant only org_admin")

    def grant_role(
        self,
        username: str,
        role: str,
        scope_type: str,
        scope_id: str,
        *,
        actor: Actor | None = None,
    ) -> None:
        user = self._user_by_name(username)
        self._require(actor, "roles.assign", Resource.organization(user.organization_id))
        self._check_granting(actor, role)
        assignment = self._assignment(user, role, scope_type, scope_id, defined=True)
        self._grant(user, assignment, actor)

    def revoke_role(
        self,
        username: str,
        role: str,
        scope_type: str,
        scope_id: str,
        *,
        actor: Actor | None = None,
    ) -> None:
        user = self._user_by_name(username)
        self._require(actor, "roles.revoke", Resource.organization(user.organization_id))
        self._check_granting(actor, role)
        assignment = self._assignment(user, role, scope_type, scope_id, defined=False)
        with self.store.immediate():  # the count and the revocation together
            self._guard_last_admin(user, (assignment,))
            if not self.roles.revoke(user.id, assignment):
                raise ValueError(f"{user.username!r} does not hold that role in that scope")
            self.store.clear_step_up(user.id)  # roles changed: a new step-up is needed
            self._record(self._role_event(AuditAction.ROLE_REVOKED, user, assignment, actor))

    def list_roles(self, username: str, *, actor: Actor | None = None) -> tuple[Assignment, ...]:
        user = self._user_by_name(username)
        self._require(actor, "roles.list", Resource.organization(user.organization_id))
        return self.roles.assignments_for(user.id)

    # --- step-up -------------------------------------------------------------------------

    def _session_event(
        self, event: AuditAction, outcome: Outcome, context: SessionContext, **details: object
    ) -> AuditEvent:
        return AuditEvent(
            event, outcome, TargetType.SESSION, context.session.handle, context.user.id, details
        )

    async def step_up(self, context: SessionContext, password: str) -> float:
        """Open this session's step-up window, or ``StepUpFailedError`` for any refusal.

        Past the per-session cap of failed attempts it refuses at once, without verifying:
        faster than the other refusals, which is acceptable because the caller is already
        authenticated (docs/29). Otherwise the same steps whatever is wrong: one verify of the
        session user's password through the runner, then the decision. A wrong password counts
        toward the account lockout and the login limiter, exactly like a failed login, and
        toward this session's cap. Returns the window's end (epoch ms)."""
        user, session = context.user, context.session
        if self._step_up_failures.blocked(session.id_hash):
            self._record(
                self._session_event(
                    AuditAction.STEP_UP_FAILED,
                    Outcome.FAILURE,
                    context,
                    reason=Reason.SESSION_CAP,
                )
            )
            raise StepUpFailedError()
        verification = await self._passwords.check(user.password_hash, password)
        now = self._now()
        key = self.limiter.key(user.username)
        reason: Reason | None = None
        if user.must_change_password:
            reason = Reason.MUST_CHANGE_PASSWORD
        elif user.locked_until_ms is not None and now < user.locked_until_ms:
            reason = Reason.LOCKED
        elif not verification.valid:
            reason = Reason.WRONG_PASSWORD
        elif self.limiter.blocked(key):
            reason = Reason.RATE_LIMITED
        if reason is not None:
            details: dict[str, object] = {"reason": reason}
            self._step_up_failures.record_failure(session.id_hash)
            if reason is Reason.WRONG_PASSWORD:
                details["failures"] = self._count_failure(user, now)
                self.limiter.record_failure(key)
            self._record(
                self._session_event(AuditAction.STEP_UP_FAILED, Outcome.FAILURE, context, **details)
            )
            raise StepUpFailedError()
        if verification.upgraded_hash is not None:
            self.store.set_password_hash(user.id, verification.upgraded_hash)
        until = now + self.step_up_ms
        self.store.set_step_up(session.id_hash, until)
        self._record(
            self._session_event(
                AuditAction.STEP_UP_GRANTED, Outcome.SUCCESS, context, step_up_until_ms=until
            )
        )
        return until

    def step_up_until(self, context: SessionContext) -> float | None:
        """The end of this session's open step-up window, or None."""
        until = context.session.step_up_until_ms
        return until if until is not None and self._now() < until else None

    def check_step_up(self, context: SessionContext, action: str) -> None:
        """For ``requires``: an action the table flags needs this session's window open;
        otherwise audit it and raise ``StepUpRequiredError``."""
        if not PERMISSIONS[action].step_up or self.step_up_until(context) is not None:
            return
        self._record(
            self._session_event(
                AuditAction.STEP_UP_REQUIRED, Outcome.FAILURE, context, action=action
            )
        )
        raise StepUpRequiredError()

    def claim_step_up(self, context: SessionContext, action: str) -> float:
        """For ``requires`` and an action the table marks consume-on-use (docs/32): use up this
        session's window, or audit and raise ``StepUpRequiredError`` if none is open. Returns the
        window's end, to give back if the action fails before changing anything."""
        claimed = self.store.claim_step_up(context.session.id_hash, self._now())
        if claimed is None:
            self._record(
                self._session_event(
                    AuditAction.STEP_UP_REQUIRED, Outcome.FAILURE, context, action=action
                )
            )
            raise StepUpRequiredError()
        return claimed

    def restore_step_up(self, context: SessionContext, until_ms: float) -> None:
        """Give back a window claimed for an action that failed before changing anything."""
        self.store.restore_step_up(context.session.id_hash, until_ms)

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
            self.store.clear_step_up(user.id)  # a lockout closes every open step-up window
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
        self.store.set_step_up(context.session.id_hash, None)  # this session's window too
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


def step_up_settings(environ: Mapping[str, str]) -> tuple[float, int]:
    """(minutes a step-up lasts, failed step-ups a session may make in 5 minutes), from
    ``GATEWAY_STEP_UP_MINUTES`` (default 5, 1 to 60) and ``GATEWAY_STEP_UP_SESSION_FAILURES``
    (default 3)."""
    try:
        minutes = float(environ.get(STEP_UP_MINUTES_ENV, DEFAULT_STEP_UP_MINUTES))
        failures = int(environ.get(STEP_UP_SESSION_FAILURES_ENV, DEFAULT_STEP_UP_SESSION_FAILURES))
    except ValueError:
        raise ValueError(
            f"{STEP_UP_MINUTES_ENV} and {STEP_UP_SESSION_FAILURES_ENV} must be numbers"
        ) from None
    if not 1 <= minutes <= 60:
        raise ValueError(f"{STEP_UP_MINUTES_ENV} must be from 1 to 60")
    if failures < 1:
        raise ValueError(f"{STEP_UP_SESSION_FAILURES_ENV} must be at least 1")
    return minutes, failures


def user_create_attempts(environ: Mapping[str, str]) -> int:
    """``GATEWAY_USER_CREATE_ATTEMPTS``: user-creation attempts a session may make in an hour
    (default 10, 1 to 1000)."""
    raw = environ.get(USER_CREATE_ATTEMPTS_ENV, str(DEFAULT_USER_CREATE_ATTEMPTS))
    try:
        attempts = int(raw)
    except ValueError:
        raise ValueError(f"{USER_CREATE_ATTEMPTS_ENV} must be a whole number") from None
    if not 1 <= attempts <= 1000:
        raise ValueError(f"{USER_CREATE_ATTEMPTS_ENV} must be from 1 to 1000")
    return attempts


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
