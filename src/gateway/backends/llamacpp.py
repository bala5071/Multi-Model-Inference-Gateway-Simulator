"""A backend that calls a local llama.cpp ``llama-server`` over its OpenAI-compatible API.

Works with anything that speaks the same streaming protocol, including Ollama.
See docs/12-real-models.md.
"""

from __future__ import annotations

import json
from typing import Any, Literal

import httpx

from gateway.backends.base import Backend, BackendError, BackendTokenCallback, Generation
from gateway.schema import GenerateRequest

Endpoint = Literal["completions", "chat"]


class LlamaCppBackend(Backend):
    def __init__(
        self,
        *,
        name: str,
        base_url: str,
        model: str | None = None,
        endpoint: Endpoint = "completions",
        client: httpx.AsyncClient | None = None,
        connect_timeout_s: float = 5.0,
    ) -> None:
        """``name`` identifies the backend in error messages.

        ``model`` is sent as the request's ``model`` field. llama-server serves one model
        and ignores it; Ollama needs it. ``client`` is for tests; by default the backend
        makes and owns one, with no limit on how long a response may take (the gateway's
        own request timeout covers that).
        """
        self._name = name
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._endpoint: Endpoint = endpoint
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(None, connect=connect_timeout_s)
        )

    @property
    def url(self) -> str:
        path = "/v1/chat/completions" if self._endpoint == "chat" else "/v1/completions"
        return self._base_url + path

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _payload(self, request: GenerateRequest) -> dict[str, Any]:
        payload: dict[str, Any] = {"stream": True, "stream_options": {"include_usage": True}}
        if self._endpoint == "chat":
            payload["messages"] = [{"role": "user", "content": request.prompt}]
        else:
            payload["prompt"] = request.prompt
        if self._model is not None:
            payload["model"] = self._model
        if request.max_output_tokens is not None:
            payload["max_tokens"] = request.max_output_tokens
        return payload

    def _error(self, code: str, detail: str) -> BackendError:
        return BackendError(code, self._name, self._base_url, detail)

    def _text_of(self, chunk: dict[str, Any]) -> str:
        choices = chunk.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            return ""  # for example the final chunk that carries only the usage
        if self._endpoint == "chat":
            delta = choices[0].get("delta")
            text = delta.get("content") if isinstance(delta, dict) else None
        else:
            text = choices[0].get("text")
        return text if isinstance(text, str) else ""

    async def generate(
        self, request: GenerateRequest, *, on_token: BackendTokenCallback | None = None
    ) -> Generation:
        pieces: list[str] = []
        usage: dict[str, Any] = {}
        try:
            async with self._client.stream(
                "POST", self.url, json=self._payload(request)
            ) as response:
                if response.status_code != 200:
                    body = (await response.aread()).decode(errors="replace").strip()
                    raise self._error(
                        "backend_error",
                        f"answered HTTP {response.status_code}"
                        + (f": {body[:200]}" if body else "."),
                    )
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line.removeprefix("data:").strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError as exc:
                        raise self._error(
                            "backend_error", "sent a stream chunk that is not valid JSON."
                        ) from exc
                    if not isinstance(chunk, dict):
                        continue
                    if isinstance(chunk.get("usage"), dict):
                        usage = chunk["usage"]
                    text = self._text_of(chunk)
                    if text:
                        if on_token is not None:
                            on_token(len(pieces), text)
                        pieces.append(text)
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise self._error(
                "backend_unavailable",
                "could not connect. Is the model server running? See docs/12-real-models.md.",
            ) from exc
        except httpx.TransportError as exc:
            raise self._error(
                "backend_error", f"the connection failed mid-response ({type(exc).__name__})."
            ) from exc

        # Some servers (Ollama) attach an all-zero usage to every chunk and the real one
        # only to the last, so a usage only counts if it reports some output.
        reported_out = usage.get("completion_tokens")
        reported_in = usage.get("prompt_tokens")
        has_usage = isinstance(reported_out, int) and reported_out > 0
        if has_usage and isinstance(reported_in, int) and reported_in >= 0:
            tokens_out, tokens_in = reported_out, reported_in
        else:
            # No usage reported. One streamed piece is, almost always, one token; for the
            # prompt, fall back to the simulator's one-token-per-word estimate.
            tokens_out = len(pieces)
            tokens_in = len(request.prompt.split())
        if not tokens_out:
            raise self._error("backend_error", "returned an empty completion.")
        return Generation(
            prompt_tokens=(),
            tokens_out=tokens_out,
            text="".join(pieces),
            prompt_token_count=tokens_in,
        )
