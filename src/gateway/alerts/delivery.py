"""The delivery worker and test send (docs/46, the A4 plan's sections 4.3 and 5).

**Every instance runs one worker.** It claims due deliveries (``OutboxStore.claim``: on Postgres
``FOR UPDATE SKIP LOCKED``, a 60 s claim), runs at most ``MAX_RUNNING`` at once, and records
each outcome with a compare-and-set on its claim, the channel's health in the same transaction.
A dead instance's claim expires and is taken again, so a delivery is made at least once; a
receiver deduplicates on ``X-Gateway-Delivery``.

**No store connection is held during a network call** (the pool rule, decided 2026-11-08). A
delivery makes three short transactions: the claim; the read of its channel and secrets
(decrypted after the connection is given back); and the outcome. The HTTP request runs between
them, and ``_guard`` raises if this task holds a transaction when it starts one
(``TransactionHeldError``). On Postgres the worker shares the evaluator's pool of 3.

**Retries.** Six tries in all: after the first five failures that may be retried, the next is
30 s, 2 min, 8 min, 30 min and 2 h later, each with up to 10% jitter from the seeded ``rng``, or
the receiver's ``Retry-After`` (at most an hour) when it gives one. Any other 4xx, an egress
refusal, a 3xx or a TLS failure is permanent. Every attempt is signed afresh.

**Test send** sends one ``test`` notification now, through the same attempt, counted per
channel and per organization an hour (a counter that cannot answer refuses); its code is
answered and audited, and it changes neither the outbox nor the channel's health.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
from collections.abc import Callable, Mapping
from dataclasses import dataclass

import httpx

from gateway.alerts.caps import AlertCaps, CeilingsStore
from gateway.alerts.channels import (
    KEY_KIND,
    PREVIOUS_KIND,
    URL_KIND,
    Channel,
    ChannelGoneError,
    ChannelStore,
    StoredSecret,
    binding_of,
)
from gateway.alerts.outbox import (
    Delivery,
    DeliveryState,
    OutboxStore,
    Outcome,
    TransactionHeldError,
)
from gateway.alerts.payload import AlertPayload
from gateway.alerts.webhook import (
    CHANNEL_DISABLED,
    CHANNEL_GONE,
    CHANNEL_SECRET_UNREADABLE,
    DELIVERED,
    Attempt,
    build_request,
    send,
)
from gateway.audit import AuditAction, AuditEvent, AuditLog, Reason, TargetType
from gateway.audit import Outcome as AuditOutcome
from gateway.clock import Clock, EpochSource, StoreClock
from gateway.egress import always_hosted, egress_scope
from gateway.management import ManagementRefusal
from gateway.permissions import Actor
from gateway.providers import ConnectionTestCounter
from gateway.redaction import Redactor
from gateway.vault import KeyUnreadableError, Vault, VaultIntegrityError

LOG = logging.getLogger("gateway.alerts")

CLAIM_MS = 60_000.0
MAX_RUNNING = 4
POLL_MS = 1_000.0
# After the 1st to 5th failed attempt that may be retried (the plan's section 4.2).
BACKOFF_MS = (30_000.0, 120_000.0, 480_000.0, 1_800_000.0, 7_200_000.0)
MAX_ATTEMPTS = len(BACKOFF_MS) + 1
JITTER = 0.10
CLAIM_ID_PREFIX = "clm_"
# Codes that say nothing about the channel's receiver: its health is left as it is.
NOT_HEALTH = frozenset({CHANNEL_GONE, CHANNEL_DISABLED})


@dataclass(frozen=True, slots=True)
class DeliverySetup:
    """What a server's channels deliver with (``build_channels``): a new webhook client for
    each attempt, the redactor the leased secrets go to, the caps, and where the instances'
    caps are compared."""

    client: Callable[[], httpx.AsyncClient]
    redactor: Redactor
    caps: AlertCaps
    ceilings: CeilingsStore

    def __repr__(self) -> str:
        return f"DeliverySetup(caps={self.caps!r})"


@dataclass(frozen=True, slots=True)
class AttemptReport:
    """What monitoring is told of each recorded attempt: the channel's id and type, and the
    code. Never a URL, an origin, a payload or anything the receiver answered."""

    channel_id: str
    channel_type: str
    code: str
    final: bool


def retry_delay_ms(attempts: int, attempt: Attempt, rng: random.Random) -> float:
    """The wait before the next try after failed attempt number ``attempts`` (1 to 5)."""
    if attempt.retry_after_s is not None:
        return attempt.retry_after_s * 1000.0
    return BACKOFF_MS[attempts - 1] * (1.0 + rng.uniform(0.0, JITTER))


def outcome_of(delivery: Delivery, attempt: Attempt, now_ms: float, rng: random.Random) -> Outcome:
    if attempt.delivered:
        return Outcome(DeliveryState.DELIVERED, DELIVERED)
    if attempt.retry and delivery.attempts < MAX_ATTEMPTS:
        return Outcome(
            DeliveryState.QUEUED,
            attempt.code,
            now_ms + retry_delay_ms(delivery.attempts, attempt, rng),
        )
    return Outcome(DeliveryState.FAILED, attempt.code)


class DeliveryWorker:
    def __init__(
        self,
        outbox: OutboxStore,
        channels: ChannelStore,
        vault: Vault,
        redactor: Redactor,
        *,
        store_clock: StoreClock,
        clock: Clock,
        epoch: EpochSource,
        client: Callable[[], httpx.AsyncClient],
        rng: random.Random,
        random_bytes: Callable[[int], bytes],
        on_attempt: Callable[[AttemptReport], None] | None = None,
        max_running: int = MAX_RUNNING,
    ) -> None:
        """``client`` makes a new ``webhook_client`` for each attempt (M45)."""
        self.outbox = outbox
        self.channels = channels
        self._vault = vault
        self._redactor = redactor
        self._store_clock = store_clock
        self._clock = clock
        self._epoch = epoch
        self._client = client
        self._rng = rng
        self._random_bytes = random_bytes
        self._on_attempt = on_attempt
        self._max_running = max_running
        self._running: set[asyncio.Task[None]] = set()

    def __repr__(self) -> str:
        return "DeliveryWorker(<vault redacted>)"

    @property
    def running(self) -> int:
        return len(self._running)

    def _guard(self) -> None:
        """The pool rule: no network call while this task holds a store transaction."""
        if self.outbox.holds_transaction():
            raise TransactionHeldError("a network call inside a store transaction")

    async def step(self) -> int:
        """Claim due deliveries for the free slots and start them: how many started."""
        free = self._max_running - len(self._running)
        if free <= 0:
            return 0
        claim_id = CLAIM_ID_PREFIX + self._random_bytes(8).hex()
        async with self.outbox.delivery_transaction():
            now = await self._store_clock.now_ms()
            claimed = await self.outbox.claim(claim_id, free, now, CLAIM_MS)
        for delivery in claimed:
            task = asyncio.create_task(self._deliver(delivery, claim_id))
            self._running.add(task)
            task.add_done_callback(self._running.discard)
        return len(claimed)

    async def drain(self) -> None:
        """Until every started delivery has finished (tests, and the shutdown)."""
        while self._running:
            await asyncio.gather(*list(self._running), return_exceptions=True)

    async def run(self) -> None:
        """Every ``POLL_MS`` until cancelled; the event loop given back between steps."""
        try:
            while True:
                try:
                    await self.step()
                except Exception as exc:  # a store that cannot answer: tried again later
                    _log_failure("claim", exc)
                await self._clock.sleep_ms(POLL_MS)
        finally:
            for task in list(self._running):
                task.cancel()

    async def read(self, channel_id: str) -> tuple[Channel | None, Mapping[str, StoredSecret]]:
        """The channel and its sealed secrets, in one short transaction."""
        async with self.outbox.delivery_transaction():
            channel = await self.channels.get_channel(channel_id)
            secrets = await self.channels.secrets(channel_id) if channel is not None else {}
            now = await self._store_clock.now_ms()
        # A previous key past its 24 hours signs no more.
        return channel, {
            kind: s
            for kind, s in secrets.items()
            if kind != PREVIOUS_KIND or (s.expires_at_ms is not None and s.expires_at_ms > now)
        }

    async def attempt(
        self,
        channel: Channel,
        secrets: Mapping[str, StoredSecret],
        body: str,
        delivery_id: str,
    ) -> Attempt:
        """One signed attempt of ``body``: the secrets decrypted now, in leases, after every
        store connection was given back, and the request sent under ``always_hosted``."""
        url, key = secrets.get(URL_KIND), secrets.get(KEY_KIND)
        if url is None or key is None:
            return Attempt(CHANNEL_SECRET_UNREADABLE, False)
        keys = [key] + ([secrets[PREVIOUS_KIND]] if PREVIOUS_KIND in secrets else [])
        with contextlib.ExitStack() as leases:
            try:
                opened_url = leases.enter_context(
                    self._vault.lease(url.sealed, binding_of(channel, URL_KIND), self._redactor)
                )
                opened_keys = [
                    leases.enter_context(
                        self._vault.lease(k.sealed, binding_of(channel, k.kind), self._redactor)
                    )
                    for k in keys
                ]
            except (KeyUnreadableError, VaultIntegrityError):
                return Attempt(CHANNEL_SECRET_UNREADABLE, False)
            request = build_request(
                opened_url, opened_keys, body, delivery_id, int(self._epoch.epoch_ms() // 1000)
            )
            self._guard()
            with egress_scope(always_hosted):
                return await send(self._client(), request, self._clock)

    async def _deliver(self, delivery: Delivery, claim_id: str) -> None:
        try:
            channel, secrets = await self.read(delivery.channel_id)
            if channel is None:
                attempt = Attempt(CHANNEL_GONE, False)
            elif not channel.enabled:
                attempt = Attempt(CHANNEL_DISABLED, False)
            else:
                attempt = await self.attempt(
                    channel, secrets, delivery.payload, delivery.notification_id
                )
            await self._record(delivery, claim_id, channel, attempt)
        except Exception as exc:  # the store failed: the claim expires and it is tried again
            _log_failure("delivery", exc)

    async def _record(
        self, delivery: Delivery, claim_id: str, channel: Channel | None, attempt: Attempt
    ) -> None:
        async with self.outbox.delivery_transaction():
            now = await self._store_clock.now_ms()
            outcome = outcome_of(delivery, attempt, now, self._rng)
            recorded = await self.outbox.record(delivery.id, claim_id, outcome, now)
            if recorded and channel is not None and attempt.code not in NOT_HEALTH:
                await self.channels.record_health(channel.id, attempt.delivered, now)
        if recorded and self._on_attempt is not None:
            self._on_attempt(
                AttemptReport(
                    delivery.channel_id,
                    channel.type if channel is not None else "webhook",
                    attempt.code,
                    outcome.state is not DeliveryState.QUEUED,
                )
            )


def _log_failure(what: str, exc: BaseException) -> None:
    # The exception's type alone: its message could carry a URL.
    LOG.warning("alert delivery: the %s failed (%s)", what, type(exc).__name__)


class TestSends:
    """``POST .../alert-channels/{channel}/test``: one ``test`` notification, now."""

    __test__ = False  # not a pytest class

    def __init__(
        self,
        worker: DeliveryWorker,
        counter: ConnectionTestCounter,
        audit: AuditLog,
        epoch: EpochSource,
        random_bytes: Callable[[int], bytes],
    ) -> None:
        self._worker = worker
        self._counter = counter
        self._audit = audit
        self._epoch = epoch
        self._random_bytes = random_bytes

    async def send(self, organization_id: str, channel_id: str, actor: Actor) -> str:
        """The test's code. A disabled channel is refused (409 ``channel_disabled``); a channel
        or an organization over its hour's test sends is refused (429 ``test_send_limited``)
        before anything is sent."""
        channel, secrets = await self._worker.read(channel_id)
        if channel is None or channel.organization_id != organization_id:
            raise ChannelGoneError()
        if not channel.enabled:
            raise ManagementRefusal(
                Reason.CHANNEL_DISABLED, "a disabled channel is sent nothing; enable it first"
            )
        await self._counter.count(channel.id, organization_id)
        payload = AlertPayload(
            "ntf_" + self._random_bytes(8).hex(),
            "test",
            self._epoch.epoch_ms(),
            organization_id,
            console_path=f"/v1/admin/orgs/{organization_id}/alert-channels/{channel.id}",
        )
        attempt = await self._worker.attempt(channel, secrets, payload.body(), payload.delivery_id)
        await self._audit.write(
            AuditEvent(
                AuditAction.ALERT_CHANNEL_TESTED, AuditOutcome.SUCCESS, TargetType.ALERT_CHANNEL,
                channel.id, actor.user_id,
                {
                    "organization_id": organization_id, "channel_type": channel.type,
                    "delivery_code": attempt.code,
                },
            ),
            self._epoch.epoch_ms(),
        )  # fmt: skip
        return attempt.code
