"""API keys, application stores and the rate limiter, below the HTTP layer."""

from __future__ import annotations

import hmac
import random
import re
import sqlite3
from collections.abc import Iterator, Mapping
from pathlib import Path

import pytest

from gateway import apikeys
from gateway.apikeys import (
    INVALID_KEY_MESSAGE,
    KEY_PATTERN,
    ApiKeyService,
    InvalidApiKeyError,
    PepperError,
    load_pepper,
)
from gateway.applications import (
    ApiKeyRecord,
    Application,
    ApplicationStore,
    InMemoryApplicationStore,
    KeyLookup,
    SqliteApplicationStore,
    application_from_template,
    seed_demo_applications,
)
from gateway.clock import ClockEpoch, SimClock
from gateway.database import open_database
from gateway.migrations import MIGRATIONS, MigrationRunner
from gateway.organizations import new_organization
from gateway.ratelimit import RateLimitedError, RequestRateLimiter
from gateway.routing import load_plans
from helpers import TEST_PEPPER

HOUR_MS = 3_600_000.0


@pytest.fixture(params=["memory", "sqlite"])
def store(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[ApplicationStore]:
    if request.param == "memory":
        yield InMemoryApplicationStore()
        return
    connection = open_database(tmp_path / "gateway.db")
    MigrationRunner(connection, MIGRATIONS, ClockEpoch(SimClock())).migrate()
    yield SqliteApplicationStore(connection)
    connection.close()


def _service(store: ApplicationStore, clock: SimClock, **kwargs: float) -> ApiKeyService:
    seed_demo_applications(store, load_plans(), created_at_ms=0.0)
    rng = random.Random(1)
    return ApiKeyService(
        store, TEST_PEPPER, ClockEpoch(clock), random_bytes=rng.randbytes, **kwargs
    )


# --- the stores ------------------------------------------------------------------------------


def test_applications_round_trip_in_creation_order(store: ApplicationStore) -> None:
    templates = load_plans()
    made = seed_demo_applications(store, templates, created_at_ms=5.0)
    assert [a.id for a in made] == ["initech", "globex", "acme"]
    assert store.list_applications() == made
    acme = store.get_application("acme")
    assert acme == Application(
        "acme", "Acme", templates.plans["enterprise"], None, None, 5.0, "default"
    )
    assert store.get_application("nobody") is None
    # Seeding again creates nothing; a duplicate id is refused.
    assert seed_demo_applications(store, templates, created_at_ms=6.0) == []
    with pytest.raises(ValueError, match="exists"):
        store.add_application(made[0])


def test_key_records_round_trip_and_change(store: ApplicationStore) -> None:
    seed_demo_applications(store, load_plans(), created_at_ms=0.0)
    record = ApiKeyRecord("0123456789abcdef", "acme", "backend", "f" * 64, 10.0)
    store.add_key(record)
    assert store.get_key(record.key_id) == record
    store.set_key_expiry(record.key_id, 99.0)
    store.record_last_used({record.key_id: 50.0})
    store.record_last_used({record.key_id: 40.0})  # never moves backwards
    store.revoke_key(record.key_id)
    changed = store.get_key(record.key_id)
    assert changed is not None
    assert (changed.expires_at_ms, changed.last_used_at_ms, changed.revoked) == (99.0, 50.0, True)
    assert store.list_keys("acme") == [changed]
    assert store.list_keys("globex") == []
    with pytest.raises(ValueError, match="exists"):
        store.add_key(record)  # the id exists
    with pytest.raises(ValueError, match="application"):
        store.add_key(ApiKeyRecord("fedcba9876543210", "nobody", "x", "0" * 64, 0.0))
    with pytest.raises(KeyError):
        store.revoke_key("no-such-key")


def test_application_from_template_copies_the_plan_and_its_limits() -> None:
    templates = load_plans()
    app = application_from_template(
        "shop", "Shop", templates, "pro", organization_id="default", created_at_ms=1.0
    )
    assert (app.plan, app.max_tokens, app.requests_per_minute) == (
        templates.plans["pro"],
        None,
        None,
    )
    with pytest.raises(ValueError, match="unknown plan"):
        application_from_template(
            "shop", "Shop", templates, "platinum", organization_id="default", created_at_ms=1.0
        )


# --- issuing and checking keys ---------------------------------------------------------------


def test_a_key_has_a_recognisable_prefix_and_at_least_128_bits_of_secret(
    store: ApplicationStore,
) -> None:
    keys = _service(store, SimClock())
    issued = [keys.issue("acme", f"k{i}") for i in range(20)]
    for one in issued:
        match = KEY_PATTERN.fullmatch(one.key)
        assert match is not None
        assert match.group("id") == one.record.key_id
        secret = one.key[len("gwk_") + 16 + 1 :]  # base64url may itself contain "_"
        assert len(secret) == 43  # 32 bytes in unpadded base64url: 256 bits
    assert len({one.key for one in issued}) == 20
    assert apikeys.SECRET_BYTES * 8 >= 128


def test_only_the_hmac_is_stored_never_the_key(store: ApplicationStore) -> None:
    keys = _service(store, SimClock())
    issued = keys.issue("acme", "backend")
    record = store.get_key(issued.record.key_id)
    assert record is not None
    expected = hmac.new(TEST_PEPPER, issued.key.encode(), "sha256").hexdigest()
    assert record.key_hmac == expected
    assert issued.key not in repr(record)
    assert issued.key not in repr(issued)
    assert "pepper" in repr(keys)
    assert TEST_PEPPER.decode() not in repr(keys)


def test_the_key_finds_its_application(store: ApplicationStore) -> None:
    keys = _service(store, SimClock())
    key = keys.issue("globex", "backend").key
    assert keys.authenticate(key).id == "globex"


@pytest.mark.parametrize(
    "mangle",
    [
        lambda k: k[:-1] + ("A" if k[-1] != "A" else "B"),  # wrong secret, right id
        lambda k: k.replace(k[4:20], "0" * 16),  # unknown id
        lambda k: k[:-3],  # malformed
        lambda k: "",
        lambda k: "Bearer " + k,
        lambda k: k.upper(),
    ],
    ids=["wrong-secret", "unknown-id", "truncated", "empty", "doubled-scheme", "upper-case"],
)
def test_every_wrong_key_is_the_same_error(store: ApplicationStore, mangle: object) -> None:
    keys = _service(store, SimClock())
    key = keys.issue("acme", "backend").key
    assert callable(mangle)
    with pytest.raises(InvalidApiKeyError) as excinfo:
        keys.authenticate(mangle(key))
    assert str(excinfo.value) == INVALID_KEY_MESSAGE


async def test_revoked_expired_and_rotated_keys(store: ApplicationStore) -> None:
    clock = SimClock()
    keys = _service(store, clock)
    revoked = keys.issue("acme", "revoked")
    expiring = keys.issue("acme", "expiring", expires_at_ms=1000.0)
    old = keys.issue("acme", "old")

    keys.revoke(revoked.record.key_id)
    new = keys.rotate(old.record.key_id, grace_ms=HOUR_MS)
    assert new.record.label == "old"
    assert new.record.application_id == "acme"

    with pytest.raises(InvalidApiKeyError):
        keys.authenticate(revoked.key)
    # Before its expiry and inside the grace period, both still work.
    assert keys.authenticate(expiring.key).id == "acme"
    assert keys.authenticate(old.key).id == "acme"
    await clock.sleep_ms(1000.0)
    with pytest.raises(InvalidApiKeyError):
        keys.authenticate(expiring.key)  # expired exactly at its expiry
    await clock.sleep_ms(HOUR_MS)
    with pytest.raises(InvalidApiKeyError):
        keys.authenticate(old.key)  # the grace period is over
    assert keys.authenticate(new.key).id == "acme"
    with pytest.raises(ValueError, match="no active key"):
        keys.rotate(revoked.record.key_id, grace_ms=0.0)


def test_rotation_never_extends_an_earlier_expiry(store: ApplicationStore) -> None:
    keys = _service(store, SimClock())
    old = keys.issue("acme", "old", expires_at_ms=10.0)
    keys.rotate(old.record.key_id, grace_ms=HOUR_MS)
    record = store.get_key(old.record.key_id)
    assert record is not None
    assert record.expires_at_ms == 10.0


def test_a_key_whose_application_is_gone_is_invalid() -> None:
    store = InMemoryApplicationStore()
    keys = _service(store, SimClock())
    key = keys.issue("acme", "backend").key
    del store._applications["acme"]
    with pytest.raises(InvalidApiKeyError):
        keys.authenticate(key)


async def test_every_attempt_takes_the_same_steps_whatever_is_wrong_with_the_key(
    store: ApplicationStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Structural, not a timing test: every attempt does one record lookup, one HMAC of the
    presented key and one constant-time comparison of two 64-character digests, in that
    order, whether the key is valid, has a wrong secret, an unknown id, is malformed,
    revoked or expired, or belongs to a disabled organization. What timing differences
    remain was not measured (docs/20)."""
    clock = SimClock()
    keys = _service(store, clock)
    store.add_organization(new_organization("north", "North", "business", created_at_ms=0.0))
    store.add_application(
        application_from_template(
            "north-shop", "Shop", load_plans(), "pro", organization_id="north", created_at_ms=0.0
        )
    )
    disabled = keys.issue("north-shop", "disabled organization").key
    store.set_organization_disabled("north", True)
    valid = keys.issue("acme", "valid").key
    revoked = keys.issue("acme", "revoked")
    keys.revoke(revoked.record.key_id)
    expired = keys.issue("acme", "expired", expires_at_ms=1.0)
    await clock.sleep_ms(1.0)
    attempts = {
        "valid": valid,
        "wrong secret": valid[:-1] + ("x" if not valid.endswith("x") else "y"),
        "unknown id": "gwk_" + "0" * 16 + "_" + "A" * 43,
        "malformed": "nonsense",
        "empty": "",
        "revoked": revoked.key,
        "expired": expired.key,
        "disabled organization": disabled,
    }

    steps: list[tuple[str, object]] = []
    real_lookup, real_hmac, real_compare = store.lookup_key, keys._hmac, hmac.compare_digest

    def lookup_key(key_id: str) -> KeyLookup | None:
        steps.append(("lookup", len(key_id)))
        return real_lookup(key_id)

    def digest(key: str) -> str:
        steps.append(("hmac", None))
        return real_hmac(key)

    def compare(a: str, b: str) -> bool:
        steps.append(("compare", (len(a), len(b))))
        return real_compare(a, b)

    monkeypatch.setattr(store, "lookup_key", lookup_key)
    monkeypatch.setattr(keys, "_hmac", digest)
    monkeypatch.setattr(hmac, "compare_digest", compare)

    for name, key in attempts.items():
        steps.clear()
        try:
            keys.authenticate(key)
        except InvalidApiKeyError:
            assert name != "valid"
        assert steps == [("lookup", 16), ("hmac", None), ("compare", (64, 64))], name


# --- last used, without a write per request -------------------------------------------------


class CountingStore(InMemoryApplicationStore):
    def __init__(self) -> None:
        super().__init__()
        self.flushes: list[dict[str, float]] = []

    def record_last_used(self, times_ms: Mapping[str, float]) -> None:
        self.flushes.append(dict(times_ms))
        super().record_last_used(times_ms)


async def test_last_used_is_written_in_batches_at_most_once_per_interval() -> None:
    clock = SimClock()
    store = CountingStore()
    keys = _service(store, clock, last_used_flush_ms=60_000.0)
    a, b = keys.issue("acme", "a"), keys.issue("globex", "b")

    for _ in range(500):
        keys.authenticate(a.key)
        keys.authenticate(b.key)
        await clock.sleep_ms(100.0)  # 1000 requests over 50 s: still inside the interval
    assert store.flushes == []
    assert store.get_key(a.record.key_id).last_used_at_ms is None  # type: ignore[union-attr]

    await clock.sleep_ms(10_000.0)
    keys.authenticate(a.key)  # the first request after the interval writes the batch
    assert len(store.flushes) == 1
    assert set(store.flushes[0]) == {a.record.key_id, b.record.key_id}
    assert store.get_key(a.record.key_id).last_used_at_ms == clock.now_ms()  # type: ignore[union-attr]
    assert keys.flush_last_used() == 0  # nothing pending


def test_flush_writes_what_is_pending(store: ApplicationStore) -> None:
    clock = SimClock(start_ms=5.0)
    keys = _service(store, clock)
    issued = keys.issue("acme", "a")
    keys.authenticate(issued.key)
    assert keys.flush_last_used() == 1
    assert store.get_key(issued.record.key_id).last_used_at_ms == 5.0  # type: ignore[union-attr]


# --- the pepper ------------------------------------------------------------------------------


def test_the_pepper_must_be_set_and_long_enough() -> None:
    assert load_pepper({"GATEWAY_KEY_PEPPER": "p" * 32}) == b"p" * 32
    for environ in ({}, {"GATEWAY_KEY_PEPPER": ""}, {"GATEWAY_KEY_PEPPER": "short-secret"}):
        with pytest.raises(PepperError) as excinfo:
            load_pepper(environ)
        assert "short-secret" not in str(excinfo.value)
    with pytest.raises(PepperError):
        ApiKeyService(InMemoryApplicationStore(), b"short", ClockEpoch(SimClock()))


def test_a_different_pepper_rejects_every_key(store: ApplicationStore) -> None:
    keys = _service(store, SimClock())
    key = keys.issue("acme", "backend").key
    other = ApiKeyService(store, b"another-pepper-of-enough-length-0123", ClockEpoch(SimClock()))
    with pytest.raises(InvalidApiKeyError):
        other.authenticate(key)


# --- the rate limiter ------------------------------------------------------------------------


async def test_rate_limit_is_a_sliding_minute_per_application() -> None:
    clock = SimClock()
    limiter = RequestRateLimiter(clock)
    for _ in range(3):
        limiter.admit("a", 3)
        await clock.sleep_ms(10_000.0)
    with pytest.raises(RateLimitedError) as excinfo:
        limiter.admit("a", 3)
    # The oldest of the three started at 0 and leaves the window at 60 s; it is now 30 s.
    assert excinfo.value.retry_after_ms == 30_000.0
    limiter.admit("b", 3)  # another application has its own window
    limiter.admit("a", None)  # no limit
    await clock.sleep_ms(30_000.0)
    limiter.admit("a", 3)  # the first left the window


def test_a_refused_request_does_not_count() -> None:
    limiter = RequestRateLimiter(SimClock())
    limiter.admit("a", 1)
    for _ in range(5):
        with pytest.raises(RateLimitedError):
            limiter.admit("a", 1)


def test_the_sqlite_store_needs_the_migrations() -> None:
    connection = open_database(":memory:")
    with pytest.raises(RuntimeError, match="run the migrations"):
        SqliteApplicationStore(connection)
    MigrationRunner(connection, MIGRATIONS, ClockEpoch(SimClock())).migrate(1)
    with pytest.raises(RuntimeError, match="run the migrations"):
        SqliteApplicationStore(connection)


def test_the_key_pattern_matches_exactly_what_is_issued() -> None:
    assert re.fullmatch(KEY_PATTERN, "gwk_" + "a" * 16 + "_" + "B" * 43)
    assert not re.fullmatch(KEY_PATTERN, "gwk_" + "a" * 15 + "_" + "B" * 43)
    assert not KEY_PATTERN.fullmatch("sk_" + "a" * 16 + "_" + "B" * 43)
    assert isinstance(sqlite3.sqlite_version, str)
