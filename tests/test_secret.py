"""``Secret`` never shows its value by accident; ``reveal()`` is called in one place; and a
provider key passed through every adapter appears nowhere but the one request header that
carries it, with a planted control the scan must find. See docs/31-chat-and-adapters.md.
Test values, not secrets."""

from __future__ import annotations

import ast
import copy
import json
import logging
import pickle
import traceback
from pathlib import Path

import pytest

from gateway.chat import ChatMessage, ChatRequest, ProviderError
from gateway.clock import SimClock
from gateway.secret import REDACTED, Secret
from mock_providers import Reply, ScriptedProvider, anthropic_events, ollama_text, openai_text
from test_adapters import ADAPTERS, anthropic, ollama, openai

SRC = Path(__file__).resolve().parent.parent / "src" / "gateway"
KEY = "sk-ant-planted-0123456789abcdefghijklmnopqrstuv"


def test_a_secret_never_shows_its_value() -> None:
    secret = Secret(KEY)
    shown = [
        repr(secret),
        str(secret),
        f"{secret}",
        f"{secret!r}",
        logging.Formatter().format(logging.makeLogRecord({"msg": "%s", "args": (secret,)})),
        format(secret, ">80"),
    ]
    assert all(text == REDACTED for text in shown)
    assert KEY not in repr([secret, {"key": secret}])
    with pytest.raises(TypeError):
        json.dumps({"key": secret})
    with pytest.raises(TypeError):
        pickle.dumps(secret)
    with pytest.raises(TypeError):
        copy.copy(secret)
    with pytest.raises(TypeError):
        copy.deepcopy({"key": secret})
    assert secret != Secret(KEY)  # no comparison by value
    assert secret.reveal() == KEY
    with pytest.raises(ValueError, match="non-empty"):
        Secret("")


def _reveal_calls() -> list[tuple[str, str]]:
    """(file, enclosing function) of every ``.reveal()`` call and every use of the mangled
    attribute in the package."""
    found = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for function in ast.walk(tree):
            if not isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            for node in ast.walk(function):
                if isinstance(node, ast.Attribute) and node.attr in ("reveal", "_Secret__value"):
                    found.append((path.relative_to(SRC).as_posix(), function.name))
    return found


def test_reveal_is_called_only_by_the_header_builder() -> None:
    assert _reveal_calls() == [
        ("adapters/http.py", "credential_headers"),
        ("adapters/http.py", "credential_headers"),
    ]


def test_the_check_finds_a_planted_reveal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    planted = tmp_path / "gateway"
    (planted / "adapters").mkdir(parents=True)
    (planted / "adapters" / "http.py").write_text(
        "def credential_headers(c):\n    return c.reveal()\n", encoding="utf-8"
    )
    (planted / "logs.py").write_text("def log(c):\n    print(c.reveal())\n", encoding="utf-8")
    monkeypatch.setattr("test_secret.SRC", planted)
    assert ("logs.py", "log") in _reveal_calls()


async def test_a_provider_key_appears_only_in_the_header_that_carries_it(
    caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    caplog.set_level(logging.DEBUG)  # httpx and httpcore included
    echo = json.dumps({"error": {"type": "authentication_error", "message": f"bad key {KEY}"}})
    corpus: dict[str, str] = {}
    headers_sent: list[str] = []
    for factory in ADAPTERS:
        name = factory.__name__
        clock = SimClock()
        ok = {
            ollama: ollama_text("Hi"),
            openai: openai_text("Hi"),
            anthropic: anthropic_events({"text": "Hi"}),
        }[factory]
        broken = {
            ollama: ollama_text("Hi", "there"),
            openai: openai_text("Hi", "there"),
            anthropic: anthropic_events({"text": "Hi"}, {"text": "there"}),
        }[factory]
        broken.fail_after = 2
        provider = ScriptedProvider(
            clock, [ok, Reply(status=401, chunks=[(0.0, echo.encode())]), broken]
        )
        adapter = factory(provider)
        request = ChatRequest("r", "economy", (ChatMessage("user", "hello"),))
        result = await adapter.chat(request, credential=Secret(KEY))
        corpus[f"{name} result"] = repr(result)
        for attempt in ("401", "broken"):
            with pytest.raises(ProviderError) as failed:
                await adapter.chat(request, credential=Secret(KEY))
            corpus[f"{name} {attempt} error"] = (
                str(failed.value)
                + repr(failed.value)
                + "".join(traceback.format_exception(failed.value))
            )
        corpus[f"{name} adapter"] = repr(adapter) + repr(vars(adapter))
        for recorded in provider.requests:
            corpus[f"{name} body"] = json.dumps(recorded.body)
            headers_sent.extend(f"{k}: {v}" for k, v in recorded.headers.items())

    def scan() -> list[str]:
        out, err = capsys.readouterr()
        texts = {
            **corpus,
            "logs": "\n".join(
                f"{r.name} {r.getMessage()} {r.args!r} {r.exc_text or ''}" for r in caplog.records
            ),
            "stdout": out,
            "stderr": err,
        }
        return sorted(where for where, text in texts.items() if KEY in text)

    # Where it must be: the credential header of each request to the two hosted formats.
    assert sum(KEY in h for h in headers_sent) == 6  # openai and anthropic, 3 requests each
    assert scan() == []
    # The control: the key logged once by mistake is found.
    logging.getLogger("gateway").warning("oops %s", KEY)
    assert scan() == ["logs"]
