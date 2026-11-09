"""Which fields of a rule change make it fire less: the ``rule_weakened`` notice (docs/46, the
main plan's section 14, M29).

Each field has a direction, as settings do. ``weakened_fields(before, after)`` names the fields
whose change makes the rule fire less (or notify less); ``after`` None is a deletion. Pure: the
one place this is decided, called by ``RuleService`` inside its write transaction.

- **The condition:** with the comparison's direction kept (``>``/``>=`` up, ``<``/``<=`` down),
  weaker when the values that meet it now are not all of those that met it before: a higher
  threshold for up, a lower for down, or a strict comparison where it was not. A change of
  direction, of signal or of parameter is a different condition: weaker.
- **Longer** ``window``, ``for_minutes`` or ``cooldown_minutes``; a reminder removed or made
  rarer; quiet hours added or widened (any minute of the week quiet now that was not); a lower
  severity; any channel removed (``organization defaults`` counted as one); disabled.
- The name changes nothing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from gateway.alertrules import QuietHours, RuleSpec

_UP = (">", ">=")
_SEVERITY = {"info": 0, "warning": 1, "critical": 2}
_MINUTE_MS = 60_000.0
# A week of minutes from a Monday (1970-01-05), for comparing quiet hours.
_WEEK = [(4 * 24 * 60 + m) * _MINUTE_MS for m in range(7 * 24 * 60)]


def _condition_weaker(before: RuleSpec, after: RuleSpec) -> bool:
    if (before.comparison in _UP) != (after.comparison in _UP):
        return True
    strict_before = before.comparison in (">", "<")
    strict_after = after.comparison in (">", "<")
    if before.comparison in _UP:
        # Met now by all that met before: t_after < t_before, or equal and not newly strict.
        if after.threshold > before.threshold:
            return True
    elif after.threshold < before.threshold:
        return True
    return after.threshold == before.threshold and strict_after and not strict_before


def _quiet_minutes(quiet: QuietHours | None) -> frozenset[int]:
    if quiet is None:
        return frozenset()
    # Imported here: the evaluator imports the rules module, which imports this one.
    from gateway.evaluator import in_quiet_hours

    return frozenset(i for i, at in enumerate(_WEEK) if in_quiet_hours(quiet, at))


def weakened_fields(before: RuleSpec, after: RuleSpec | None) -> list[str]:
    """The fields whose change from ``before`` to ``after`` makes the rule fire or notify less,
    in a fixed order; ``after`` None (deleted) names ``enabled``. Only an enabled rule can be
    weakened: a disabled one fires not at all already."""
    if not before.enabled:
        return []
    if after is None:
        return ["enabled"]
    found: list[str] = []
    if after.signal != before.signal:
        found.append("signal")
    if after.parameter != before.parameter:
        found.append("parameter")
    if after.signal == before.signal and after.parameter == before.parameter:
        if after.comparison != before.comparison and _condition_weaker(before, after):
            found.append("comparison")
        elif after.threshold != before.threshold and _condition_weaker(before, after):
            found.append("threshold")
    if (after.window or 0) > (before.window or 0):
        found.append("window")
    if after.for_minutes > before.for_minutes:
        found.append("for_minutes")
    if _SEVERITY[after.severity] < _SEVERITY[before.severity]:
        found.append("severity")
    if set(before.channels) - set(after.channels):
        found.append("channels")
    if after.cooldown_minutes > before.cooldown_minutes:
        found.append("cooldown_minutes")
    if before.repeat_every_minutes is not None and (
        after.repeat_every_minutes is None
        or after.repeat_every_minutes > before.repeat_every_minutes
    ):
        found.append("repeat_every_minutes")
    if _quiet_minutes(after.quiet_hours) - _quiet_minutes(before.quiet_hours):
        found.append("quiet_hours")
    if not after.enabled:
        found.append("enabled")
    return found
