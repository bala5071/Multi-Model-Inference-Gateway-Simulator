from __future__ import annotations

import pytest

from gateway.registry import ModelRegistry, UnknownModelError
from gateway.schema import GenerateRequest, RequestResult, TokenCallback


class StubWorker:
    def __init__(self, model: str) -> None:
        self.model = model
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True

    async def submit(
        self, request: GenerateRequest, *, on_token: TokenCallback | None = None
    ) -> RequestResult:
        return RequestResult(
            request_id=request.request_id,
            model=self.model,
            enqueue_ms=0.0,
            start_ms=0.0,
            first_token_ms=0.0,
            finish_ms=0.0,
            tokens_in=0,
            tokens_out=1,
            cost_usd=0.0,
        )


def _registry() -> tuple[ModelRegistry, StubWorker, StubWorker]:
    economy = StubWorker("llama-3.2-1b")
    premium = StubWorker("llama-3.1-8b")
    registry = ModelRegistry()
    registry.register("economy", economy, aliases=["llama-3.2-1b", "small"])
    registry.register("premium", premium, aliases=["llama-3.1-8b"])
    return registry, economy, premium


def test_get_by_name_returns_the_registered_worker() -> None:
    registry, economy, premium = _registry()
    assert registry.get("economy") is economy
    assert registry.get("premium") is premium


def test_alias_resolves_to_the_same_worker() -> None:
    registry, economy, premium = _registry()
    assert registry.get("llama-3.2-1b") is economy
    assert registry.get("small") is economy
    assert registry.get("llama-3.1-8b") is premium
    assert registry.resolve("llama-3.2-1b") == "economy"
    assert registry.resolve("economy") == "economy"


def test_names_exclude_aliases_and_valid_names_include_them() -> None:
    registry, _, _ = _registry()
    assert registry.names == ("economy", "premium")
    assert registry.valid_names == (
        "economy",
        "premium",
        "llama-3.2-1b",
        "small",
        "llama-3.1-8b",
    )


@pytest.mark.parametrize("name", ["gpt-4", "", "Economy", "economy "])
def test_unknown_model_error_carries_the_valid_names(name: str) -> None:
    registry, _, _ = _registry()

    with pytest.raises(UnknownModelError) as excinfo:
        registry.get(name)

    error = excinfo.value
    assert error.model == name
    assert error.valid_names == registry.valid_names
    assert isinstance(error, LookupError)
    assert repr(name) in str(error)
    for valid_name in registry.valid_names:
        assert valid_name in str(error)


def test_aliases_of_lists_a_models_aliases() -> None:
    registry, _, _ = _registry()
    assert registry.aliases_of("economy") == ("llama-3.2-1b", "small")
    assert registry.aliases_of("premium") == ("llama-3.1-8b",)
    with pytest.raises(UnknownModelError):
        registry.aliases_of("small")  # an alias, not a registered name


async def test_aclose_closes_every_worker() -> None:
    registry, economy, premium = _registry()
    await registry.aclose()
    assert economy.closed
    assert premium.closed


def test_empty_registry_reports_no_valid_names() -> None:
    with pytest.raises(UnknownModelError) as excinfo:
        ModelRegistry().get("economy")
    assert excinfo.value.valid_names == ()


async def test_submit_routes_by_the_request_model() -> None:
    registry, _, _ = _registry()
    by_name = await registry.submit(GenerateRequest("r1", "premium", "hi"))
    by_alias = await registry.submit(GenerateRequest("r2", "small", "hi"))
    assert by_name.model == "llama-3.1-8b"
    assert by_alias.model == "llama-3.2-1b"


async def test_submit_to_unknown_model_raises() -> None:
    registry, _, _ = _registry()
    with pytest.raises(UnknownModelError):
        await registry.submit(GenerateRequest("r1", "nope", "hi"))


@pytest.mark.parametrize(
    ("name", "aliases"),
    [
        ("economy", []),
        ("small", []),
        ("standard", ["premium"]),
        ("standard", ["llama-3.2-1b"]),
        ("standard", ["standard"]),
        ("standard", ["mid", "mid"]),
        ("", []),
        ("standard", [""]),
    ],
)
def test_conflicting_or_empty_names_are_rejected(name: str, aliases: list[str]) -> None:
    registry, _, _ = _registry()
    before = registry.valid_names

    with pytest.raises(ValueError, match=r"registered|duplicate|empty"):
        registry.register(name, StubWorker("x"), aliases=aliases)

    assert registry.valid_names == before
