"""The notices a change sends, queued in the outbox in that change's transaction (docs/46).

- **``rule_weakened``** (M29): a write that makes an enabled rule fire less goes to the rule's
  channels **as they were before the write**, naming the rule and the fields, never the user.
- **``channel_removed``**: disabling or deleting an enabled channel goes to the organization's
  other default channels.

**Where a notification goes** (``destinations``): ``["organization defaults"]`` is the
organization's enabled default channels; channel ids are those of them that exist **in the
organization** now and are enabled, checked again here at queue time (M15's second check; the
first is the rule write's). ``[]`` is nowhere.

Group 1 queues; group 2's delivery worker sends, and counts these notices against the caps.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING

from gateway.alerts.channels import Channel, ChannelStore
from gateway.alerts.outbox import DELIVERY_ID_PREFIX, Delivery, DeliveryState, OutboxStore
from gateway.alerts.payload import (
    AlertPayload,
    PayloadApplication,
    PayloadDetails,
    PayloadRule,
    PayloadTooLargeError,
)
from gateway.applications import ApplicationStore
from gateway.clock import EpochSource
from gateway.signals import SIGNALS

if TYPE_CHECKING:
    from gateway.alertrules import AlertRule

ORGANIZATION_DEFAULTS = "organization defaults"
NOTIFICATION_ID_PREFIX = "ntf_"
PAYLOAD_TOO_LARGE = "payload_too_large"
CHANNEL_GONE = "channel_gone"


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


class Notices:
    def __init__(
        self,
        channels: ChannelStore,
        outbox: OutboxStore,
        applications: ApplicationStore,
        epoch: EpochSource,
        random_bytes: Callable[[int], bytes],
    ) -> None:
        self.channels = channels
        self.outbox = outbox
        self._applications = applications
        self._epoch = epoch
        self._random_bytes = random_bytes

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

    async def queue(self, payload: AlertPayload, channels: Sequence[Channel]) -> int:
        """One delivery of ``payload`` per channel, in the caller's transaction. A body over the
        size bound is queued as failed (``payload_too_large``), never cut short."""
        if not channels:
            return 0
        now = self._epoch.epoch_ms()
        state, code = DeliveryState.QUEUED, None
        attempt_at: float | None = now
        try:
            body = payload.body()
        except PayloadTooLargeError:
            body, state, code, attempt_at = "", DeliveryState.FAILED, PAYLOAD_TOO_LARGE, None
        return await self.outbox.queue(
            [
                Delivery(
                    self.new_id(DELIVERY_ID_PREFIX), payload.delivery_id, c.id,
                    payload.organization_id, state, 0, attempt_at, None, None, code, body, now,
                    now,
                )
                for c in channels
            ]
        )  # fmt: skip

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
        return await self.queue(payload, destinations)

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
