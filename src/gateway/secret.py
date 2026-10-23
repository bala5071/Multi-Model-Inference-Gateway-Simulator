"""``Secret``: a credential that cannot be shown by accident.

See docs/31-chat-and-adapters.md and docs/provider-plan.md, section 3.

A provider key travels through the gateway as a ``Secret``. Its ``repr``, ``str`` and format
say ``Secret(<redacted>)``, so an f-string, a log argument, an exception message, a traceback
or a test assertion never shows the value. It cannot be pickled, copied into JSON or compared
by value. ``reveal()`` gives the value back; it is called in exactly one place, the adapters'
header builder (``gateway.adapters.http``), and a test fails on any other call.
"""

from __future__ import annotations

from typing import Any, NoReturn

REDACTED = "Secret(<redacted>)"


class Secret:
    """A credential. Hold it, pass it on, and reveal it only to put it in a request header."""

    __slots__ = ("__value",)

    def __init__(self, value: str) -> None:
        if not isinstance(value, str) or not value:
            raise ValueError("a secret is a non-empty string")
        self.__value = value

    def reveal(self) -> str:
        return self.__value

    def __repr__(self) -> str:
        return REDACTED

    __str__ = __repr__

    def __format__(self, spec: str) -> str:
        return REDACTED

    def __eq__(self, other: object) -> bool:
        return self is other

    def __hash__(self) -> int:
        return id(self)

    def __reduce__(self) -> NoReturn:
        raise TypeError("a Secret cannot be pickled")

    def __copy__(self) -> NoReturn:
        raise TypeError("a Secret is not copied")

    def __deepcopy__(self, memo: Any) -> NoReturn:
        raise TypeError("a Secret is not copied")
