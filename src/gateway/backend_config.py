"""Which backend the gateway uses, read from YAML, and the registry built from that choice.

See docs/12-real-models.md.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Literal, cast

import httpx
import yaml

from gateway.backends.llamacpp import Endpoint, LlamaCppBackend
from gateway.clock import Clock
from gateway.passthrough import PassthroughWorker
from gateway.profiles import PROFILES, Tier
from gateway.registry import ModelRegistry

BackendKind = Literal["simulated", "llamacpp"]


class BackendConfigError(ValueError):
    """The backends file is malformed or inconsistent."""


@dataclass(frozen=True, slots=True)
class LlamaServer:
    url: str
    # Sent as the request's "model" field. llama-server ignores it; Ollama requires it.
    model: str | None = None


@dataclass(frozen=True, slots=True)
class BackendConfig:
    kind: BackendKind = "simulated"
    endpoint: Endpoint = "completions"
    max_in_flight: int | None = None
    servers: Mapping[Tier, LlamaServer] = field(default_factory=dict)
    # A calibration file whose timing parameters replace the illustrative ones in the
    # simulator. Only used by the simulated backend.
    profiles_path: Path | None = None


def parse_backend_config(data: object, *, kind_override: str | None = None) -> BackendConfig:
    if not isinstance(data, dict):
        raise BackendConfigError("the backends file must be a mapping")
    kind = kind_override or data.get("backend", "simulated")
    if kind not in ("simulated", "llamacpp"):
        raise BackendConfigError(f"backend must be 'simulated' or 'llamacpp', not {kind!r}")

    simulated = data.get("simulated") or {}
    if not isinstance(simulated, dict):
        raise BackendConfigError("'simulated' must be a mapping")
    profiles = simulated.get("profiles")
    if profiles is not None and (not isinstance(profiles, str) or not profiles):
        raise BackendConfigError("simulated.profiles must be the path of a calibration file")

    section = data.get("llamacpp") or {}
    if not isinstance(section, dict):
        raise BackendConfigError("'llamacpp' must be a mapping")
    endpoint = section.get("endpoint", "completions")
    if endpoint not in ("completions", "chat"):
        raise BackendConfigError(
            f"llamacpp.endpoint must be 'completions' or 'chat', not {endpoint!r}"
        )
    max_in_flight = section.get("max_in_flight")
    if max_in_flight is not None and (
        isinstance(max_in_flight, bool) or not isinstance(max_in_flight, int) or max_in_flight < 1
    ):
        raise BackendConfigError("llamacpp.max_in_flight must be a positive integer")

    raw_servers = section.get("servers") or {}
    if not isinstance(raw_servers, dict):
        raise BackendConfigError("llamacpp.servers must map each tier to a server")
    servers: dict[Tier, LlamaServer] = {}
    for name, raw in raw_servers.items():
        try:
            tier = Tier(name)
        except ValueError:
            raise BackendConfigError(
                f"llamacpp.servers: {name!r} is not a tier; tiers are: "
                f"{', '.join(t.value for t in Tier)}"
            ) from None
        if not isinstance(raw, dict) or not isinstance(raw.get("url"), str) or not raw["url"]:
            raise BackendConfigError(f"llamacpp.servers.{name} needs a 'url'")
        if not raw["url"].startswith(("http://", "https://")):
            raise BackendConfigError(f"llamacpp.servers.{name}.url must start with http(s)://")
        model = raw.get("model")
        if model is not None and (not isinstance(model, str) or not model):
            raise BackendConfigError(f"llamacpp.servers.{name}.model must be a non-empty string")
        servers[tier] = LlamaServer(raw["url"], model)

    if kind == "llamacpp":
        missing = [t.value for t in Tier if t not in servers]
        if missing:
            raise BackendConfigError(
                f"backend is 'llamacpp' but no server is configured for: {', '.join(missing)}"
            )
    return BackendConfig(
        kind=cast(BackendKind, kind),
        endpoint=cast(Endpoint, endpoint),
        max_in_flight=max_in_flight,
        servers=servers,
        profiles_path=Path(profiles) if profiles else None,
    )


def load_backend_config(
    path: Path | None = None, *, kind_override: str | None = None
) -> BackendConfig:
    """Load a backends file, or the one packaged with the gateway if no path is given."""
    if path is None:
        text = resources.files("gateway").joinpath("backends.yaml").read_text(encoding="utf-8")
    else:
        text = path.read_text(encoding="utf-8")
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise BackendConfigError(f"the backends file is not valid YAML: {exc}") from exc
    return parse_backend_config(data, kind_override=kind_override)


def build_llamacpp_registry(
    config: BackendConfig, clock: Clock, *, client: httpx.AsyncClient | None = None
) -> ModelRegistry:
    """One pass-through worker per tier, each in front of its configured model server.

    Names and aliases are the same as the simulated registries', so plans, routing and the
    rest of the gateway do not change. ``client`` replaces the HTTP client, for tests.
    """
    registry = ModelRegistry()
    for tier, profile in PROFILES.items():
        server = config.servers[tier]
        backend = LlamaCppBackend(
            name=tier.value,
            base_url=server.url,
            model=server.model,
            endpoint=config.endpoint,
            client=client,
        )
        worker = PassthroughWorker(profile, backend, clock, max_in_flight=config.max_in_flight)
        registry.register(tier.value, worker, aliases=[profile.name])
    return registry
