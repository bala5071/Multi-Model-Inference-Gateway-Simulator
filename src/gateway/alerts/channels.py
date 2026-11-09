"""Alert channels: an organization's webhook destinations, their sealed secrets, and how they
change (docs/46, the A4 plan's sections 2, 3, 4.1 and 9).

**A channel belongs to an organization,** so one webhook can serve several applications. It has
a name (the rule-name charset), a type (``webhook`` only in A4), ``enabled`` and ``is_default``
(default channels receive the default rules' notifications and the summaries). At most
``MAX_CHANNELS`` an organization, counted under the global write lock.

**Its URL is a secret:** many receivers carry a token in its path or query. It is sealed in the
vault's channel scope (``ChannelBinding``), and only its origin (``url_origin``) is ever shown,
audited, logged or put in an event. ``check_webhook_url`` refuses an obviously bad URL when it is
stored; the connect-time check of every delivery (group 2) is the one relied on.

**Its signing key** is 32 bytes from the injected ``random_bytes``, sealed the same way and
returned once, in the answer that creates the channel or rotates the key. A rotation keeps the
previous key for ``PREVIOUS_KEY_MS`` (store clock), and both sign meanwhile.

``channel_secrets`` is read by the channel stores and nothing else (a test greps every store's
SQL).
"""

from __future__ import annotations

import contextlib
import ipaddress
import re
import sqlite3
import unicodedata
from collections.abc import AsyncIterator, Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import urlsplit

from gateway.audit import AuditAction, AuditEvent, AuditLog, Outcome, Reason, TargetType
from gateway.clock import EpochSource, EpochStoreClock, StoreClock
from gateway.database import write_transaction
from gateway.egress import refusal
from gateway.management import ManagementRefusal, Stores, resolve_organization
from gateway.permissions import Actor, Resource
from gateway.providers import ChangeMarker
from gateway.vault import ChannelBinding, SealedKey, Vault

if TYPE_CHECKING:
    from gateway.alerts.notices import Notices

CHANNEL_ID_PREFIX = "chn_"
CHANNEL_ID = re.compile(r"chn_[0-9a-f]{16}")
MAX_CHANNELS = 20
CHANNEL_TYPES = ("webhook",)
# The rule-name charset (gateway.alertrules.NAME; a test keeps the two equal).
NAME = re.compile(r"[A-Za-z0-9 ._-]{1,64}")
SIGNING_KEY_BYTES = 32
# The previous signing key signs beside the new one this long after a rotation (store clock).
PREVIOUS_KEY_MS = 24 * 60 * 60 * 1000.0
MAX_URL_LENGTH = 2048
DEFAULT_WEBHOOK_PORT = 443
# More ports the operator allows for webhooks, each 1024 to 65535 (the plan's decision 8).
WEBHOOK_PORTS_ENV = "GATEWAY_ALERT_WEBHOOK_PORTS"
URL_KIND = "webhook_url"
KEY_KIND = "webhook_signing_key"
PREVIOUS_KIND = "webhook_signing_key_previous"
# What a channel change names in its audit row (never a value).
CHANNEL_FIELDS = frozenset({"name", "url", "default", "enabled"})


class WebhookUrlError(ValueError):
    """A webhook URL refused when it is stored. The message says which rule, never the URL."""


class ChannelNameTakenError(Exception):
    """The organization has a channel of that name (its unique index)."""


class ChannelVersionConflictError(Exception):
    pass


class NothingChangedError(Exception):
    """A change that changes nothing: answered with the channel as it is, after the step-up
    window it claimed is given back (it is raised through ``requires``)."""

    def __init__(self, channel: Channel) -> None:
        super().__init__("nothing changed")
        self.channel = channel


