"""argon2id hashing, the two hash runners, and the password policy.

See docs/24-password-hashing.md.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

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
    Verification,
    load_blocklist,
    parameters_from,
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


def test_the_production_parameters_are_64_mib_3_iterations_1_lane() -> None:
    # The setting accepted in docs/24. The one full-cost hash in the suite: it shows the
    # parameters reach the stored hash.
    assert (PRODUCTION.memory_cost_kib, PRODUCTION.time_cost, PRODUCTION.parallelism) == (
        64 * 1024,
        3,
        1,
    )
    assert PasswordHasher(PRODUCTION).hash(SECRET).startswith("$argon2id$v=19$m=65536,t=3,p=1$")


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
    passwords = Passwords(PasswordHasher(CHEAP), InlineHashRunner())

    stored = await passwords.hash(SECRET)
    assert await passwords.verify(stored, SECRET)

    # Two argon2id operations ran, and simulated time did not move. How long they take does
    # not matter: there is no await inside, so the loop never looks idle.
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
    hasher = PasswordHasher(CHEAP)
    served = threading.Event()

    def hash_once_the_loop_has_served() -> str:
        # Stands in for a slow hash: it can only finish once the loop has run another task.
        assert served.wait(timeout=30)
        return hasher.hash(SECRET)

    async def other_request() -> None:
        await clock.sleep_ms(1.0)
        served.set()

    task = asyncio.create_task(other_request())
    try:
        stored = await runner.run(hash_once_the_loop_has_served)
    finally:
        await task
        runner.close()
    # The hash finished, so the loop served the other task while the thread worked.
    assert hasher.verify(stored, SECRET)


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
    policy.check("v" * MIN_LENGTH)
    policy.check("v" * MAX_LENGTH)


def test_no_composition_rules(policy: PasswordPolicy) -> None:
    # The list itself has "qqqqqqqqqqqq" and "1234567890123": these are not on it.
    policy.check("a long passphrase of plain lower case words")
    policy.check("7391046285317")  # digits only, thirteen of them, not on the list


def test_the_blocklist_refuses_common_passwords_whatever_their_case(
    policy: PasswordPolicy,
) -> None:
    for password in ("q1w2e3r4t5y6", "Q1W2E3R4T5Y6", "1qaz2wsx3edc"):
        with pytest.raises(PasswordPolicyError) as excinfo:
            policy.check(password)
        assert excinfo.value.problem is PolicyProblem.TOO_COMMON
        assert password not in str(excinfo.value)


def test_the_bundled_blocklist_is_the_recorded_file() -> None:
    data = Path(__file__).resolve().parent.parent / "src" / "gateway" / "data"
    lines = (data / "common-passwords.txt").read_text(encoding="utf-8").splitlines()
    blocklist = load_blocklist()
    # SecLists 100k-most-used-passwords-NCSC.txt at 1a7bb912 (docs/licenses.md): 99,839
    # entries, of which the 1,212 of 12 or more characters are stored; 1,197 once case is
    # ignored. Every one is long enough to pass the length rule, so every one refuses
    # something the length rule would not.
    assert len(lines) == 1212
    assert all(len(line) >= MIN_LENGTH for line in lines)
    assert len(blocklist) == 1197


@pytest.mark.parametrize(
    ("username", "password", "refused"),
    [
        ("alice", "alice likes long walks", True),
        ("alice", "my friend ALICE is here", True),
        ("Alice", "nothing personal at all", False),
        ("al", "always a long passphrase", False),  # too short a username to check
        (None, "alice likes long walks", False),
    ],
)
def test_a_password_may_not_contain_the_username(
    policy: PasswordPolicy, username: str | None, password: str, refused: bool
) -> None:
    if not refused:
        policy.check(password, username)
        return
    with pytest.raises(PasswordPolicyError) as excinfo:
        policy.check(password, username)
    assert excinfo.value.problem is PolicyProblem.CONTAINS_USERNAME
    assert "alice" not in str(excinfo.value).lower()


@pytest.mark.parametrize("password", ["my gateway password", "MyGATEWAYsecret!", "xgatewayx12"])
def test_a_password_may_not_contain_the_product_name(policy: PasswordPolicy, password: str) -> None:
    with pytest.raises(PasswordPolicyError) as excinfo:
        policy.check(password + "padding", "someone")
    assert excinfo.value.problem is PolicyProblem.CONTAINS_PRODUCT_NAME


def test_each_refusal_says_which_rule_failed_and_never_shows_the_list(
    policy: PasswordPolicy,
) -> None:
    cases = {
        PolicyProblem.TOO_SHORT: ("short", None, "at least 12 characters"),
        PolicyProblem.TOO_LONG: ("v" * (MAX_LENGTH + 1), None, "at most 1024 characters"),
        PolicyProblem.TOO_COMMON: ("q1w2e3r4t5y6", None, "list of very common passwords"),
        PolicyProblem.CONTAINS_USERNAME: ("alice likes long walks", "alice", "the username"),
        PolicyProblem.CONTAINS_PRODUCT_NAME: ("my gateway password", None, "product's name"),
    }
    assert set(cases) == set(PolicyProblem)
    messages = set()
    for problem, (password, username, says) in cases.items():
        with pytest.raises(PasswordPolicyError) as excinfo:
            policy.check(password, username)
        message = str(excinfo.value)
        assert excinfo.value.problem is problem
        assert says in message
        assert not any(entry in message.casefold() for entry in policy.blocklist)
        messages.add(message)
    assert len(messages) == len(cases)  # one message per rule, so the caller can tell them apart


# --- configurable parameters and upgrades ------------------------------------------------


def test_parameters_come_from_the_environment_where_set() -> None:
    default = PRODUCTION
    assert parameters_from({}, default) == default
    assert parameters_from(
        {
            "GATEWAY_ARGON2_MEMORY_KIB": "65536",
            "GATEWAY_ARGON2_TIME_COST": "3",
            "GATEWAY_ARGON2_PARALLELISM": "1",
        },
        default,
    ) == Argon2Parameters(time_cost=3, memory_cost_kib=65536, parallelism=1)
    for bad in (
        {"GATEWAY_ARGON2_TIME_COST": "0"},
        {"GATEWAY_ARGON2_MEMORY_KIB": "lots"},
        {"GATEWAY_ARGON2_MEMORY_KIB": "8", "GATEWAY_ARGON2_PARALLELISM": "4"},
    ):
        with pytest.raises(ValueError, match="GATEWAY_ARGON2"):
            parameters_from(bad, default)


async def test_an_old_hash_still_verifies_and_is_upgraded_to_the_current_setting() -> None:
    old = PasswordHasher(CHEAP).hash(SECRET)
    current = Argon2Parameters(time_cost=2, memory_cost_kib=64)
    passwords = Passwords(PasswordHasher(current), InlineHashRunner())

    upgraded = await passwords.check(old, SECRET)
    assert upgraded.valid
    assert upgraded.upgraded_hash is not None
    assert upgraded.upgraded_hash.startswith("$argon2id$v=19$m=64,t=2,p=1$")
    assert SECRET not in repr(upgraded)
    # The upgraded hash verifies, and needs no further upgrade.
    again = await passwords.check(upgraded.upgraded_hash, SECRET)
    assert (again.valid, again.upgraded_hash) == (True, None)
    # A wrong password is never upgraded.
    assert await passwords.check(old, "not the password at all") == Verification(False)
