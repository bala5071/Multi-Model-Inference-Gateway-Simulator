"""argon2id hashing, the two hash runners, and the password policy.

See docs/24-password-hashing.md.
"""

from __future__ import annotations

import asyncio
import threading

import pytest

from gateway.clock import SimClock, WallClock
from gateway.passwords import (
    CHEAP,
    MAX_LENGTH,
    MIN_LENGTH,
    PRODUCTION,
    Argon2Parameters,
    InlineHashRunner,
    PasswordHasher,
    PasswordPolicy,
    PasswordPolicyError,
    Passwords,
    PolicyProblem,
    ThreadHashRunner,
    load_blocklist,
)

SECRET = "correct horse battery staple"


# --- hashing -------------------------------------------------------------------------------------


def test_hashes_are_argon2id_with_the_given_parameters_and_a_fresh_salt() -> None:
    hasher = PasswordHasher(CHEAP)
    first, second = hasher.hash(SECRET), hasher.hash(SECRET)
    assert first.startswith("$argon2id$v=19$m=8,t=1,p=1$")
    assert first != second  # a new salt every time
    assert SECRET not in first
    assert hasher.verify(first, SECRET)
    assert hasher.verify(second, SECRET)


def test_the_production_parameters_are_owasps_first_configuration() -> None:
    assert (PRODUCTION.memory_cost_kib, PRODUCTION.time_cost, PRODUCTION.parallelism) == (
        19 * 1024,
        2,
        1,
    )
    assert PasswordHasher(PRODUCTION).hash(SECRET).startswith("$argon2id$v=19$m=19456,t=2,p=1$")


@pytest.mark.parametrize(
    "stored",
    ["", "not a hash", "$argon2id$v=19$m=8,t=1,p=1$garbage", "$2b$12$bcryptlookingthing"],
)
def test_a_wrong_password_or_an_unreadable_hash_is_false_not_an_error(stored: str) -> None:
    hasher = PasswordHasher(CHEAP)
    assert hasher.verify(hasher.hash(SECRET), "wrong password entirely") is False
    assert hasher.verify(stored, SECRET) is False


def test_a_hash_made_with_other_parameters_needs_rehashing() -> None:
    old = PasswordHasher(CHEAP).hash(SECRET)
    newer = PasswordHasher(Argon2Parameters(time_cost=2, memory_cost_kib=16))
    assert newer.needs_rehash(old)
    assert not newer.needs_rehash(newer.hash(SECRET))
    assert newer.verify(old, SECRET)  # still verifies, so it can be rehashed at the next login


def test_nothing_shows_the_password() -> None:
    passwords = Passwords(PasswordHasher(CHEAP), InlineHashRunner())
    assert SECRET not in repr(passwords)
    with pytest.raises(PasswordPolicyError) as excinfo:
        PasswordPolicy(frozenset()).check("short secret"[:11])
    assert "short secr" not in str(excinfo.value)


# --- the runners -------------------------------------------------------------------------------


async def test_inline_hashing_does_not_let_simulated_time_move() -> None:
    clock = SimClock()
    woke: list[float] = []

    async def sleeper() -> None:
        await clock.sleep_ms(10.0)
        woke.append(clock.now_ms())

    pending = asyncio.create_task(sleeper())
    await clock.sleep_ms(0.0)  # the sleeper is now waiting for t = 10 ms
    passwords = Passwords(PasswordHasher(PRODUCTION), InlineHashRunner())

    stored = await passwords.hash(SECRET)
    assert await passwords.verify(stored, SECRET)

    # Two full-cost argon2id operations ran, and simulated time did not move.
    assert clock.now_ms() == 0.0
    assert woke == []
    await pending
    assert woke == [10.0]


async def test_a_thread_would_let_simulated_time_jump_which_is_why_simclock_hashes_inline() -> None:
    clock = SimClock()
    woke = threading.Event()

    async def sleeper() -> None:
        await clock.sleep_ms(10.0)
        woke.set()

    pending = asyncio.create_task(sleeper())
    await clock.sleep_ms(0.0)
    runner = ThreadHashRunner(max_workers=1)

    def slow_work() -> bool:
        # Stands in for a hash: it can only finish once simulated time has moved on.
        return woke.wait(timeout=30)

    try:
        finished = await runner.run(slow_work)
    finally:
        runner.close()
    # While the thread worked the loop sat idle, so the SimClock jumped to the sleeper.
    assert finished is True
    assert clock.now_ms() == 10.0
    await pending


async def test_on_a_thread_the_event_loop_keeps_serving_while_hashing() -> None:
    clock = WallClock()
    runner = ThreadHashRunner()
    passwords = Passwords(PasswordHasher(PRODUCTION), runner)
    ticks = 0
    hashing = True

    async def ticker() -> None:
        nonlocal ticks
        while hashing:
            await clock.sleep_ms(1.0)
            ticks += 1

    task = asyncio.create_task(ticker())
    try:
        stored = await passwords.hash(SECRET)
        for _ in range(4):
            assert await passwords.verify(stored, SECRET)
    finally:
        hashing = False
        await task
        runner.close()
    # Five full-cost operations on the pool, and the loop ran other work meanwhile.
    assert ticks >= 1


async def test_the_pool_runs_at_most_max_workers_hashes_at_once() -> None:
    runner = ThreadHashRunner(max_workers=2)
    lock = threading.Lock()
    running, most = 0, 0
    pause = threading.Event()

    def work() -> None:
        nonlocal running, most
        with lock:
            running += 1
            most = max(most, running)
        pause.wait(0.05)
        with lock:
            running -= 1

    try:
        await asyncio.gather(*(runner.run(work) for _ in range(6)))
    finally:
        runner.close()
    assert most == 2
    with pytest.raises(ValueError, match="max_workers"):
        ThreadHashRunner(max_workers=0)


# --- policy ----------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def policy() -> PasswordPolicy:
    return PasswordPolicy(load_blocklist())


def test_length_bounds(policy: PasswordPolicy) -> None:
    for problem, password in (
        (PolicyProblem.TOO_SHORT, "x" * (MIN_LENGTH - 1)),
        (PolicyProblem.TOO_LONG, "x" * (MAX_LENGTH + 1)),
    ):
        with pytest.raises(PasswordPolicyError) as excinfo:
            policy.check(password)
        assert excinfo.value.problem is problem
    policy.check("q" * MIN_LENGTH)
    policy.check("q" * MAX_LENGTH)


def test_no_composition_rules(policy: PasswordPolicy) -> None:
    policy.check("a long passphrase of plain lower case words")
    policy.check("1234567890123")  # digits only, thirteen of them, not on the list


def test_the_blocklist_refuses_common_passwords_whatever_their_case(
    policy: PasswordPolicy,
) -> None:
    for password in ("unbelievable", "UNBELIEVABLE", "Unbelievable"):
        with pytest.raises(PasswordPolicyError) as excinfo:
            policy.check(password)
        assert excinfo.value.problem is PolicyProblem.TOO_COMMON
        assert password not in str(excinfo.value)


def test_the_bundled_blocklist_is_the_recorded_file() -> None:
    blocklist = load_blocklist()
    # SecLists 10k-most-common.txt at 913b3273 (docs/licenses.md): 10,001 entries, of which
    # only 10 are long enough to matter once the minimum length is 12.
    assert len(blocklist) == 10_001
    assert sum(1 for entry in blocklist if len(entry) >= MIN_LENGTH) == 10
    assert {"password", "123456", "qwerty"} <= blocklist
