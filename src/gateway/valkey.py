"""The counters every instance shares, in Valkey.

See docs/35-valkey-counters.md and docs/scale-out-plan.md, sections 3.4, 3.5, 3.7 to 3.9 and 4.

- **One limit for every instance.** Each rule that reads a count and then writes it is one Lua
  script, so no interleaving of instances can break it: a sliding window that checks every
  window it is given and counts in all of them or none (the request rate limit, the
  user-creation cap, the test-connection counts), the audit cap, and the circuit breakers.
- **Windows are timed on the instance's ``EpochSource``** (calendar time, comparable between
  instances), never on ``Clock`` (per process) and never on Valkey's ``TIME``, which is read
  only to measure how far apart the two are.
- **Missing is forbidden.** Every call has a timeout on the injected clock. A call that fails,
  times out, or is made while the instance's clock is too far from Valkey's, refuses what it
  would have decided; the table in docs/35 says how for each counter. Never open.
- **No name in Valkey says whose it is.** Every key is an HMAC, under a key derived from the
  pepper for that purpose alone, of what it counts (an application id, a username's HMAC, a
  session digest, a provider id); its value is a count or a time.
- **The store is watched** (``ValkeyWatch``): at startup it must be safe to count in (no
  eviction, append-only persistence) and within the skew bound, or the server does not start;
  every 30 s after, the skew again and the run id, whose change means a restart.
"""

from __future__ import annotations

import hashlib
import hmac
import itertools
import logging
import math
import secrets
from collections.abc import Awaitable, Callable, Mapping
from functools import partial
from typing import Any, TypeVar, cast

from redis.asyncio import Redis
from redis.exceptions import RedisError

from gateway.audit import (
    AuditAction,
    AuditEvent,
    AuditLog,
    DroppedRows,
    Outcome,
    Reason,
    TargetType,
)
from gateway.auth import LOCK_FAILURES, LOCK_WINDOW_MS
from gateway.breaker import (
    CLOSE_AFTER,
    FAILURE_THRESHOLD,
    MAX_OPEN_MS,
    OPEN_MS,
    BreakerState,
    Transition,
)
from gateway.clock import Clock, EpochSource
from gateway.counters import OnRestart
from gateway.keyderivation import KeyPurpose, derive_key
from gateway.management import ManagementRefusal
from gateway.overload import RequestTimeoutError, run_with_timeout
from gateway.providers import HOUR_MS, TESTS_PER_HOUR, TESTS_PER_ORGANIZATION_HOUR
from gateway.ratelimit import WINDOW_MS, RateLimitedError
from gateway.state import StateUnavailableError

T = TypeVar("T")
LOG = logging.getLogger("gateway.valkey")

COUNTERS_ENV = "GATEWAY_COUNTERS"
URL_ENV = "GATEWAY_VALKEY_URL"
MAX_SKEW_ENV = "GATEWAY_STATE_MAX_SKEW_MS"
DEFAULT_TIMEOUT_MS = 200.0
DEFAULT_MAX_SKEW_MS = 1000.0
CHECK_EVERY_MS = 30_000.0
MINUTE_MS = 60_000.0
DAY_MS = 24 * 60 * MINUTE_MS
# How long a breaker's state, a dropped-row count and a restart marker are kept when untouched.
# An integer: PEXPIRE takes whole milliseconds and refuses "86400000.0".
KEEP_MS = int(DAY_MS)
# How long a half-open probe holds its slot if its instance dies before reporting (decision 10).
PROBE_MS = 120_000.0