def parse_webhook_ports(text: str | None) -> frozenset[int]:
    """The ports a webhook may use: 443, and those ``GATEWAY_ALERT_WEBHOOK_PORTS`` lists
    (comma-separated, each a whole number from 1024 to 65535). Anything else refuses the start,
    naming the variable."""
    ports = {DEFAULT_WEBHOOK_PORT}
    for entry in (text or "").split(","):
        entry = entry.strip()
        if not entry:
            continue
        if not entry.isdigit() or not 1024 <= int(entry) <= 65535:
            raise ValueError(
                f"{WEBHOOK_PORTS_ENV} lists ports from 1024 to 65535, comma-separated; "
                "443 is always allowed"
            )
        ports.add(int(entry))
    return frozenset(ports)


_NUMERIC_LABEL = re.compile(r"(0x[0-9a-f]*|[0-9]+)")
_DOTTED_QUAD = re.compile(
    r"(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)(\.(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)){3}"
)
_NOT_PUBLIC_NAMES = (".localhost", ".local", ".internal")


@dataclass(frozen=True, slots=True)
class CheckedUrl:
    """A webhook URL that passed the checks of the plan's section 4.1: the URL itself (sealed,
    never shown) and its origin (the only part ever shown)."""

    url: str
    origin: str
    port: int

    def __repr__(self) -> str:
        return f"CheckedUrl(origin={self.origin!r}, <url redacted>)"


def check_webhook_url(url: object, ports: Collection[int] = (DEFAULT_WEBHOOK_PORT,)) -> CheckedUrl:
    """A webhook URL when it is stored (the plan's section 4.1), or ``WebhookUrlError`` naming
    the rule broken. Refused: a scheme other than https; user info or a fragment; over 2,048
    characters; any control character, whitespace or backslash anywhere; a host that is not
    IDNA, a literal address that is not public, ``localhost`` or a name ending ``.localhost``,
    ``.local`` or ``.internal``; a host in a numeric form other than the canonical dotted quad
    (``2130706433``, ``0x7f000001``, ``0177.0.0.1``, ``127.1``, ``0x7f.1``: resolvers read them
    as addresses); a port not in ``ports``. A query string is allowed (receivers carry tokens
    in it): it is part of the sealed secret."""
    if not isinstance(url, str) or not url:
        raise WebhookUrlError("a webhook URL is a non-empty string")
    if len(url) > MAX_URL_LENGTH:
        raise WebhookUrlError(f"a webhook URL is at most {MAX_URL_LENGTH} characters")
    for character in url:
        if (
            character.isspace()
            or character == "\\"
            or unicodedata.category(character) in ("Cc", "Cf", "Zl", "Zp")
        ):
            raise WebhookUrlError("a webhook URL has no control character, whitespace or backslash")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise WebhookUrlError("a webhook URL's port is a number from 1 to 65535") from None
    if parts.scheme != "https":
        raise WebhookUrlError("a webhook URL uses https")
    if "@" in parts.netloc:
        raise WebhookUrlError("a webhook URL has no user info")
    if "#" in url:
        raise WebhookUrlError("a webhook URL has no fragment")
    host = parts.hostname or ""
    if not host:
        raise WebhookUrlError("a webhook URL needs a host")
    if "%" in host:
        raise WebhookUrlError("a webhook URL's host has no zone id")
    effective = port if port is not None else DEFAULT_WEBHOOK_PORT
    if effective not in ports:
        raise WebhookUrlError("a webhook URL's port is not one allowed (443 by default)")
    bare = host.rstrip(".")
    literal: ipaddress.IPv4Address | ipaddress.IPv6Address | None
    try:
        literal = ipaddress.ip_address(bare)
    except ValueError:
        literal = None
    if literal is not None:
        if isinstance(literal, ipaddress.IPv4Address) and not _DOTTED_QUAD.fullmatch(bare):
            raise WebhookUrlError("a webhook URL's address is a plain dotted quad")
        if refusal(literal) is not None:
            raise WebhookUrlError("a webhook URL's host must be a public address")
        shown = f"[{literal.compressed}]" if literal.version == 6 else literal.compressed
    else:
        labels = bare.lower().split(".")
        if _NUMERIC_LABEL.fullmatch(labels[-1]):
            # A last label that is a number makes the whole host an address to a resolver
            # (2130706433, 0x7f000001, 0177.0.0.1, 127.1, 0x7f.1): only the canonical dotted
            # quad, above, is accepted.
            raise WebhookUrlError("a webhook URL's address is a plain dotted quad")
        try:
            shown = bare.encode("idna").decode("ascii").lower()
        except UnicodeError:
            raise WebhookUrlError("a webhook URL's host is not a valid name") from None
        if shown == "localhost" or shown.endswith(_NOT_PUBLIC_NAMES):
            raise WebhookUrlError("a webhook URL's host must be a public name")
    origin = f"https://{shown}" + (f":{port}" if port is not None and port != 443 else "")
    return CheckedUrl(url, origin, effective)


