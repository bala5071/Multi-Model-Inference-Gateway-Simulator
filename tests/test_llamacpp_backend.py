"""LlamaCppBackend and PassthroughWorker against a mock llama-server."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from gateway.backends import Backend, BackendError, LlamaCppBackend
from gateway.clock import SimClock
from gateway.cost_model import estimate_cost
from gateway.overload import QueueFullError, RequestTimeoutError, run_with_timeout
from gateway.passthrough import PassthroughWorker
from gateway.profiles import ECONOMY
from gateway.schema import GenerateRequest
from mock_llama import MockLlamaServer, sse

URL = "http://127.0.0.1:8081"


def _backend(server: MockLlamaServer, **kwargs: object) -> LlamaCppBackend:
    return LlamaCppBackend(name="economy", base_url=URL, client=server.client(), **kwargs)  # type: ignore[arg-type]


def _request(
    prompt: str = "Say hello to the world", max_tokens: int | None = None
) -> GenerateRequest:
    return GenerateRequest("r1", "economy", prompt, max_tokens)


# --- the backend ---------------------------------------------------------------------------


def test_llamacpp_backend_is_a_backend() -> None:
    assert isinstance(_backend(MockLlamaServer(SimClock())), Backend)


async def test_completions_stream_is_parsed_into_pieces_text_and_usage() -> None:
    server = MockLlamaServer(SimClock())
    seen: list[tuple[int, str]] = []

    generation = await _backend(server).generate(
        _request(), on_token=lambda index, text: seen.append((index, text))
    )

    assert seen == [(0, "Hello"), (1, " world"), (2, "!")]
    assert generation.text == "Hello world!"
    assert (generation.tokens_in, generation.tokens_out) == (7, 3)
    assert generation.prompt_tokens == ()  # the server reports a count, not ids
    (request,) = server.requests
    assert (request.method, str(request.url)) == ("POST", f"{URL}/v1/completions")
    assert server.bodies == [
        {
            "prompt": "Say hello to the world",
            "stream": True,
            "stream_options": {"include_usage": True},
        }
    ]


async def test_max_tokens_and_model_are_sent_when_set() -> None:
    server = MockLlamaServer(SimClock())
    await _backend(server, model="llama3.2:1b").generate(_request(max_tokens=32))
    assert server.bodies[0]["max_tokens"] == 32
    assert server.bodies[0]["model"] == "llama3.2:1b"


async def test_chat_endpoint_sends_messages_and_reads_deltas() -> None:
    server = MockLlamaServer(SimClock())
    seen: list[str] = []

    generation = await _backend(server, endpoint="chat").generate(
        _request(), on_token=lambda _, text: seen.append(text)
    )

    assert str(server.requests[0].url) == f"{URL}/v1/chat/completions"
    assert server.bodies[0]["messages"] == [{"role": "user", "content": "Say hello to the world"}]
    assert "prompt" not in server.bodies[0]
    # The role-only opening delta is not a token.
    assert seen == ["Hello", " world", "!"]
    assert generation.text == "Hello world!"


async def test_without_usage_tokens_are_counted_from_the_stream_and_the_prompt() -> None:
    server = MockLlamaServer(SimClock(), report_usage=False)
    generation = await _backend(server).generate(_request("one two three four"))
    assert generation.tokens_out == 3  # one per streamed piece
    assert generation.tokens_in == 4  # one per word: an estimate, not the model's count


async def test_all_zero_usage_on_every_chunk_is_not_taken_as_the_real_usage() -> None:
    # What Ollama sends: a zeroed usage on each piece, the real one on a final empty chunk.
    zero = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    real = {"prompt_tokens": 72, "completion_tokens": 2, "total_tokens": 74}
    pieces = sse({"choices": [{"index": 0, "text": "Hi"}], "usage": zero}) + sse(
        {"choices": [{"index": 0, "text": " there"}], "usage": zero}
    )

    async def generate(body: bytes) -> tuple[int, int]:
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, content=body))
        )
        backend = LlamaCppBackend(name="economy", base_url=URL, client=client)
        generation = await backend.generate(_request("one two three"))
        return generation.tokens_in, generation.tokens_out

    with_final = pieces + sse({"choices": [], "usage": real}) + sse("[DONE]")
    assert await generate(with_final) == (72, 2)
    # If the final chunk never comes, the zeros must not be reported as "0 prompt tokens".
    assert await generate(pieces + sse("[DONE]")) == (3, 2)


async def test_base_url_may_end_with_a_slash() -> None:
    server = MockLlamaServer(SimClock())
    backend = LlamaCppBackend(name="economy", base_url=URL + "/", client=server.client())
    await backend.generate(_request())
    assert str(server.requests[0].url) == f"{URL}/v1/completions"


async def test_server_that_refuses_connections_is_reported_as_unavailable() -> None:
    server = MockLlamaServer(SimClock(), down={8081})

    with pytest.raises(BackendError) as excinfo:
        await _backend(server).generate(_request())

    error = excinfo.value
    assert (error.code, error.backend, error.url) == ("backend_unavailable", "economy", URL)
    assert str(error) == (
        "Model backend 'economy' at http://127.0.0.1:8081: could not connect. "
        "Is the model server running? See docs/12-real-models.md."
    )


async def test_http_error_from_the_server_is_reported_with_its_status_and_body() -> None:
    server = MockLlamaServer(SimClock(), status={8081: 503})

    with pytest.raises(BackendError) as excinfo:
        await _backend(server).generate(_request())

    assert excinfo.value.code == "backend_error"
    assert "answered HTTP 503" in str(excinfo.value)
    assert "Loading model" in str(excinfo.value)


async def test_connection_dropped_mid_response_is_a_backend_error() -> None:
    server = MockLlamaServer(SimClock(), drop_after=2)
    seen: list[str] = []

    with pytest.raises(BackendError) as excinfo:
        await _backend(server).generate(_request(), on_token=lambda _, text: seen.append(text))

    assert seen == ["Hello", " world"]  # what arrived before the break was passed on
    assert excinfo.value.code == "backend_error"
    assert "failed mid-response (ReadError)" in str(excinfo.value)


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (b"data: {not json\n\n", "not valid JSON"),
        (sse("[DONE]"), "empty completion"),
        (sse({"choices": [{"index": 0, "text": ""}]}) + sse("[DONE]"), "empty completion"),
    ],
    ids=["malformed-chunk", "no-chunks", "only-empty-text"],
)
async def test_unusable_streams_are_backend_errors(body: bytes, message: str) -> None:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=body))
    )
    backend = LlamaCppBackend(name="economy", base_url=URL, client=client)

    with pytest.raises(BackendError) as excinfo:
        await backend.generate(_request())

    assert excinfo.value.code == "backend_error"
    assert message in str(excinfo.value)


async def test_odd_but_harmless_stream_lines_are_ignored() -> None:
    body = (
        b": comment\n\n"
        b"event: ping\n\n"
        + sse({"object": "something else"})
        + sse({"choices": []})
        + sse({"choices": [{"index": 0, "text": "ok"}]})
        + b"data: [1, 2]\n\n"
        + sse("[DONE]")
        + sse({"choices": [{"index": 0, "text": "after the end"}]})
    )
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=body))
    )
    generation = await LlamaCppBackend(name="economy", base_url=URL, client=client).generate(
        _request()
    )
    assert (generation.text, generation.tokens_out) == ("ok", 1)


async def test_backend_closes_only_a_client_it_created() -> None:
    server = MockLlamaServer(SimClock())
    shared = server.client()
    await LlamaCppBackend(name="economy", base_url=URL, client=shared).aclose()
    assert not shared.is_closed

    owned = LlamaCppBackend(name="economy", base_url=URL)
    await owned.aclose()
    assert owned._client.is_closed


# --- the pass-through worker ---------------------------------------------------------------


async def test_worker_measures_ttft_and_total_latency_from_the_stream() -> None:
    clock = SimClock(start_ms=1000.0)
    server = MockLlamaServer(clock, first_token_delay_ms=50.0, token_gap_ms=10.0)
    worker = PassthroughWorker(ECONOMY, _backend(server), clock)
    streamed: list[tuple[int, float, str]] = []

    result = await worker.submit(
        _request(), on_token=lambda index, at_ms, text: streamed.append((index, at_ms, text))
    )

    # The first piece arrives 50 ms after the request, then one every 10 ms.
    assert result.token_times_ms == (1050.0, 1060.0, 1070.0)
    assert streamed == [(0, 1050.0, "Hello"), (1, 1060.0, " world"), (2, 1070.0, "!")]
    assert (result.enqueue_ms, result.start_ms) == (1000.0, 1000.0)
    assert (result.first_token_ms, result.finish_ms) == (1050.0, 1070.0)
    assert (result.time_to_first_token_ms, result.latency_ms) == (50.0, 70.0)
    assert result.queue_wait_ms == 0.0
    assert result.inter_token_ms == (10.0, 10.0)
    assert (result.tokens_in, result.tokens_out, result.cached_tokens) == (7, 3, 0)
    assert result.text == "Hello world!"
    assert result.model == "llama-3.2-1b"
    assert (
        result.cost_usd
        == estimate_cost(ECONOMY, new_prefill_tokens=7, output_tokens=3, batch_size=1).cost_usd
    )


async def test_worker_does_not_queue_or_batch_requests_itself() -> None:
    clock = SimClock()
    server = MockLlamaServer(clock)
    worker = PassthroughWorker(ECONOMY, _backend(server), clock)

    results = await asyncio.gather(*(worker.submit(_request()) for _ in range(5)))

    # All five were sent at once and each got its first piece after the server's 50 ms;
    # nothing in the gateway made one wait for another.
    assert len(server.requests) == 5
    assert {r.time_to_first_token_ms for r in results} == {50.0}
    assert clock.now_ms() == 70.0
    assert worker.waiting == 0
    assert worker.in_flight == 0


async def test_worker_refuses_requests_beyond_its_in_flight_limit() -> None:
    clock = SimClock()
    server = MockLlamaServer(clock)
    worker = PassthroughWorker(ECONOMY, _backend(server), clock, max_in_flight=2)

    results = await asyncio.gather(
        *(worker.submit(_request()) for _ in range(3)), return_exceptions=True
    )
    again = await worker.submit(_request())

    assert [type(r).__name__ for r in results] == ["RequestResult"] * 2 + ["QueueFullError"]
    rejected = results[2]
    assert isinstance(rejected, QueueFullError)
    assert (rejected.model, rejected.waiting, rejected.limit) == ("llama-3.2-1b", 2, 2)
    assert again.tokens_out == 3  # there is room again once the first two finish


async def test_timeout_closes_the_stream_to_the_model_server() -> None:
    clock = SimClock()
    server = MockLlamaServer(clock, pieces=tuple(f" w{i}" for i in range(100)))
    worker = PassthroughWorker(ECONOMY, _backend(server), clock)

    with pytest.raises(RequestTimeoutError):
        await run_with_timeout(clock, worker.submit(_request()), 75.0)

    # Closing the connection is what tells llama-server to stop generating and free the slot.
    assert server.streams[0].closed
    assert worker.in_flight == 0
    assert clock.now_ms() == 75.0


async def test_backend_failure_leaves_the_worker_usable() -> None:
    clock = SimClock()
    server = MockLlamaServer(clock, down={8081})
    worker = PassthroughWorker(ECONOMY, _backend(server), clock, max_in_flight=1)

    with pytest.raises(BackendError):
        await worker.submit(_request())
    assert worker.in_flight == 0

    server.down.clear()
    assert (await worker.submit(_request())).text == "Hello world!"


async def test_closed_worker_refuses_requests_and_closes_its_backend() -> None:
    clock = SimClock()
    backend = LlamaCppBackend(name="economy", base_url=URL)
    worker = PassthroughWorker(ECONOMY, backend, clock)

    await worker.aclose()

    assert backend._client.is_closed
    with pytest.raises(RuntimeError, match="closed"):
        await worker.submit(_request())


def test_in_flight_limit_must_be_positive() -> None:
    server = MockLlamaServer(SimClock())
    with pytest.raises(ValueError, match="max_in_flight"):
        PassthroughWorker(ECONOMY, _backend(server), SimClock(), max_in_flight=0)