# KEYS: one sorted set per window. ARGV: now, member, then (limit, window_ms) per key.
# Every window is checked first; only if none is full is the member added to all of them.
# Returns {0, ""} when counted, or {i, retry_after_ms} for the first full window i (1-based).
WINDOW_ADMIT = """
local now = tonumber(ARGV[1])
for i, key in ipairs(KEYS) do
  local limit = tonumber(ARGV[2 * i + 1])
  local window = tonumber(ARGV[2 * i + 2])
  redis.call('ZREMRANGEBYSCORE', key, '-inf', now - window)
  local count = redis.call('ZCARD', key)
  if count > limit then
    redis.call('ZPOPMIN', key, count - limit)
    count = limit
  end
  if count >= limit then
    local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
    return {i, tostring(tonumber(oldest[2]) + window - now)}
  end
end
for i, key in ipairs(KEYS) do
  local window = tonumber(ARGV[2 * i + 2])
  redis.call('ZADD', key, now, ARGV[2])
  redis.call('PEXPIRE', key, math.ceil(window))
end
return {0, ''}
"""

# KEYS: this minute's count, this minute's dropped count, the set of minutes that dropped rows.
# ARGV: the cap, the minute, how long to keep. Returns 1 if the row may be written, else 0.
AUDIT_TAKE = """
local written = redis.call('INCR', KEYS[1])
redis.call('PEXPIRE', KEYS[1], ARGV[3])
if written <= tonumber(ARGV[1]) then return 1 end
redis.call('INCR', KEYS[2])
redis.call('PEXPIRE', KEYS[2], ARGV[3])
redis.call('SADD', KEYS[3], ARGV[2])
redis.call('PEXPIRE', KEYS[3], ARGV[3])
return 0
"""

# KEYS: a minute's dropped count, the set of minutes. ARGV: the minute. Takes the count once:
# whichever instance runs this first gets it, every other gets nothing.
AUDIT_COLLECT = """
local dropped = redis.call('GETDEL', KEYS[1])
redis.call('SREM', KEYS[2], ARGV[1])
return dropped
"""

# KEYS: one breaker's hash. ARGV: op, now, failure threshold, open ms, max open ms, close after,
# probe ms, keep ms. The transitions of gateway.breaker.Breakers, plus the probe's expiry.
# Returns {admitted (1/0), the state moved to or "", open_ms or "", the current state}.
BREAKER = """
local key = KEYS[1]
local op, now = ARGV[1], tonumber(ARGV[2])
local threshold, base, max_open = tonumber(ARGV[3]), tonumber(ARGV[4]), tonumber(ARGV[5])
local close_after, probe_ms, keep_ms = tonumber(ARGV[6]), tonumber(ARGV[7]), ARGV[8]
local state = redis.call('HGET', key, 'state') or 'closed'
local moved, admitted = '', 1
local function open(ms)
  if ms > max_open then ms = max_open end
  redis.call('HSET', key, 'state', 'open', 'open_ms', ms, 'open_until', now + ms,
    'failures', 0, 'successes', 0)
  redis.call('HDEL', key, 'probe_until')
  state, moved = 'open', 'open'
end
if op == 'state' then
  return {1, '', '', state}
elseif op == 'admit' then
  if state == 'open' then
    if now < tonumber(redis.call('HGET', key, 'open_until')) then return {0, '', '', state} end
    redis.call('HSET', key, 'state', 'half_open')
    redis.call('HDEL', key, 'probe_until')
    state, moved = 'half_open', 'half_open'
  end
  if state == 'half_open' then
    if now < tonumber(redis.call('HGET', key, 'probe_until') or '0') then
      admitted = 0
    else
      redis.call('HSET', key, 'probe_until', now + probe_ms)
    end
  end
elseif op == 'release' then
  redis.call('HDEL', key, 'probe_until')
elseif op == 'success' then
  redis.call('HSET', key, 'failures', 0)
  redis.call('HDEL', key, 'probe_until')
  if state == 'half_open' then
    if redis.call('HINCRBY', key, 'successes', 1) >= close_after then
      redis.call('HSET', key, 'state', 'closed', 'successes', 0, 'open_ms', 0)
      state, moved = 'closed', 'closed'
    end
  end
elseif op == 'failure' then
  redis.call('HDEL', key, 'probe_until')
  if state == 'half_open' then
    local was = tonumber(redis.call('HGET', key, 'open_ms') or '0')
    if was > 0 then open(was * 2) else open(base) end
  elseif redis.call('HINCRBY', key, 'failures', 1) >= threshold then
    open(base)
  end
end
redis.call('PEXPIRE', key, keep_ms)
return {admitted, moved, redis.call('HGET', key, 'open_ms') or '', state}
"""