# --- channels and their secrets ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Channel:
    id: str
    organization_id: str
    name: str
    type: str
    enabled: bool
    is_default: bool
    url_origin: str
    version: int
    created_at_ms: float
    updated_at_ms: float
    last_delivered_at_ms: float | None = None
    last_failed_at_ms: float | None = None
    consecutive_failures: int = 0


@dataclass(frozen=True, slots=True)
class StoredSecret:
    """A channel secret as stored: sealed, never the value."""

    channel_id: str
    kind: str
    sealed: SealedKey
    expires_at_ms: float | None = None


COLUMNS = (
    "id, organization_id, name, type, enabled, is_default, url_origin, version, created_at_ms,"
    " updated_at_ms, last_delivered_at_ms, last_failed_at_ms, consecutive_failures"
)


def channel_row(c: Channel) -> tuple[Any, ...]:
    return (
        c.id, c.organization_id, c.name, c.type, int(c.enabled), int(c.is_default),
        c.url_origin, c.version, c.created_at_ms, c.updated_at_ms, c.last_delivered_at_ms,
        c.last_failed_at_ms, c.consecutive_failures,
    )  # fmt: skip


def channel_from_row(row: Sequence[Any]) -> Channel:
    return Channel(
        row[0], row[1], row[2], row[3], bool(row[4]), bool(row[5]), row[6], int(row[7]),
        float(row[8]), float(row[9]), row[10], row[11], int(row[12]),
    )  # fmt: skip


class ChannelStore(Protocol):
    def write_transaction(self) -> contextlib.AbstractAsyncContextManager[None]: ...

    async def get_channel(self, channel_id: str) -> Channel | None: ...

    async def list_channels(
        self, organization_id: str, limit: int, after: str | None = None
    ) -> list[Channel]:
        """A page of the organization's channels, by id, after ``after``."""
        ...

    async def organization_channels(self, organization_id: str) -> list[Channel]:
        """Every channel of the organization (at most ``MAX_CHANNELS``), by id."""
        ...

    async def count_channels(self, organization_id: str) -> int: ...

    async def insert_channel(self, channel: Channel) -> None:
        """``ChannelNameTakenError`` if the organization has one of that name."""
        ...

    async def put_channel(self, channel: Channel, expected_version: int) -> None:
        """Write over the channel of its id at ``expected_version``, or
        ``ChannelVersionConflictError``; ``ChannelNameTakenError`` for a name taken."""
        ...

    async def delete_channel(self, channel_id: str) -> bool:
        """The channel and every secret of it; whether it was there."""
        ...

    async def put_secret(self, secret: StoredSecret) -> None: ...

    async def secrets(self, channel_id: str) -> dict[str, StoredSecret]:
        """The channel's secrets, by kind."""
        ...

    async def delete_secret(self, channel_id: str, kind: str) -> None: ...

    async def all_secrets(self) -> list[StoredSecret]:
        """Every channel secret (``rotate-vault-key``)."""
        ...


