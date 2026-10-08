"""Maps model names and aliases to the worker that serves them."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol

from gateway.schema import GenerateRequest, RequestResult, TokenCallback


class Worker(Protocol):
    async def submit(
        self, request: GenerateRequest, *, on_token: TokenCallback | None = None
    ) -> RequestResult:
        """Serve a request. ``on_token`` is called as each output token is produced."""
        ...

    async def aclose(self) -> None:
        """Stop the worker's scheduler task and cancel every request it still holds."""
        ...


class UnknownModelError(LookupError):
    """Raised when a model name is neither a registered name nor an alias."""

    def __init__(self, model: str, valid_names: Iterable[str]) -> None:
        self.model = model
        self.valid_names = tuple(valid_names)
        super().__init__(f"Unknown model {model!r}. Valid names: {', '.join(self.valid_names)}")


class ModelRegistry:
    def __init__(self) -> None:
        self._workers: dict[str, Worker] = {}
        self._aliases: dict[str, str] = {}

    def register(self, name: str, worker: Worker, *, aliases: Iterable[str] = ()) -> None:
        aliases = tuple(aliases)
        new_names = (name, *aliases)
        for new_name in new_names:
            if not new_name:
                raise ValueError("model names and aliases must not be empty")
            if new_name in self._workers or new_name in self._aliases:
                raise ValueError(f"{new_name!r} is already registered")
        if len(set(new_names)) != len(new_names):
            raise ValueError(f"duplicate names in registration of {name!r}")
        self._workers[name] = worker
        for alias in aliases:
            self._aliases[alias] = name

    @property
    def names(self) -> tuple[str, ...]:
        """Registered model names, without aliases, in registration order."""
        return tuple(self._workers)

    @property
    def valid_names(self) -> tuple[str, ...]:
        """Every name ``get`` accepts: model names first, then aliases."""
        return (*self._workers, *self._aliases)

    def aliases_of(self, name: str) -> tuple[str, ...]:
        """Aliases of a registered model name, in registration order."""
        if name not in self._workers:
            raise UnknownModelError(name, self.valid_names)
        return tuple(alias for alias, target in self._aliases.items() if target == name)

    def resolve(self, name: str) -> str:
        """Return the registered model name for a name or alias."""
        if name in self._workers:
            return name
        if name in self._aliases:
            return self._aliases[name]
        raise UnknownModelError(name, self.valid_names)

    def get(self, name: str) -> Worker:
        return self._workers[self.resolve(name)]

    async def submit(
        self, request: GenerateRequest, *, on_token: TokenCallback | None = None
    ) -> RequestResult:
        return await self.get(request.model).submit(request, on_token=on_token)

    async def aclose(self) -> None:
        for worker in self._workers.values():
            await worker.aclose()
