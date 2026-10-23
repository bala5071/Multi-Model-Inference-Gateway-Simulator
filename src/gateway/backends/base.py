"""The one interface every backend implements."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass

from gateway.chat import (
    UNKNOWN_CAPABILITIES,
    Capabilities,
    ChatDeltaCallback,
    ChatNotSupportedError,
    ChatRequest,
    ChatResult,
)
from gateway.schema import GenerateRequest
from gateway.secret import Secret

# Called by a backend that really generates, as each piece of output arrives:
# (index of the piece, its text). Backends that only simulate never call it.
BackendTokenCallback = Callable[[int, str], None]


@dataclass(frozen=True, slots=True)
class Generation:
    """What a model produced for one request, without any timing."""

    # Token ids of the prompt, for backends that expose them. The prefix cache needs them.
    prompt_tokens: tuple[int, ...]
    tokens_out: int
    # The generated text. Empty for backends that produce none.
    text: str = ""
    # For backends that report how many prompt tokens there were but not their ids.
    prompt_token_count: int | None = None

    def __post_init__(self) -> None:
        if self.tokens_out < 1:
            raise ValueError("tokens_out must be at least 1")
        if self.prompt_token_count is not None and self.prompt_token_count < 0:
            raise ValueError("prompt_token_count must not be negative")

    @property
    def tokens_in(self) -> int:
        if self.prompt_token_count is not None:
            return self.prompt_token_count
        return len(self.prompt_tokens)


class BackendError(Exception):
    """A backend could not produce a result. ``code`` says which way it failed.

    ``backend_unavailable``: it could not be reached at all.
    ``backend_error``: it was reached but answered badly or broke off.
    """

    def __init__(self, code: str, backend: str, url: str, detail: str) -> None:
        self.code = code
        self.backend = backend
        self.url = url
        self.detail = detail
        super().__init__(f"Model backend {backend!r} at {url}: {detail}")


class Backend(ABC):
    """Produces a model's output for a request.

    ``SimulatedBackend`` returns at once and says only how many tokens there would be;
    the worker in front of it decides when they appear. A backend that runs a real model
    takes as long as the model takes and reports each piece through ``on_token``.
    """

    @abstractmethod
    async def generate(
        self, request: GenerateRequest, *, on_token: BackendTokenCallback | None = None
    ) -> Generation: ...

    async def aclose(self) -> None:
        """Release anything the backend holds, such as network connections."""
        return None

    async def chat(
        self,
        request: ChatRequest,
        *,
        credential: Secret | None = None,
        on_delta: ChatDeltaCallback | None = None,
    ) -> ChatResult:
        """Answer a chat request in the common format (docs/31). ``credential`` is passed for
        each call, never held by the backend. Backends that only take a prompt do not speak
        it."""
        raise ChatNotSupportedError(type(self).__name__)

    def capabilities(self) -> Capabilities:
        """What the configured model can do; unknown unless the backend knows better."""
        return UNKNOWN_CAPABILITIES