class InMemoryChannelStore:
    def __init__(self) -> None:
        self._channels: dict[str, Channel] = {}
        self._secrets: dict[tuple[str, str], StoredSecret] = {}

    @contextlib.asynccontextmanager
    async def write_transaction(self) -> AsyncIterator[None]:
        yield

    async def get_channel(self, channel_id: str) -> Channel | None:
        return self._channels.get(channel_id)

    async def list_channels(
        self, organization_id: str, limit: int, after: str | None = None
    ) -> list[Channel]:
        found = await self.organization_channels(organization_id)
        return [c for c in found if after is None or c.id > after][:limit]

    async def organization_channels(self, organization_id: str) -> list[Channel]:
        return sorted(
            (c for c in self._channels.values() if c.organization_id == organization_id),
            key=lambda c: c.id,
        )

    async def count_channels(self, organization_id: str) -> int:
        return len(await self.organization_channels(organization_id))

    def _name_taken(self, channel: Channel) -> bool:
        return any(
            c.organization_id == channel.organization_id and c.name == channel.name
            for c in self._channels.values()
            if c.id != channel.id
        )

    async def insert_channel(self, channel: Channel) -> None:
        if channel.id in self._channels:
            raise ChannelVersionConflictError()
        if self._name_taken(channel):
            raise ChannelNameTakenError()
        self._channels[channel.id] = channel

    async def put_channel(self, channel: Channel, expected_version: int) -> None:
        found = self._channels.get(channel.id)
        if found is None or found.version != expected_version:
            raise ChannelVersionConflictError()
        if self._name_taken(channel):
            raise ChannelNameTakenError()
        self._channels[channel.id] = channel

    async def delete_channel(self, channel_id: str) -> bool:
        for key in [k for k in self._secrets if k[0] == channel_id]:
            del self._secrets[key]
        return self._channels.pop(channel_id, None) is not None

    async def put_secret(self, secret: StoredSecret) -> None:
        self._secrets[(secret.channel_id, secret.kind)] = secret

    async def secrets(self, channel_id: str) -> dict[str, StoredSecret]:
        return {k: s for (c, k), s in self._secrets.items() if c == channel_id}

    async def delete_secret(self, channel_id: str, kind: str) -> None:
        self._secrets.pop((channel_id, kind), None)

    async def all_secrets(self) -> list[StoredSecret]:
        return [self._secrets[k] for k in sorted(self._secrets)]


_SECRET_COLUMNS = "channel_id, kind, key_id, nonce, ciphertext, expires_at_ms"


def secret_from_row(row: Sequence[Any]) -> StoredSecret:
    return StoredSecret(
        row[0], row[1], SealedKey(row[2], bytes(row[3]), bytes(row[4]), None), row[5]
    )


