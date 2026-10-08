from __future__ import annotations

import pytest

from gateway.backends import Backend, Generation, SimulatedBackend
from gateway.cost_model import OutputLengthGenerator
from gateway.schema import GenerateRequest
from gateway.tokenizer import WordTokenizer

PROMPTS = ("hello", "Summarise this contract.", "one two three four five", "")


def _request(prompt: str, max_output_tokens: int | None = None) -> GenerateRequest:
    return GenerateRequest("r", "economy", prompt, max_output_tokens)


def test_backend_cannot_be_instantiated_without_generate() -> None:
    with pytest.raises(TypeError):
        Backend()  # type: ignore[abstract]


def test_simulated_backend_is_a_backend() -> None:
    assert isinstance(SimulatedBackend(seed=1), Backend)


async def test_same_seed_gives_the_same_generations() -> None:
    first = SimulatedBackend(seed=7)
    second = SimulatedBackend(seed=7)
    for prompt in PROMPTS:
        assert await first.generate(_request(prompt)) == await second.generate(_request(prompt))


async def test_generation_does_not_depend_on_call_history() -> None:
    backend = SimulatedBackend(seed=7)
    forward = [await backend.generate(_request(p)) for p in PROMPTS]
    backward = [await backend.generate(_request(p)) for p in reversed(PROMPTS)]
    assert forward == list(reversed(backward))


async def test_different_seeds_give_different_output_lengths() -> None:
    lengths_a = [(await SimulatedBackend(seed=7).generate(_request(p))).tokens_out for p in PROMPTS]
    lengths_b = [(await SimulatedBackend(seed=8).generate(_request(p))).tokens_out for p in PROMPTS]
    assert lengths_a != lengths_b


async def test_token_counts_follow_the_prompt_and_the_length_generator() -> None:
    generation = await SimulatedBackend(seed=7).generate(_request("one two three four five"))
    assert generation.tokens_in == 5
    assert generation.tokens_out == OutputLengthGenerator(seed=7).length_for(
        "one two three four five"
    )


async def test_max_output_tokens_caps_the_output() -> None:
    backend = SimulatedBackend(seed=7)
    uncapped = (await backend.generate(_request("Summarise this contract."))).tokens_out
    assert uncapped > 10
    capped = await backend.generate(_request("Summarise this contract.", max_output_tokens=10))
    assert capped.tokens_out == 10
    roomy = await backend.generate(_request("Summarise this contract.", max_output_tokens=10_000))
    assert roomy.tokens_out == uncapped


async def test_prompt_tokens_come_from_the_injected_tokenizer() -> None:
    class CharTokenizer:
        def encode(self, text: str) -> tuple[int, ...]:
            return tuple(ord(c) for c in text)

    default = await SimulatedBackend(seed=7).generate(_request("ab cd"))
    custom = await SimulatedBackend(seed=7, tokenizer=CharTokenizer()).generate(_request("ab cd"))

    assert default.prompt_tokens == WordTokenizer().encode("ab cd")
    assert custom.prompt_tokens == (97, 98, 32, 99, 100)
    assert (default.tokens_in, custom.tokens_in) == (2, 5)
    assert default.tokens_out == custom.tokens_out


def test_generation_counts_its_prompt_tokens() -> None:
    assert Generation(prompt_tokens=(), tokens_out=1).tokens_in == 0
    assert Generation(prompt_tokens=(5, 6, 7), tokens_out=1).tokens_in == 3


def test_generation_rejects_empty_output() -> None:
    with pytest.raises(ValueError, match="tokens_out"):
        Generation(prompt_tokens=(1,), tokens_out=0)


def test_request_rejects_non_positive_max_output_tokens() -> None:
    with pytest.raises(ValueError, match="max_output_tokens"):
        GenerateRequest("r", "economy", "hi", max_output_tokens=0)