class ValkeyNotSafeError(RuntimeError):
    """Valkey is not set up to count in: it could evict a counter (a forgotten failure is an
    admitted attempt) or lose them all on a restart. The server refuses to start."""


class ValkeyCounters:
    """Every counter of one gateway instance, in one Valkey database that other instances
    share. ``clock`` times the calls out (``WallClock`` in the server); ``epoch`` is what
    every window is timed on; ``secret`` is the pepper, from which the key for counter names
    and the login limiter's key are derived."""

    def __init__(
        self,
        client: Redis,
        clock: Clock,
        epoch: EpochSource,
        secret: bytes,
        *,
        prefix: str = "gw:",
        timeout_ms: float = DEFAULT_TIMEOUT_MS,
        max_skew_ms: float = DEFAULT_MAX_SKEW_MS,
        check_every_ms: float = CHECK_EVERY_MS,
        audit: AuditLog | None = None,
        random_bytes: Callable[[int], bytes] = secrets.token_bytes,
    ) -> None:
        self.client = client
        self.clock = clock
        self.epoch = epoch
        self.prefix = prefix
        self._timeout_ms = timeout_ms
        self._names = derive_key(secret, KeyPurpose.COUNTER_KEYS)
        self._login_key = derive_key(secret, KeyPurpose.LOGIN_LIMITER)
        self._instance = random_bytes(8).hex()
        self._sequence = itertools.count()
        # False while the instance's clock is past the skew bound: every call is refused.
        self.healthy = True
        self.window_admit = client.register_script(WINDOW_ADMIT)
        self.audit_take = client.register_script(AUDIT_TAKE)
        self.audit_collect = client.register_script(AUDIT_COLLECT)
        self.breaker = client.register_script(BREAKER)
        self._watch = ValkeyWatch(self, max_skew_ms, check_every_ms, audit)

    def __repr__(self) -> str:
        return f"ValkeyCounters(prefix={self.prefix!r}, <client and keys redacted>)"

    @property
    def watch(self) -> ValkeyWatch:
        return self._watch

    def name(self, kind: str, identifier: str) -> str:
        """A key's name: what it counts, as an HMAC; never the identifier itself."""
        digest = hmac.new(self._names, identifier.encode(), hashlib.sha256).hexdigest()
        return f"{self.prefix}{kind}:{digest[:32]}"

    def login_key(self, username: str) -> str:
        return hmac.new(self._login_key, username.encode(), hashlib.sha256).hexdigest()

    def member(self, now_ms: float) -> str:
        """A window entry: unique even when two instances count in the same millisecond."""
        return f"{now_ms!r}:{self._instance}:{next(self._sequence)}"

    async def call(self, work: Callable[[], Awaitable[T]]) -> T:
        """Run one Valkey call, or raise ``StateUnavailableError``: refused while the clock
        is past the bound, and on any failure or timeout. The error says which, never the
        address or what was sent."""
        if not self.healthy:
            raise StateUnavailableError("clock_skew")
        try:
            return await run_with_timeout(self.clock, work(), self._timeout_ms)
        except RequestTimeoutError:
            raise StateUnavailableError("timeout") from None
        except (RedisError, OSError) as exc:
            raise StateUnavailableError(type(exc).__name__) from None

    def rate_limiter(self) -> ValkeyRateLimiter:
        return ValkeyRateLimiter(self)

    def failure_window(self, kind: str, max_failures: int, window_ms: float) -> ValkeyWindow:
        return ValkeyWindow(self, kind, max_failures, window_ms)

    def connection_tests(
        self,
        tests_per_hour: int = TESTS_PER_HOUR,
        tests_per_organization_hour: int = TESTS_PER_ORGANIZATION_HOUR,
    ) -> ValkeyConnectionTestCounter:
        return ValkeyConnectionTestCounter(self, tests_per_hour, tests_per_organization_hour)

    def breakers(
        self,
        on_transition: Callable[[Transition], None] | None = None,
        *,
        failure_threshold: int = FAILURE_THRESHOLD,
        open_ms: float = OPEN_MS,
        max_open_ms: float = MAX_OPEN_MS,
        close_after: int = CLOSE_AFTER,
        probe_ms: float = PROBE_MS,
    ) -> ValkeyBreakers:
        return ValkeyBreakers(
            self,
            on_transition,
            (failure_threshold, open_ms, max_open_ms, close_after, probe_ms),
        )

    def audit_cap(self, rows_per_minute: int) -> ValkeyAuditCap:
        return ValkeyAuditCap(self, rows_per_minute)

    async def aclose(self) -> None:
        await self.client.aclose()