class SqliteChannelStore:
    """Over migration 17's ``alert_channels`` and ``channel_secrets``. Coroutines that never
    suspend (docs/34)."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._db = connection

    def write_transaction(self) -> contextlib.AbstractAsyncContextManager[None]:
        return write_transaction(self._db)

    def _channels(self, where: str, args: Sequence[Any]) -> list[Channel]:
        rows = self._db.execute(
            f"SELECT {COLUMNS} FROM alert_channels WHERE {where}", tuple(args)
        ).fetchall()
        return [channel_from_row(r) for r in rows]

    async def get_channel(self, channel_id: str) -> Channel | None:
        found = self._channels("id = ?", (channel_id,))
        return found[0] if found else None

    async def list_channels(
        self, organization_id: str, limit: int, after: str | None = None
    ) -> list[Channel]:
        return self._channels(
            "organization_id = ? AND id > ? ORDER BY id LIMIT ?",
            (organization_id, after or "", limit),
        )

    async def organization_channels(self, organization_id: str) -> list[Channel]:
        return self._channels("organization_id = ? ORDER BY id", (organization_id,))

    async def count_channels(self, organization_id: str) -> int:
        row = self._db.execute(
            "SELECT count(*) FROM alert_channels WHERE organization_id = ?", (organization_id,)
        ).fetchone()
        return int(row[0])

    async def insert_channel(self, channel: Channel) -> None:
        # An id there already is a conflict whatever the name, as on Postgres (its key first).
        if await self.get_channel(channel.id) is not None:
            raise ChannelVersionConflictError()
        try:
            self._db.execute(
                f"INSERT INTO alert_channels ({COLUMNS}) VALUES ({', '.join('?' * 13)})",
                channel_row(channel),
            )
        except sqlite3.IntegrityError as exc:
            if "alert_channels.organization_id" in str(exc):
                raise ChannelNameTakenError() from None
            raise ChannelVersionConflictError() from None

    async def put_channel(self, channel: Channel, expected_version: int) -> None:
        row = channel_row(channel)
        try:
            done = self._db.execute(
                "UPDATE alert_channels SET name = ?, type = ?, enabled = ?, is_default = ?,"
                " url_origin = ?, version = ?, updated_at_ms = ?, last_delivered_at_ms = ?,"
                " last_failed_at_ms = ?, consecutive_failures = ?"
                " WHERE id = ? AND organization_id = ? AND version = ?",
                (*row[2:8], *row[9:13], channel.id, channel.organization_id, expected_version),
            )
        except sqlite3.IntegrityError:
            raise ChannelNameTakenError() from None
        if done.rowcount != 1:
            raise ChannelVersionConflictError()

    async def delete_channel(self, channel_id: str) -> bool:
        self._db.execute("DELETE FROM channel_secrets WHERE channel_id = ?", (channel_id,))
        done = self._db.execute("DELETE FROM alert_channels WHERE id = ?", (channel_id,))
        return done.rowcount == 1

    async def put_secret(self, secret: StoredSecret) -> None:
        s = secret.sealed
        self._db.execute(
            f"INSERT OR REPLACE INTO channel_secrets ({_SECRET_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?)",
            (secret.channel_id, secret.kind, s.key_id, s.nonce, s.ciphertext, secret.expires_at_ms),
        )

    async def secrets(self, channel_id: str) -> dict[str, StoredSecret]:
        rows = self._db.execute(
            f"SELECT {_SECRET_COLUMNS} FROM channel_secrets WHERE channel_id = ?", (channel_id,)
        ).fetchall()
        return {r[1]: secret_from_row(r) for r in rows}

    async def delete_secret(self, channel_id: str, kind: str) -> None:
        self._db.execute(
            "DELETE FROM channel_secrets WHERE channel_id = ? AND kind = ?", (channel_id, kind)
        )

    async def all_secrets(self) -> list[StoredSecret]:
        rows = self._db.execute(
            f"SELECT {_SECRET_COLUMNS} FROM channel_secrets ORDER BY channel_id, kind"
        ).fetchall()
        return [secret_from_row(r) for r in rows]


async def resolve_channel(
    stores: Stores, channels: ChannelStore, organization_id: str, channel_id: str
) -> Resource | None:
    """The organization, as the resource a channel action acts on, if the channel exists **and
    belongs to it**; anything else (an id not in a channel id's form included) is missing,
    answered as forbidden."""
    resource = await resolve_organization(stores, organization_id)
    if resource is None or not CHANNEL_ID.fullmatch(channel_id):
        return None
    found = await channels.get_channel(channel_id)
    if found is None or found.organization_id != organization_id:
        return None
    return resource


def binding_of(channel: Channel, kind: str) -> ChannelBinding:
    return ChannelBinding(channel.organization_id, channel.id, kind, channel.url_origin)


def _check_name(name: object) -> str:
    if not isinstance(name, str) or not NAME.fullmatch(name) or name != name.strip():
        raise ValueError("1 to 64 of A-Z a-z 0-9 space . _ -, no space at an end")
    return name


# --- the service ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CreatedChannel:
    channel: Channel
    # Shown once, in the answer that creates the channel or rotates its key; hex.
    signing_key: str

    def __repr__(self) -> str:
        return f"CreatedChannel(channel={self.channel!r}, <signing key redacted>)"


class ChannelService:
    """Channels below HTTP: each change checked, under the write lock, audited by channel id and
    the names of the fields changed (never a URL, an origin or a key)."""

    def __init__(
        self,
        store: ChannelStore,
        vault: Vault,
        audit: AuditLog,
        epoch: EpochSource,
        random_bytes: Callable[[int], bytes],
        *,
        notices: Notices | None = None,
        ports: Collection[int] = (DEFAULT_WEBHOOK_PORT,),
        store_clock: StoreClock | None = None,
    ) -> None:
        """``store_clock`` times the previous signing key's 24 hours (the database's clock
        where it is shared, docs/37); ``epoch`` without it."""
        self.store = store
        self.vault = vault
        self._audit = audit
        self._epoch = epoch
        self._random_bytes = random_bytes
        # What a channel change sends (channel_removed, and channel_gone for a deleted one's
        # queued deliveries), in its transaction; rule changes send through it too.
        self.notices = notices
        self.ports = frozenset(ports)
        self.store_clock: StoreClock = (
            store_clock if store_clock is not None else EpochStoreClock(epoch)
        )

    def __repr__(self) -> str:
        return "ChannelService(<vault redacted>)"

    async def _record(
        self,
        action: AuditAction,
        channel: Channel,
        actor: Actor,
        changed: Sequence[str] = (),
    ) -> None:
        details: dict[str, object] = {
            "organization_id": channel.organization_id,
            "channel_type": channel.type,
        }
        if changed:
            details["channel_fields"] = sorted(changed)
        await self._audit.write(
            AuditEvent(
                action, Outcome.SUCCESS, TargetType.ALERT_CHANNEL, channel.id, actor.user_id,
                details,
            ),
            self._epoch.epoch_ms(),
        )  # fmt: skip

    def _new_key(self) -> str:
        key = self._random_bytes(SIGNING_KEY_BYTES)
        if len(key) != SIGNING_KEY_BYTES:
            raise RuntimeError("a signing key is 32 bytes")
        return key.hex()

    async def create(
        self,
        organization_id: str,
        *,
        name: object,
        url: object,
        is_default: object,
        channel_type: object,
        actor: Actor,
        marker: ChangeMarker,
    ) -> CreatedChannel:
        if channel_type not in CHANNEL_TYPES:
            raise ChannelFieldError("type", "must be webhook")
        try:
            checked_name = _check_name(name)
        except ValueError as exc:
            raise ChannelFieldError("name", str(exc)) from None
        if not isinstance(is_default, bool):
            raise ChannelFieldError("default", "must be true or false")
        try:
            checked = check_webhook_url(url, self.ports)
        except WebhookUrlError as exc:
            raise ChannelFieldError("url", str(exc)) from None
        async with self.store.write_transaction():
            if await self.store.count_channels(organization_id) >= MAX_CHANNELS:
                raise ManagementRefusal(
                    Reason.CHANNEL_LIMIT, f"an organization has at most {MAX_CHANNELS} channels"
                )
            now = self._epoch.epoch_ms()
            channel = Channel(
                CHANNEL_ID_PREFIX + self._random_bytes(8).hex(), organization_id, checked_name,
                "webhook", True, is_default, checked.origin, 1, now, now,
            )  # fmt: skip
            try:
                await self.store.insert_channel(channel)
            except ChannelNameTakenError:
                raise _name_taken() from None
            key = self._new_key()
            await self.store.put_secret(
                StoredSecret(
                    channel.id,
                    URL_KIND,
                    self.vault.seal(checked.url, binding_of(channel, URL_KIND)),
                )
            )
            await self.store.put_secret(
                StoredSecret(
                    channel.id, KEY_KIND, self.vault.seal(key, binding_of(channel, KEY_KIND))
                )
            )
            await self._record(AuditAction.ALERT_CHANNEL_CREATED, channel, actor)
            marker.changed()
        return CreatedChannel(channel, key)

    async def _current(self, channel_id: str) -> Channel:
        found = await self.store.get_channel(channel_id)
        if found is None:
            raise ChannelGoneError()
        return found

    async def update(
        self,
        channel_id: str,
        expected_version: int,
        changes: Mapping[str, object],
        *,
        actor: Actor,
        marker: ChangeMarker,
    ) -> Channel:
        """``name``, ``url``, ``default`` and ``enabled: true`` (re-enabling, with step-up: the
        route's action). ``enabled: false`` is refused: disabling is its own action, so a
        disable never needs the password and a re-enable always does (A4-16)."""
        for field in changes:
            if field not in ("name", "url", "default", "enabled"):
                raise ChannelFieldError(field, "is not a channel field")
        if "enabled" in changes and changes["enabled"] is not True:
            raise ChannelFieldError("enabled", "only true; disable a channel with /disable")
        name = changes.get("name")
        checked = None
        try:
            if "name" in changes:
                name = _check_name(name)
        except ValueError as exc:
            raise ChannelFieldError("name", str(exc)) from None
        if "default" in changes and not isinstance(changes["default"], bool):
            raise ChannelFieldError("default", "must be true or false")
        if "url" in changes:
            try:
                checked = check_webhook_url(changes["url"], self.ports)
            except WebhookUrlError as exc:
                raise ChannelFieldError("url", str(exc)) from None
        async with self.store.write_transaction():
            found = await self._current(channel_id)
            if found.version != expected_version:
                raise _version_conflict(found.version, expected_version)
            after = found
            changed: list[str] = []
            if "name" in changes and name != found.name:
                after = replace(after, name=str(name))
                changed.append("name")
            if "default" in changes and changes["default"] != found.is_default:
                after = replace(after, is_default=bool(changes["default"]))
                changed.append("default")
            if "enabled" in changes and not found.enabled:
                after = replace(after, enabled=True)
                changed.append("enabled")
            secrets = await self.store.secrets(found.id)
            if checked is not None:
                # A URL given is a change, even the same one: comparing would mean decrypting
                # the stored one, and its value is revealed only to send it (M36).
                after = replace(after, url_origin=checked.origin)
                changed.append("url")
            if not changed:
                raise NothingChangedError(found)
            after = replace(after, version=found.version + 1, updated_at_ms=self._epoch.epoch_ms())
            try:
                await self.store.put_channel(after, expected_version)
            except ChannelNameTakenError:
                raise _name_taken() from None
            except ChannelVersionConflictError:
                raise _version_conflict(None, expected_version) from None
            if checked is not None:
                await self.store.put_secret(
                    StoredSecret(
                        after.id,
                        URL_KIND,
                        self.vault.seal(checked.url, binding_of(after, URL_KIND)),
                    )
                )
                if after.url_origin != found.url_origin:
                    # The signing keys are bound to the origin: rebound, the same keys.
                    for kind in (KEY_KIND, PREVIOUS_KIND):
                        stored = secrets.get(kind)
                        if stored is not None:
                            await self.store.put_secret(
                                replace(
                                    stored,
                                    sealed=self.vault.rebind(
                                        stored.sealed, binding_of(found, kind),
                                        binding_of(after, kind),
                                    ),
                                )
                            )  # fmt: skip
            fields = [f for f in changed if f != "enabled"]
            if fields:
                await self._record(AuditAction.ALERT_CHANNEL_CHANGED, after, actor, fields)
            if "enabled" in changed:
                await self._record(AuditAction.ALERT_CHANNEL_ENABLED, after, actor)
            marker.changed()
        return after

    async def disable(self, channel_id: str, *, actor: Actor) -> Channel:
        """Disabled: no step-up (it narrows where data goes), audited, and the organization's
        other default channels told (``channel_removed``). Disabling a disabled channel changes
        nothing and tells nobody."""
        async with self.store.write_transaction():
            found = await self._current(channel_id)
            if not found.enabled:
                return found
            after = replace(
                found, enabled=False, version=found.version + 1,
                updated_at_ms=self._epoch.epoch_ms(),
            )  # fmt: skip
            await self.store.put_channel(after, found.version)
            await self._record(AuditAction.ALERT_CHANNEL_DISABLED, after, actor)
            if self.notices is not None:
                await self.notices.channel_removed(after)
        return after

    async def delete(self, channel_id: str, *, actor: Actor) -> None:
        """Deleted with its secrets; its queued deliveries fail as ``channel_gone``; the other
        default channels told if it was enabled (a disabled one was announced when disabled)."""
        async with self.store.write_transaction():
            found = await self._current(channel_id)
            await self.store.delete_channel(found.id)
            await self._record(AuditAction.ALERT_CHANNEL_DELETED, found, actor)
            if self.notices is not None:
                await self.notices.channel_gone(found.id)
                if found.enabled:
                    await self.notices.channel_removed(found)

    async def rotate_key(self, channel_id: str, *, actor: Actor, marker: ChangeMarker) -> str:
        """A new signing key, returned once; the one it replaces kept for 24 hours as the
        previous key (both sign meanwhile), replacing any older previous key."""
        async with self.store.write_transaction():
            found = await self._current(channel_id)
            secrets = await self.store.secrets(found.id)
            current = secrets.get(KEY_KIND)
            now = await self.store_clock.now_ms()
            if current is not None:
                await self.store.put_secret(
                    StoredSecret(
                        found.id,
                        PREVIOUS_KIND,
                        self.vault.rebind(
                            current.sealed,
                            binding_of(found, KEY_KIND),
                            binding_of(found, PREVIOUS_KIND),
                        ),
                        now + PREVIOUS_KEY_MS,
                    )
                )
            key = self._new_key()
            await self.store.put_secret(
                StoredSecret(found.id, KEY_KIND, self.vault.seal(key, binding_of(found, KEY_KIND)))
            )
            after = replace(found, updated_at_ms=self._epoch.epoch_ms())
            await self.store.put_channel(after, found.version)
            await self._record(AuditAction.ALERT_CHANNEL_KEY_ROTATED, after, actor)
            marker.changed()
        return key

    async def reseal_all(self) -> tuple[int, list[str]]:
        """``rotate-vault-key``: every channel secret not under the current master key resealed
        under it, in the caller's transaction. Returns how many, and the channels whose secrets
        no loaded key can read (left as they are: their deliveries fail as
        ``channel_secret_unreadable``)."""
        resealed, unreadable = 0, []
        for stored in await self.store.all_secrets():
            if stored.sealed.key_id == self.vault.current_key_id:
                continue
            channel = await self.store.get_channel(stored.channel_id)
            if channel is None or not self.vault.readable(stored.sealed):
                unreadable.append(stored.channel_id)
                continue
            await self.store.put_secret(
                replace(
                    stored,
                    sealed=self.vault.reseal(stored.sealed, binding_of(channel, stored.kind)),
                )
            )
            resealed += 1
        return resealed, sorted(set(unreadable))


class ChannelFieldError(ValueError):
    """A channel body that is not valid: names the field (answered 422)."""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(f"{field}: {message}")
        self.field = field


class ChannelGoneError(Exception):
    """Resolved a moment ago, deleted since: answered as missing."""


def _name_taken() -> ManagementRefusal:
    return ManagementRefusal(
        Reason.CHANNEL_NAME_TAKEN, "the organization already has a channel of that name"
    )


def _version_conflict(current: int | None, expected: int) -> ManagementRefusal:
    return ManagementRefusal(
        Reason.CHANNEL_VERSION_CONFLICT,
        f"the channel is at version {current}, not {expected}: read it again and make the "
        "change from there",
    )
