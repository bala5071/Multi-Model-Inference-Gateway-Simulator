"""Tests that never finish, for tests/test_suite_timeout.py. Not collected by the suite (the
file name does not start with ``test_``); that test runs them by name with a short limit."""

from __future__ import annotations

import asyncio
import threading


async def test_waits_forever_on_the_event_loop() -> None:
    await asyncio.Event().wait()


def test_waits_forever_in_a_blocking_call() -> None:
    threading.Event().wait()