def _unavailable(what: str, exc: StateUnavailableError) -> None:
    LOG.warning("%s refused: the shared counters are unavailable (%s)", what, exc)


class ValkeyRateLimiter:
    """Requests per minute per application, one window for every instance."""

    def __init__(self, counters: ValkeyCounters) -> None:
        self._counters = counters

    async def admit(self, application_id: str, requests_per_minute: int | None) -> None:
        """Count one request, or raise ``RateLimitedError`` (counting nothing). Unavailable:
        ``StateUnavailableError``, which the server answers 503: the request is not served."""
        if requests_per_minute is None:
            return
        c = self._counters
        now = c.epoch.epoch_ms()
        name = c.name("rate", application_id)
        found = await c.call(
            lambda: c.window_admit(
                keys=[name], args=[now, c.member(now), requests_per_minute, WINDOW_MS]
            )
        )
        refused, retry_after = cast(list[Any], found)
        if int(refused):
            raise RateLimitedError(application_id, requests_per_minute, float(retry_after))


class ValkeyWindow:
    """Failures per key in a sliding window, shared (``FailureWindow``): the login limiter,
    the step-up session cap and the user-creation cap. Unavailable: ``blocked`` says yes and
    ``admit`` says no (refused); a failure that cannot be recorded is not, because the attempt
    it records has been refused already."""

    def __init__(
        self, counters: ValkeyCounters, kind: str, max_failures: int, window_ms: float
    ) -> None:
        self._counters = counters
        self._kind = kind
        self._max = max_failures
        self._window_ms = window_ms

    def key(self, username: str) -> str:
        return self._counters.login_key(username)

    async def blocked(self, key: str) -> bool:
        c = self._counters
        now = c.epoch.epoch_ms()
        name = c.name(self._kind, key)
        try:
            count = await c.call(lambda: c.client.zcount(name, f"({now - self._window_ms}", "+inf"))
        except StateUnavailableError as exc:
            _unavailable(self._kind, exc)
            return True
        return int(count) >= self._max

    async def record_failure(self, key: str) -> None:
        c = self._counters
        now = c.epoch.epoch_ms()
        name = c.name(self._kind, key)

        async def record() -> None:
            async with c.client.pipeline(transaction=True) as pipe:
                pipe.zremrangebyscore(name, "-inf", now - self._window_ms)
                pipe.zadd(name, {c.member(now): now})
                pipe.pexpire(name, math.ceil(self._window_ms))
                await pipe.execute()

        try:
            await c.call(record)
        except StateUnavailableError as exc:
            _unavailable(self._kind, exc)

    async def admit(self, key: str) -> bool:
        c = self._counters
        now = c.epoch.epoch_ms()
        name = c.name(self._kind, key)
        try:
            found = await c.call(
                lambda: c.window_admit(
                    keys=[name], args=[now, c.member(now), self._max, self._window_ms]
                )
            )
        except StateUnavailableError as exc:
            _unavailable(self._kind, exc)
            return False
        return int(cast(list[Any], found)[0]) == 0

    async def clear(self, key: str) -> None:
        # Not cleared, the count only stays stricter: never an admission.
        c = self._counters
        name = c.name(self._kind, key)
        try:
            await c.call(lambda: c.client.delete(name))
        except StateUnavailableError as exc:
            _unavailable(self._kind, exc)


