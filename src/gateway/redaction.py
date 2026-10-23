"""Keep provider keys out of the logs: a redaction layer under every logger.

See docs/32-provider-settings-and-vault.md.

``Redactor`` holds the exact values to scrub while they are in use (a key just submitted to
the admin endpoint, a key leased for one request) and a fixed set of patterns that look like
credentials. ``install`` wraps the process's log-record factory, so every record, from any
logger (uvicorn's included, which do not propagate to the root), is scrubbed when it is created:
its message is formatted, scrubbed, and stored with no arguments left; an exception's text is
formatted and scrubbed too. The server factories install it; tests install and remove it.

It is a second line, not the first: keys are never logged on purpose (``Secret`` is redacted,
provider error bodies are never kept), and the leak scans check that.
"""

from __future__ import annotations

import logging
import re
import traceback
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from typing import Any

REDACTED = "[redacted]"
PATTERNS = (
    re.compile(r"(?i)(bearer\s+)\S+"),
    re.compile(r"(?i)(x-api-key[\"']?\s*[:=]\s*[\"']?)[^\s\"',]+"),
    re.compile(r"sk-ant-[A-Za-z0-9_-]{16,}"),
    re.compile(r"sk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"gwk_[0-9a-f]{16}_[A-Za-z0-9_-]{43}"),
)
# Exact values shorter than this are not scrubbed: they would match ordinary words.
MIN_EXACT = 8


class Redactor:
    def __init__(self) -> None:
        self._values: dict[str, int] = {}

    def __repr__(self) -> str:
        return f"Redactor(<{len(self._values)} values>)"

    @contextmanager
    def watching(self, value: str) -> Iterator[None]:
        """Scrub ``value`` from every record created while the block runs."""
        if len(value) < MIN_EXACT:
            yield
            return
        self._values[value] = self._values.get(value, 0) + 1
        try:
            yield
        finally:
            self._values[value] -= 1
            if not self._values[value]:
                del self._values[value]

    def scrub(self, text: str) -> str:
        for value in sorted(self._values, key=len, reverse=True):
            text = text.replace(value, REDACTED)
        for pattern in PATTERNS:
            text = pattern.sub(lambda m: (m.group(1) if m.groups() else "") + REDACTED, text)
        return text


Factory = Callable[..., logging.LogRecord]


def install(redactor: Redactor) -> Callable[[], None]:
    """Scrub every log record from now on; returns the function that removes it again."""
    previous: Factory = logging.getLogRecordFactory()

    def scrub_value(value: object) -> object:
        return redactor.scrub(value) if isinstance(value, str) else value

    def factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
        record = previous(*args, **kwargs)
        # The message and each string argument are scrubbed where they are, so formatters that
        # read the arguments themselves (uvicorn's access log) keep working.
        record.msg = scrub_value(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(scrub_value(a) for a in record.args)
        elif isinstance(record.args, Mapping):
            record.args = {k: scrub_value(v) for k, v in record.args.items()}
        try:
            message = record.getMessage()
        except Exception:  # a broken format string: keep what there is, scrubbed
            message = redactor.scrub(str(record.msg))
            record.msg, record.args = message, ()
        if redactor.scrub(message) != message:
            # Something still shows a secret (an argument's repr): give up the arguments.
            record.msg, record.args = redactor.scrub(message), ()
        if record.exc_info and record.exc_info[0] is not None:
            text = "".join(traceback.format_exception(*record.exc_info))
            record.exc_text = redactor.scrub(text)
            record.exc_info = None
        return record

    logging.setLogRecordFactory(factory)

    def remove() -> None:
        logging.setLogRecordFactory(previous)

    return remove
