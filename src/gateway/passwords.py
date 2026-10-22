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
from collections.abc import Callable
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


@dataclass(frozen=True, slots=True)
class Argon2Parameters:
    """argon2id cost parameters. The defaults are OWASP's first recommended configuration
    (19 MiB of memory, 2 iterations, 1 lane); docs/24 records what they cost here."""

    time_cost: int = 2
    memory_cost_kib: int = 19 * 1024
    parallelism: int = 1
    hash_len: int = 32
    salt_len: int = 16


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


# --- policy ------------------------------------------------------------------------------------


class PolicyProblem(StrEnum):
    TOO_SHORT = "too_short"
    TOO_LONG = "too_long"
    TOO_COMMON = "too_common"


_MESSAGES = {
    PolicyProblem.TOO_SHORT: f"A password needs at least {MIN_LENGTH} characters.",
    PolicyProblem.TOO_LONG: f"A password can have at most {MAX_LENGTH} characters.",
    PolicyProblem.TOO_COMMON: "That password is on the list of very common passwords.",
}


class PasswordPolicyError(ValueError):
    """The password breaks the policy. The message says which rule, never the password."""

    def __init__(self, problem: PolicyProblem) -> None:
        self.problem = problem
        super().__init__(_MESSAGES[problem])


def load_blocklist() -> frozenset[str]:
    """The bundled list of very common passwords, lower-cased (docs/licenses.md)."""
    text = resources.files("gateway").joinpath(BLOCKLIST_RESOURCE).read_text(encoding="utf-8")
    return frozenset(line.strip().lower() for line in text.splitlines() if line.strip())


@dataclass(frozen=True, slots=True)
class PasswordPolicy:
    """At least ``min_length`` and at most ``max_length`` characters, and not on the
    blocklist (compared without case). No composition rules: a long passphrase of lower-case
    words is fine."""

    blocklist: frozenset[str]
    min_length: int = MIN_LENGTH
    max_length: int = MAX_LENGTH

    def check(self, password: str) -> None:
        """Raise ``PasswordPolicyError`` for the first rule the password breaks."""
        if len(password) < self.min_length:
            raise PasswordPolicyError(PolicyProblem.TOO_SHORT)
        if len(password) > self.max_length:
            raise PasswordPolicyError(PolicyProblem.TOO_LONG)
        if password.lower() in self.blocklist:
            raise PasswordPolicyError(PolicyProblem.TOO_COMMON)