class ValkeyConnectionTestCounter:
    """Test-connections per provider and per organization an hour: both windows counted, or
    neither. Unavailable: ``StateUnavailableError`` (503), no test is run."""

    def __init__(self, counters: ValkeyCounters, per_hour: int, per_organization_hour: int) -> None:
        self._counters = counters
        self._per_hour = per_hour
        self._per_organization_hour = per_organization_hour

    async def count(self, provider_id: str, organization_id: str) -> None:
        c = self._counters
        now = c.epoch.epoch_ms()
        keys = [c.name("tests", provider_id), c.name("org-tests", organization_id)]
        args: list[str | float] = [
            now,
            c.member(now),
            self._per_hour,
            HOUR_MS,
            self._per_organization_hour,
            HOUR_MS,
        ]
        found = await c.call(lambda: c.window_admit(keys=keys, args=args))
        full = int(cast(list[Any], found)[0])
        if full:
            limit, what = (
                (self._per_hour, "a provider")
                if full == 1
                else (self._per_organization_hour, "an organization's providers")
            )
            raise ManagementRefusal(
                Reason.TEST_RATE_LIMITED,
                f"at most {limit} tests of {what} an hour; try again later",
            )


class ValkeyBreakers:
    """The circuit breakers of ``gateway.breaker.Breakers``, shared: one probe at a time
    across every instance, and a probe whose instance died frees its slot after ``probe_ms``.
    Unavailable: ``admit`` says no (the provider is skipped: a request never goes further than
    its mode allows because of it), ``state`` says open, and the reports are dropped."""

    def __init__(
        self,
        counters: ValkeyCounters,
        on_transition: Callable[[Transition], None] | None,
        settings: tuple[int, float, float, int, float],
    ) -> None:
        self._counters = counters
        self._on_transition = on_transition
        self._settings = settings

    async def _run(self, op: str, organization_id: str, provider_id: str) -> list[Any]:
        c = self._counters
        name = c.name("breaker", f"{organization_id}\x00{provider_id}")
        args: list[str | float] = [op, c.epoch.epoch_ms(), *self._settings, KEEP_MS]
        found = cast(list[Any], await c.call(lambda: c.breaker(keys=[name], args=args)))
        moved, open_ms = str(found[1]), str(found[2])
        if moved and self._on_transition is not None:
            self._on_transition(
                Transition(
                    organization_id,
                    provider_id,
                    BreakerState(moved),
                    float(open_ms) if moved == BreakerState.OPEN.value else None,
                )
            )
        return found

    async def admit(self, organization_id: str, provider_id: str) -> bool:
        try:
            found = await self._run("admit", organization_id, provider_id)
        except StateUnavailableError as exc:
            _unavailable("breaker", exc)
            return False
        return int(found[0]) == 1

    async def _report(self, op: str, organization_id: str, provider_id: str) -> None:
        try:
            await self._run(op, organization_id, provider_id)
        except StateUnavailableError as exc:
            _unavailable("breaker", exc)

    async def release(self, organization_id: str, provider_id: str) -> None:
        await self._report("release", organization_id, provider_id)

    async def success(self, organization_id: str, provider_id: str) -> None:
        await self._report("success", organization_id, provider_id)

    async def failure(self, organization_id: str, provider_id: str) -> None:
        await self._report("failure", organization_id, provider_id)

    async def state(self, organization_id: str, provider_id: str) -> BreakerState:
        try:
            found = await self._run("state", organization_id, provider_id)
        except StateUnavailableError as exc:
            _unavailable("breaker", exc)
            return BreakerState.OPEN
        return BreakerState(str(found[3]))


