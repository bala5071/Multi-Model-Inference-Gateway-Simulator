"""Password hashing with argon2id, how it runs without blocking or moving simulated time,
and the password policy.

See docs/24-password-hashing.md and docs/identity-plan.md (sections 2 and 4).

Hashing is deliberately slow. Run on the event loop it would stall every other request; run
on a thread it would leave the loop idle, and a ``SimClock`` moves time forward whenever the
loop is idle. So a ``Passwords`` object hashes through an injected ``HashRunner``:

- ``ThreadHashRunner`` (the real server): a small dedicated thread pool. argon2-cffi releases
  the GIL while it hashes, so the loop keeps serving, and the pool bounds the CPU a burst of
  logins can take.
- ``InlineHashRunner`` (``SimClock``: tests, simulated runs): hashes synchronously, with no
  ``await`` inside, so the loop never looks idle and simulated time cannot move.

A password never appears in an exception message, a ``repr`` or a log.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from enum import StrEnum
from importlib import resources
from typing import Protocol, TypeVar

import argon2
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

T = TypeVar("T")

MIN_LENGTH = 12
# Bounds the hashing cost a caller can ask for; counted in characters.
MAX_LENGTH = 1024
BLOCKLIST_RESOURCE = "data/common-passwords.txt"
# A password may not contain these, whatever the case (nor the username).
PRODUCT_NAMES = ("gateway",)
# The username rule applies to usernames at least this long: refusing every password that
# contains "al" would refuse far too many good ones.
MIN_USERNAME_TO_CHECK = 3
MEMORY_ENV = "GATEWAY_ARGON2_MEMORY_KIB"
TIME_COST_ENV = "GATEWAY_ARGON2_TIME_COST"
PARALLELISM_ENV = "GATEWAY_ARGON2_PARALLELISM"


@dataclass(frozen=True, slots=True)
class Argon2Parameters:
    """argon2id cost parameters. The defaults are RFC 9106's second recommended setting with
    one lane (64 MiB of memory, 3 iterations); docs/24 records what they cost here and why
    they were chosen. ``GATEWAY_ARGON2_*`` overrides them (``parameters_from``)."""

    time_cost: int = 3
    memory_cost_kib: int = 64 * 1024
    parallelism: int = 1
    hash_len: int = 32
    salt_len: int = 16

    def __post_init__(self) -> None:
        if self.time_cost < 1 or self.parallelism < 1:
            raise ValueError("time cost and parallelism must be at least 1")
        if self.memory_cost_kib < 8 * self.parallelism:
            raise ValueError("argon2 needs at least 8 KiB of memory per lane")


def parameters_from(environ: Mapping[str, str], default: Argon2Parameters) -> Argon2Parameters:
    """``default``, with memory, iterations and lanes replaced by ``GATEWAY_ARGON2_*`` where
    set. The wiring reads the environment; this only parses it."""
    try:
        return Argon2Parameters(
            time_cost=int(environ.get(TIME_COST_ENV, default.time_cost)),
            memory_cost_kib=int(environ.get(MEMORY_ENV, default.memory_cost_kib)),
            parallelism=int(environ.get(PARALLELISM_ENV, default.parallelism)),
            hash_len=default.hash_len,
            salt_len=default.salt_len,
        )
    except ValueError as exc:
        raise ValueError(
            f"invalid argon2 setting ({MEMORY_ENV}, {TIME_COST_ENV}, {PARALLELISM_ENV}): {exc}"
        ) from None


PRODUCTION = Argon2Parameters()
# For tests only: the same algorithm at the lowest cost argon2 allows, so suites stay fast.
CHEAP = Argon2Parameters(time_cost=1, memory_cost_kib=8, parallelism=1)


class PasswordHasher:
    """argon2id hashes in the standard PHC string form (``$argon2id$v=19$m=...``), with a
    random salt per hash. Verification is argon2-cffi's, which compares in constant time."""

    def __init__(self, parameters: Argon2Parameters = PRODUCTION) -> None:
        self.parameters = parameters
        self._hasher = argon2.PasswordHasher(
            time_cost=parameters.time_cost,
            memory_cost=parameters.memory_cost_kib,
            parallelism=parameters.parallelism,
            hash_len=parameters.hash_len,
            salt_len=parameters.salt_len,
            type=argon2.Type.ID,
        )

    def __repr__(self) -> str:
        return f"PasswordHasher({self.parameters!r})"

    def hash(self, password: str) -> str:
        return self._hasher.hash(password)

    def verify(self, stored_hash: str, password: str) -> bool:
        """True if ``password`` matches; False for a mismatch or a hash it cannot read."""
        try:
            return self._hasher.verify(stored_hash, password)
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return False

    def needs_rehash(self, stored_hash: str) -> bool:
        """True if the hash was made with other parameters than these."""
        try:
            return self._hasher.check_needs_rehash(stored_hash)
        except InvalidHashError:
            return True


class HashRunner(Protocol):
    async def run(self, work: Callable[[], T]) -> T:
        """Run ``work`` (a hash or a verify) and return its result."""
        ...


