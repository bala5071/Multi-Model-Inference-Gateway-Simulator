"""What a notification is, where it goes, and how it is queued in the outbox: in the transaction
of the change it belongs to (docs/46).

- **An incident's notifications** (``prepare``): the evaluator's change and its recorded
  notifications' deliveries, queued by ``apply``'s ``then`` in the change's transaction (M22).
  The first one that is delivered fixes the incident's channels (``notify_channels``, ids only);
  every later one goes to those that still exist and are enabled, never to the rule's channels
  as they are now (A4-7). A held notification (quiet hours) gets no delivery: its summary does.
- **``rule_weakened``** (M29): a write that makes an enabled rule fire less goes to the rule's
  channels **as they were before the write**, naming the rule and the fields, never the user.
- **``channel_removed``**: disabling or deleting an enabled channel goes to the organization's
  other default channels.
- **``suppressed_summary``** (``summaries``): once an hour has ended, one an organization for
  the notifications the caps suppressed in it (a unique index, M26), to the default channels,
  not counted.
- **``channel_delivery_failing``**, the built-in (its incidents are the evaluator's): to every
  default channel but the failing one (CDF-3), under the organization's own rule id
  (``builtin_rule_ids``: an HMAC of the organization id under its own derived key).

**Where a notification goes** (``destinations``): ``["organization defaults"]`` is the
organization's enabled default channels; channel ids are those of them that exist **in the
organization** now and are enabled, checked again here at queue time (M15's second check; the
first is the rule write's). ``[]`` is nowhere.

**The caps** (``queue``): a notification, whatever its number of channels, counts once against
its rule's hour and its organization's hour, by one conditional upsert each in the outbox's
transaction (M25), the hour from the store clock. Over either, its deliveries are queued as
``suppressed`` with ``rule_cap`` or ``organization_cap``. Counted in that order: one its
organization refuses has counted against its rule.

**An email channel** (docs/48) gets one delivery per **verified** recipient, read here at queue
time; each counts against the caps as one (the webhooks of a notification still count once
together, as above), so a notification to an email channel of 10 verified recipients takes 10.
One with no verified recipient gets one delivery with no recipient, not counted, which fails
``no_verified_recipient`` and so counts toward the channel's health.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import replace
from typing import TYPE_CHECKING

from gateway.alerts.caps import AlertCaps
from gateway.alerts.channels import Channel, ChannelStore
from gateway.alerts.outbox import DELIVERY_ID_PREFIX, Delivery, DeliveryState, OutboxStore
from gateway.alerts.payload import (
    AlertPayload,
    PayloadApplication,
    PayloadDetails,
    PayloadIncident,
    PayloadRule,
    PayloadTooLargeError,
    iso,
)
from gateway.alerts.recipients import VERIFIED
from gateway.alerts.webhook import ORGANIZATION_CAP, RULE_CAP
from gateway.applications import Application, ApplicationStore
from gateway.clock import EpochSource, EpochStoreClock, StoreClock
from gateway.incidents import Change, Incident, Notification, NotificationKind, NotificationState
from gateway.signals import SIGNALS

if TYPE_CHECKING:
    from gateway.alertrules import AlertRule
    from gateway.alerts.recipients import RecipientStore

ORGANIZATION_DEFAULTS = "organization defaults"
NOTIFICATION_ID_PREFIX = "ntf_"
PAYLOAD_TOO_LARGE = "payload_too_large"
CHANNEL_GONE = "channel_gone"
HOUR_MS = 60 * 60 * 1000.0
BUILTIN_RULE_PREFIX = "rul_cdf_"
# What the built-in's firing notification is, as a payload's kind.
BUILTIN_KIND = "channel_delivery_failing"


def builtin_rule_ids(key: bytes) -> Callable[[str], str]:
    """The built-in's rule id of an organization: ``rul_cdf_`` and the first 16 hex characters
    of an HMAC of its id under ``key`` (``KeyPurpose.BUILTIN_RULE_ID``): fixed per
    organization, different for each, revealing nothing (the plan's decision 12)."""

    def rule_id(organization_id: str) -> str:
        digest = hmac.new(key, organization_id.encode(), hashlib.sha256).hexdigest()
        return BUILTIN_RULE_PREFIX + digest[:16]

    return rule_id


def rule_payload(rule: AlertRule) -> PayloadRule:
    s = rule.spec
    return PayloadRule(
        rule.id, s.name, s.signal, s.comparison, s.threshold, SIGNALS[s.signal].unit,
        None if s.window is None else s.window * 60, s.for_minutes * 60, s.severity,
    )  # fmt: skip


def pick(
    channels: Sequence[Channel], wanted: Sequence[str], *, exclude: str | None
) -> list[Channel]:
    """The channels a notification goes to, from the organization's ``channels``: its enabled
    default channels for ``["organization defaults"]``, the enabled ones named otherwise; never
    ``exclude``. Only the organization's own channels are given, so an id of another
    organization's channel matches nothing."""
    if list(wanted) == [ORGANIZATION_DEFAULTS]:
        found = [c for c in channels if c.is_default and c.enabled]
    else:
        named = set(wanted)
        found = [c for c in channels if c.id in named and c.enabled]
    return [c for c in found if c.id != exclude]


def _held(reason: str | None) -> int | None:
    # A quiet-hours summary's reason is "<n> held" (gateway.evaluator.quiet_hours_over).
    if reason is None or not reason.endswith(" held"):
        return None
    count = reason.split(" ", 1)[0]
    return int(count) if count.isdigit() else None


class Notices:
    def __init__(
        self,
        channels: ChannelStore,
        outbox: OutboxStore,
        applications: ApplicationStore,
        epoch: EpochSource,
        random_bytes: Callable[[int], bytes],
        *,
        caps: AlertCaps | None = None,
        store_clock: StoreClock | None = None,
        builtin_rule_id: Callable[[str], str] | None = None,
        recipients: RecipientStore | None = None,
    ) -> None:
        """``caps``: the caps counted when a notification is queued (None: none, as in the
        tests of group 1). ``store_clock``: the time notifications are queued at and their
        hour counted in (the database's where it is shared); ``epoch`` without it.
        ``builtin_rule_id``: the built-in's rule id of an organization (None: no built-in)."""
        self.channels = channels
        self.outbox = outbox
        self._applications = applications
        self._epoch = epoch
        self._random_bytes = random_bytes
        self.caps = caps
        self.store_clock: StoreClock = (
            store_clock if store_clock is not None else EpochStoreClock(epoch)
        )
        self.builtin_rule_id = builtin_rule_id
        # An email channel's recipients, its deliveries one per verified recipient (docs/48).
        self.recipients = recipients

    def new_id(self, prefix: str) -> str:
        return prefix + self._random_bytes(8).hex()

    async def organization_of(self, application_id: str) -> str | None:
        application = await self._applications.get_application(application_id)
        return application.organization_id if application is not None else None

    async def destinations(
        self, organization_id: str, wanted: Sequence[str], *, exclude: str | None = None
    ) -> list[Channel]:
        return pick(
            await self.channels.organization_channels(organization_id), wanted, exclude=exclude
        )

    async def _counted(
        self, organization_id: str, rule_id: str | None, now_ms: float
    ) -> str | None:
        """Count one notification against its caps: None if under both, else the code."""
        if self.caps is None:
            return None
        hour = int(now_ms // HOUR_MS)
        if rule_id is not None and not await self.outbox.count_notification(
            "rule", rule_id, hour, self.caps.per_rule
        ):
            return RULE_CAP
        if not await self.outbox.count_notification(
            "organization", organization_id, hour, self.caps.per_organization
        ):
            return ORGANIZATION_CAP
        return None

    async def queue(
        self,
        payload: AlertPayload,
        channels: Sequence[Channel],
        *,
        rule_id: str | None = None,
        counted: bool = True,
        now_ms: float | None = None,
    ) -> int:
        """One delivery of ``payload`` per channel, in the caller's transaction: counted once
        against the caps (unless ``counted`` is false: a summary), suppressed over them. A body
        over the size bound is queued as failed (``payload_too_large``), never cut short.
        ``now_ms``: the store's time, if the caller has it."""
        if not channels:
            return 0
        now = now_ms if now_ms is not None else await self.store_clock.now_ms()
        state, code = DeliveryState.QUEUED, None
        attempt_at: float | None = now
        try:
            body = payload.body()
        except PayloadTooLargeError:
            body, state, code, attempt_at = "", DeliveryState.FAILED, PAYLOAD_TOO_LARGE, None
        # (channel, recipient, counted): the webhooks of a notification count once together;
        # each verified recipient of an email channel counts one; an email channel with none
        # gets a row of no recipient, not counted.
        rows: list[tuple[Channel, str | None, bool]] = []
        webhooks = [c for c in channels if c.type != "email"]
        rows += [(c, None, i == 0) for i, c in enumerate(webhooks)]
        for c in channels:
            if c.type != "email":
                continue
            verified = (
                [
                    r.id
                    for r in await self.recipients.channel_recipients(c.id)
                    if r.state == VERIFIED
                ]
                if self.recipients is not None
                else []
            )
            rows += [(c, r, True) for r in verified] or [(c, None, False)]
        deliveries = []
        capped: str | None = None
        for c, recipient, unit in rows:
            row_state, row_code, row_at = state, code, attempt_at
            if state is DeliveryState.QUEUED and counted:
                if unit:
                    capped = await self._counted(payload.organization_id, rule_id, now)
                if capped is not None and (unit or c.type != "email"):
                    row_state, row_code, row_at = DeliveryState.SUPPRESSED, capped, None
            deliveries.append(
                Delivery(
                    self.new_id(DELIVERY_ID_PREFIX), payload.delivery_id, c.id,
                    payload.organization_id, row_state, 0, row_at, None, None, row_code, body,
                    now, now, recipient,
                )
            )  # fmt: skip
        return await self.outbox.queue(deliveries)

    # --- the evaluator's notifications ----------------------------------------------------------

    def incident_payload(
        self,
        notification: Notification,
        incident: Incident,
        rule: AlertRule | None,
        application: Application | None,
    ) -> AlertPayload:
        org = incident.organization_id
        builtin = incident.application_id == ""
        kind = notification.kind.value
        if builtin and notification.kind is NotificationKind.FIRING:
            kind = BUILTIN_KIND
        console = (
            f"/v1/admin/orgs/{org}/alert-channels/{incident.series_key}"
            if builtin
            else f"/v1/admin/orgs/{org}/applications/{incident.application_id}"
            f"/incidents/{incident.id}"
        )
        details = None
        if builtin:
            details = PayloadDetails(channel=incident.series_key)
        elif notification.kind is NotificationKind.QUIET_HOURS_SUMMARY:
            details = PayloadDetails(held=_held(notification.reason))
        return AlertPayload(
            notification.id,
            kind,
            notification.at_ms,
            org,
            None if application is None else PayloadApplication(application.id, application.name),
            None if rule is None or builtin else rule_payload(rule),
            PayloadIncident(
                incident.id, incident.state.value, incident.series_key or None, incident.value,
                iso(incident.fired_at_ms), iso(incident.resolved_at_ms),
                incident.resolve_reason.value if incident.resolve_reason is not None else None,
            ),
            console,
            details,
        )  # fmt: skip

    async def prepare(
        self, change: Change, rule: AlertRule | None, now_ms: float
    ) -> tuple[Change, Callable[[], Awaitable[None]] | None]:
        """``change`` with its incident's first channels fixed if this is its first delivered
        notification, and what queues its recorded notifications' deliveries (``apply``'s
        ``then``, in the change's transaction), or None if it has none. The reads (the
        application, the channels) are made here, before the transaction."""
        deliverable = [n for n in change.notifications if n.state is NotificationState.RECORDED]
        if not deliverable:
            return change, None
        incident = change.incident
        organization_id = incident.organization_id
        builtin = incident.application_id == ""
        # The built-in never sends to the channel that is failing (CDF-3).
        exclude = incident.series_key if builtin else None
        if incident.notify_channels is None:
            wanted: Sequence[str] = (
                [ORGANIZATION_DEFAULTS] if builtin else (rule.spec.channels if rule else [])
            )
            channels = await self.destinations(organization_id, wanted, exclude=exclude)
            incident = replace(incident, notify_channels=tuple(c.id for c in channels))
            change = replace(change, incident=incident)
        else:
            # Its first channels, those still in the organization and enabled (A4-7).
            channels = await self.destinations(
                organization_id, incident.notify_channels, exclude=exclude
            )
        application = (
            None if builtin else await self._applications.get_application(incident.application_id)
        )
        payloads = [self.incident_payload(n, incident, rule, application) for n in deliverable]
        rule_id = incident.rule_id

        async def then() -> None:
            for payload in payloads:
                await self.queue(payload, channels, rule_id=rule_id, now_ms=now_ms)

        return change, then

    async def summaries(self, hour: int, now_ms: float) -> int:
        """Each organization's ``suppressed_summary`` of ``hour`` (an hour since the epoch, on
        the store clock), queued once (the unique index) to its default channels, not counted:
        how many were queued here."""
        start, end = hour * HOUR_MS, (hour + 1) * HOUR_MS
        queued = 0
        for found in await self.outbox.suppressed(start, end):
            async with self.outbox.delivery_transaction():
                notification_id = self.new_id(NOTIFICATION_ID_PREFIX)
                if not await self.outbox.add_summary(
                    found.organization_id, hour, notification_id, now_ms
                ):
                    continue  # another holder's (M26)
                org = found.organization_id
                payload = AlertPayload(
                    notification_id, "suppressed_summary", now_ms, org,
                    console_path=f"/v1/admin/orgs/{org}/alert-channels",
                    details=PayloadDetails(
                        notifications=found.notifications, rules=found.rules,
                        period_start=iso(start), period_end=iso(end),
                    ),
                )  # fmt: skip
                channels = await self.destinations(org, [ORGANIZATION_DEFAULTS])
                await self.queue(payload, channels, counted=False, now_ms=now_ms)
                queued += 1
        return queued

    # --- the notices of a change ----------------------------------------------------------------

    async def rule_weakened(
        self, application_id: str, before: AlertRule, fields: Sequence[str], *, deleted: bool
    ) -> int:
        """The notice of a weakened rule, to its channels as they were before the write."""
        application = await self._applications.get_application(application_id)
        if application is None:
            return 0
        organization_id = application.organization_id
        destinations = await self.destinations(organization_id, before.spec.channels)
        payload = AlertPayload(
            self.new_id(NOTIFICATION_ID_PREFIX),
            "rule_weakened",
            self._epoch.epoch_ms(),
            organization_id,
            PayloadApplication(application.id, application.name),
            rule_payload(before),
            None,
            f"/v1/admin/orgs/{organization_id}/applications/{application.id}"
            f"/alert-rules/{before.id}",
            PayloadDetails(fields=tuple(fields), deleted=deleted),
        )
        return await self.queue(payload, destinations, rule_id=before.id)

    async def channel_removed(self, channel: Channel) -> None:
        destinations = await self.destinations(
            channel.organization_id, [ORGANIZATION_DEFAULTS], exclude=channel.id
        )
        payload = AlertPayload(
            self.new_id(NOTIFICATION_ID_PREFIX),
            "channel_removed",
            self._epoch.epoch_ms(),
            channel.organization_id,
            console_path=f"/v1/admin/orgs/{channel.organization_id}/alert-channels/{channel.id}",
            details=PayloadDetails(channel=channel.id),
        )
        await self.queue(payload, destinations)

    async def channel_gone(self, channel_id: str) -> None:
        await self.outbox.fail_channel(channel_id, CHANNEL_GONE, self._epoch.epoch_ms())