class ValkeyAuditCap:
    """The per-minute cap on unknown-user audit rows, shared. Unavailable: the row is dropped
    (writing it would let a flood through) and counted here, and that count is written as its
    own ``audit_rows_dropped`` entry once the minute is over."""

    def __init__(self, counters: ValkeyCounters, rows_per_minute: int) -> None:
        if rows_per_minute < 1:
            raise ValueError("unknown_user_rows_per_minute must be at least 1")
        self._counters = counters
        self._cap = rows_per_minute
        self._local: dict[int, int] = {}

    def _keys(self, minute: int) -> tuple[str, str, str]:
        p = self._counters.prefix
        return f"{p}audit:written:{minute}", f"{p}audit:dropped:{minute}", f"{p}audit:minutes"

    async def take(self, at_ms: float) -> bool:
        c = self._counters
        minute = math.floor(at_ms / MINUTE_MS)
        keys = list(self._keys(minute))
        try:
            found = await c.call(lambda: c.audit_take(keys=keys, args=[self._cap, minute, KEEP_MS]))
        except StateUnavailableError as exc:
            _unavailable("audit row", exc)
            self._local[minute] = self._local.get(minute, 0) + 1
            return False
        return int(cast(int, found)) == 1

    async def collect(self, now_ms: float) -> DroppedRows | None:
        current = math.floor(now_ms / MINUTE_MS)
        done = sorted(m for m in self._local if m < current)
        if done:
            minute = done[0]
            return DroppedRows(minute * MINUTE_MS, self._local.pop(minute))
        c = self._counters
        _, _, minutes_key = self._keys(current)
        try:
            minutes = await c.call(lambda: c.client.smembers(minutes_key))
            for minute in sorted(int(m) for m in cast(set[Any], minutes)):
                if minute >= current:
                    continue
                _, dropped_key, _ = self._keys(minute)
                found = await c.call(
                    partial(c.audit_collect, keys=[dropped_key, minutes_key], args=[minute])
                )
                if found is not None:
                    return DroppedRows(minute * MINUTE_MS, int(cast(str, found)))
        except StateUnavailableError as exc:
            _unavailable("dropped-row count", exc)
        return None


class ValkeyWatch:
    """Watches the store (``CounterWatch``): its settings and the skew at startup, then the
    skew and the run id every ``check_every_ms``."""

    def __init__(
        self,
        counters: ValkeyCounters,
        max_skew_ms: float,
        check_every_ms: float,
        audit: AuditLog | None,
    ) -> None:
        self._counters = counters
        self._max_skew_ms = max_skew_ms
        self._check_every_ms = check_every_ms
        # Where the restart row goes; set after construction when the audit log needs these
        # counters for its own cap.
        self.audit = audit
        self.run_id: str | None = None
        self.skew_ms: float | None = None

    async def skew(self) -> float:
        """How far Valkey's clock is from the instance's epoch, from the midpoint of one round
        trip. Valkey's time is read for this and nothing else."""
        c = self._counters
        before = c.epoch.epoch_ms()
        seconds, micros = cast(tuple[int, int], await self._raw(lambda: c.client.time()))
        after = c.epoch.epoch_ms()
        return seconds * 1000 + micros / 1000 - (before + after) / 2

    async def _raw(self, work: Callable[[], Awaitable[T]]) -> T:
        # Not refused while unhealthy: this is what decides whether the instance is.
        c = self._counters
        was, c.healthy = c.healthy, True
        try:
            return await c.call(work)
        finally:
            c.healthy = was

    async def check(self, on_restart: OnRestart) -> None:
        """Measure the skew (refusing every call while it is past the bound) and compare the
        run id with the last one seen."""
        c = self._counters
        self.skew_ms = await self.skew()
        healthy = abs(self.skew_ms) <= self._max_skew_ms
        if healthy != c.healthy:
            LOG.warning(
                "the shared counters are %s: this instance's clock is %.0f ms from Valkey's "
                "(the bound is %.0f ms)",
                "usable again" if healthy else "refused",
                self.skew_ms,
                self._max_skew_ms,
            )
        c.healthy = healthy
        info = cast(dict[str, Any], await self._raw(lambda: c.client.info("server")))
        run_id = str(info["run_id"])
        if self.run_id is not None and run_id != self.run_id:
            await self._restarted(run_id, on_restart)
        self.run_id = run_id

    async def _restarted(self, run_id: str, on_restart: OnRestart) -> None:
        c = self._counters
        LOG.warning("Valkey restarted: every shared counter started again from empty")
        await on_restart(run_id)
        marker = f"{c.prefix}restart:{run_id}"
        # One audit row per restart, whichever instance sees it first.
        first = await self._raw(lambda: c.client.set(marker, "1", nx=True, px=KEEP_MS))
        if first and self.audit is not None:
            await self.audit.write(
                AuditEvent(
                    AuditAction.STATE_STORE_RESTARTED,
                    Outcome.INFO,
                    TargetType.STATE_STORE,
                    details={"store": "valkey"},
                ),
                c.epoch.epoch_ms(),
            )

    async def start(self, on_restart: OnRestart) -> None:
        """Refuse to start (``ValkeyNotSafeError``, ``StateUnavailableError``) unless Valkey
        answers, keeps every counter (``maxmemory-policy noeviction``, ``appendonly yes``) and
        is within the skew bound."""
        c = self._counters
        policy = cast(
            dict[str, str], await self._raw(lambda: c.client.config_get("maxmemory-policy"))
        )
        appendonly = cast(
            dict[str, str], await self._raw(lambda: c.client.config_get("appendonly"))
        )
        if policy.get("maxmemory-policy") != "noeviction" or appendonly.get("appendonly") != "yes":
            raise ValkeyNotSafeError(
                "Valkey must run with maxmemory-policy noeviction and appendonly yes: an evicted "
                "or lost counter is a forgotten failure (docs/35)"
            )
        await self.check(on_restart)
        if not c.healthy:
            raise StateUnavailableError(
                f"this instance's clock is {self.skew_ms:.0f} ms from Valkey's; the bound is "
                f"{self._max_skew_ms:.0f} ms ({MAX_SKEW_ENV})"
            )

    async def run(self, on_restart: OnRestart) -> None:
        while True:
            await self._counters.clock.sleep_ms(self._check_every_ms)
            try:
                await self.check(on_restart)
            except StateUnavailableError as exc:
                _unavailable("the periodic check", exc)