class InlineHashRunner:
    """Runs the work synchronously, on the event loop, with no await point inside.

    For ``SimClock``: the loop is busy for the whole hash, so simulated time cannot move
    while it runs. It blocks every other task for that time, which is what a simulation
    wants and what a real server must not do.
    """

    async def run(self, work: Callable[[], T]) -> T:
        return work()


class ThreadHashRunner:
    """Runs the work on a small dedicated thread pool, for the ``WallClock`` server.

    argon2-cffi releases the GIL while hashing, so the event loop keeps serving requests.
    ``max_workers`` bounds how many hashes run at once; more wait their turn.
    """

    def __init__(self, max_workers: int = 2) -> None:
        if max_workers < 1:
            raise ValueError("max_workers must be at least 1")
        self.max_workers = max_workers
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="argon2")

    async def run(self, work: Callable[[], T]) -> T:
        return await asyncio.get_running_loop().run_in_executor(self._pool, work)

    def close(self) -> None:
        self._pool.shutdown(wait=True)


@dataclass(frozen=True, slots=True)
class Verification:
    """The result of checking a password. ``upgraded_hash`` is set when the password was
    right but the stored hash was made with other parameters: store it in place of the old
    one. ``repr`` never shows it."""

    valid: bool
    upgraded_hash: str | None = None

    def __repr__(self) -> str:
        upgrade = "<new hash>" if self.upgraded_hash is not None else "None"
        return f"Verification(valid={self.valid}, upgraded_hash={upgrade})"


class Passwords:
    """Hashing and verifying through a runner: the one way the gateway touches passwords."""

    def __init__(self, hasher: PasswordHasher, runner: HashRunner) -> None:
        self.hasher = hasher
        self._runner = runner

    def __repr__(self) -> str:
        return f"Passwords({self.hasher!r}, {type(self._runner).__name__})"

    async def hash(self, password: str) -> str:
        return await self._runner.run(lambda: self.hasher.hash(password))

    async def verify(self, stored_hash: str, password: str) -> bool:
        return await self._runner.run(lambda: self.hasher.verify(stored_hash, password))

    async def check(self, stored_hash: str, password: str) -> Verification:
        """Verify, and if the password is right and the stored hash used other parameters
        (argon2 keeps them in the hash), re-hash it with the current ones."""

        def work() -> Verification:
            if not self.hasher.verify(stored_hash, password):
                return Verification(False)
            if self.hasher.needs_rehash(stored_hash):
                return Verification(True, self.hasher.hash(password))
            return Verification(True)

        return await self._runner.run(work)


# --- policy ------------------------------------------------------------------------------------


class PolicyProblem(StrEnum):
    TOO_SHORT = "too_short"
    TOO_LONG = "too_long"
    TOO_COMMON = "too_common"
    CONTAINS_USERNAME = "contains_username"
    CONTAINS_PRODUCT_NAME = "contains_product_name"


_MESSAGES = {
    PolicyProblem.TOO_SHORT: f"A password needs at least {MIN_LENGTH} characters.",
    PolicyProblem.TOO_LONG: f"A password can have at most {MAX_LENGTH} characters.",
    PolicyProblem.TOO_COMMON: "That password is on the list of very common passwords.",
    PolicyProblem.CONTAINS_USERNAME: "A password may not contain the username.",
    PolicyProblem.CONTAINS_PRODUCT_NAME: "A password may not contain the product's name.",
}


class PasswordPolicyError(ValueError):
    """The password breaks the policy. The message says which rule, never the password."""

    def __init__(self, problem: PolicyProblem) -> None:
        self.problem = problem
        super().__init__(_MESSAGES[problem])


def load_blocklist() -> frozenset[str]:
    """The bundled list of very common passwords, case-folded (docs/licenses.md)."""
    text = resources.files("gateway").joinpath(BLOCKLIST_RESOURCE).read_text(encoding="utf-8")
    return frozenset(line.strip().casefold() for line in text.splitlines() if line.strip())


@dataclass(frozen=True, slots=True)
class PasswordPolicy:
    """At least ``min_length`` and at most ``max_length`` characters; not on the blocklist;
    not containing the username (of at least ``MIN_USERNAME_TO_CHECK`` characters) or the
    product's name. Every comparison ignores case. No composition rules: a long passphrase of
    lower-case words is fine."""

    blocklist: frozenset[str]
    min_length: int = MIN_LENGTH
    max_length: int = MAX_LENGTH
    product_names: tuple[str, ...] = PRODUCT_NAMES

    def check(self, password: str, username: str | None = None) -> None:
        """Raise ``PasswordPolicyError`` for the first rule the password breaks."""
        if len(password) < self.min_length:
            raise PasswordPolicyError(PolicyProblem.TOO_SHORT)
        if len(password) > self.max_length:
            raise PasswordPolicyError(PolicyProblem.TOO_LONG)
        folded = password.casefold()
        if folded in self.blocklist:
            raise PasswordPolicyError(PolicyProblem.TOO_COMMON)
        checked_name = username is not None and len(username) >= MIN_USERNAME_TO_CHECK
        if checked_name and username is not None and username.casefold() in folded:
            raise PasswordPolicyError(PolicyProblem.CONTAINS_USERNAME)
        if any(name.casefold() in folded for name in self.product_names):
            raise PasswordPolicyError(PolicyProblem.CONTAINS_PRODUCT_NAME)