def shared_counters_from(
    environ: Mapping[str, str], clock: Clock, epoch: EpochSource, secret: bytes
) -> ValkeyCounters | None:
    """``GATEWAY_COUNTERS``: ``memory`` (the default: each counter this process's own) or
    ``valkey``, which needs ``GATEWAY_VALKEY_URL`` and takes ``GATEWAY_STATE_MAX_SKEW_MS``.
    Nothing connects until the server starts. Errors name the variable, never its value."""
    kind = environ.get(COUNTERS_ENV, "memory")
    if kind == "memory":
        return None
    if kind != "valkey":
        raise ValueError(f"{COUNTERS_ENV} must be memory or valkey")
    url = environ.get(URL_ENV, "")
    if not url:
        raise ValueError(f"{COUNTERS_ENV}=valkey needs {URL_ENV}")
    try:
        max_skew_ms = float(environ.get(MAX_SKEW_ENV, DEFAULT_MAX_SKEW_MS))
    except ValueError:
        raise ValueError(f"{MAX_SKEW_ENV} must be a number of milliseconds") from None
    if not max_skew_ms > 0:
        raise ValueError(f"{MAX_SKEW_ENV} must be positive")
    client = Redis.from_url(url, decode_responses=True)
    return ValkeyCounters(client, clock, epoch, secret, max_skew_ms=max_skew_ms)


def account_windows(
    counters: ValkeyCounters,
    *,
    step_up_session_failures: int,
    step_up_window_ms: float,
    user_create_attempts: int,
    user_create_window_ms: float,
) -> tuple[ValkeyWindow, ValkeyWindow, ValkeyWindow]:
    """The account service's three windows: the login limiter (its limit and window), the
    step-up session cap and the user-creation cap."""
    return (
        counters.failure_window("login", LOCK_FAILURES, LOCK_WINDOW_MS),
        counters.failure_window("step-up", step_up_session_failures, step_up_window_ms),
        counters.failure_window("user-create", user_create_attempts, user_create_window_ms),
    )
