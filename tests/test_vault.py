"""The vault and the redaction layer. See docs/32-provider-settings-and-vault.md. Test values,
not secrets."""

from __future__ import annotations

import base64
import logging
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from gateway.app import create_default_app
from gateway.redaction import REDACTED, Redactor, install
from gateway.vault import (
    VAULT_KEY_ENV,
    VAULT_KEY_PREVIOUS_ENV,
    Binding,
    KeyUnreadableError,
    Vault,
    VaultIntegrityError,
    VaultKeyError,
    load_master_keys,
    master_key_id,
)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from manage import main as manage

MASTER = bytes(range(32))
OTHER = bytes(range(32, 64))
KEY = "sk-planted-vault-0123456789abcdefghijklmnop"
WHERE = Binding("north", "prv_00000000000000aa", "openai_compatible", "https://api.example.com/v1")


def _key(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def test_a_key_seals_and_opens_and_only_its_last_four_are_kept_in_clear() -> None:
    vault = Vault(MASTER)
    sealed = vault.seal(KEY, WHERE)
    assert sealed.key_id == master_key_id(MASTER)
    assert KEY.encode() not in sealed.ciphertext
    assert sealed.last4 == KEY[-4:]
    assert vault.open(sealed, WHERE).reveal() == KEY
    assert KEY not in repr(sealed)
    assert repr(vault) == f"Vault(current_key_id={vault.current_key_id!r}, <keys redacted>)"
    # A short key shows nothing of itself.
    assert vault.seal("short-key-123", WHERE).last4 is None
    # A new nonce each time.
    assert vault.seal(KEY, WHERE).nonce != sealed.nonce


@pytest.mark.parametrize(
    "elsewhere",
    [
        replace(WHERE, organization_id="south"),
        replace(WHERE, provider_id="prv_00000000000000bb"),
        replace(WHERE, provider_type="anthropic"),
        replace(WHERE, base_url="https://attacker.example.com/v1"),
    ],
)
def test_a_ciphertext_opens_only_where_it_was_sealed(elsewhere: Binding) -> None:
    vault = Vault(MASTER)
    sealed = vault.seal(KEY, WHERE)
    with pytest.raises(VaultIntegrityError):
        vault.open(sealed, elsewhere)


def test_a_path_change_on_the_same_origin_keeps_the_binding() -> None:
    vault = Vault(MASTER)
    sealed = vault.seal(KEY, WHERE)
    assert vault.open(sealed, replace(WHERE, base_url="https://api.example.com/v2")).reveal() == KEY


def test_no_trial_decryption(monkeypatch: pytest.MonkeyPatch) -> None:
    """A row sealed under a master key that is not loaded is unreadable, and no other loaded
    key is tried on it."""
    sealed = Vault(OTHER).seal(KEY, WHERE)
    vault = Vault(MASTER, bytes(range(64, 96)))
    tried: list[bytes] = []
    real = AESGCM.decrypt

    def counting(self: AESGCM, nonce: bytes, data: bytes, aad: bytes | None) -> bytes:
        tried.append(nonce)
        return real(self, nonce, data, aad)

    monkeypatch.setattr(AESGCM, "decrypt", counting)
    assert not vault.readable(sealed)
    with pytest.raises(KeyUnreadableError) as unreadable:
        vault.open(sealed, WHERE)
    assert unreadable.value.key_id == master_key_id(OTHER)
    assert tried == []


def test_the_previous_master_key_opens_and_the_new_one_seals() -> None:
    old = Vault(OTHER).seal(KEY, WHERE)
    vault = Vault(MASTER, OTHER)
    assert vault.open(old, WHERE).reveal() == KEY
    resealed = vault.reseal(old, WHERE)
    assert resealed.key_id == master_key_id(MASTER)
    assert resealed.last4 == old.last4
    assert Vault(MASTER).open(resealed, WHERE).reveal() == KEY


def test_master_keys_from_the_environment() -> None:
    assert load_master_keys({VAULT_KEY_ENV: _key(MASTER)}) == (MASTER, None)
    assert load_master_keys({VAULT_KEY_ENV: _key(MASTER), VAULT_KEY_PREVIOUS_ENV: _key(OTHER)}) == (
        MASTER,
        OTHER,
    )
    for environ in ({}, {VAULT_KEY_ENV: "too-short"}, {VAULT_KEY_ENV: "!!!not base64!!!"}):
        with pytest.raises(VaultKeyError) as refused:
            load_master_keys(environ)
        assert "too-short" not in str(refused.value)


def test_the_server_refuses_to_start_without_the_master_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(VAULT_KEY_ENV)
    with pytest.raises(VaultKeyError, match=VAULT_KEY_ENV):
        create_default_app()
    monkeypatch.setenv(VAULT_KEY_ENV, "not-a-32-byte-key")
    with pytest.raises(VaultKeyError):
        create_default_app()


def test_rotation_through_the_cli(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Keys sealed under the old master key are re-sealed under the new one, after a backup;
    afterwards the old key is not needed."""
    from gateway.clock import ClockEpoch, SimClock
    from gateway.database import open_database
    from gateway.migrations import MIGRATIONS, MigrationRunner
    from gateway.providers import ProviderStore
    from test_providers_api import seed_provider

    path = tmp_path / "gateway.db"
    connection = open_database(path)
    MigrationRunner(connection, MIGRATIONS, ClockEpoch(SimClock())).migrate()
    config = seed_provider(connection)
    ProviderStore(connection).put_key(config.id, Vault(OTHER).seal(KEY, config.binding), 1.0)
    connection.close()
    environ = {
        "GATEWAY_DATABASE": str(path),
        VAULT_KEY_ENV: _key(MASTER),
        VAULT_KEY_PREVIOUS_ENV: _key(OTHER),
    }
    assert manage(["rotate-vault-key"], environ) == 0
    err = capsys.readouterr().err
    assert "re-sealed 1 provider key(s)" in err
    assert len(list(tmp_path.glob("gateway.db.before-vault-rotation-*.db"))) == 1
    connection = open_database(path)
    stored = ProviderStore(connection).get_key(config.id)
    assert stored is not None
    assert Vault(MASTER).open(stored.sealed, config.binding).reveal() == KEY
    connection.close()
    # Without the previous key there is nothing to rotate from.
    assert manage(["rotate-vault-key"], {**environ, VAULT_KEY_PREVIOUS_ENV: ""}) == 1
    assert KEY not in capsys.readouterr().err


# --- the redaction layer -----------------------------------------------------------------------


def test_redaction_scrubs_watched_values_patterns_and_exception_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    redactor = Redactor()
    remove = install(redactor)
    try:
        log = logging.getLogger("gateway.test")
        with redactor.watching("plain-secret-value-42"):
            log.warning("value %s", "plain-secret-value-42")
        log.warning("after: %s", "plain-secret-value-42")  # no longer watched
        log.warning("header Authorization: Bearer abc.def-ghi")
        log.warning("x-api-key: sk-ant-0123456789abcdefghij")
        log.warning("key sk-0123456789abcdefghij and gwk_%s_%s", "a" * 16, "B" * 43)
        try:
            raise RuntimeError("failed with sk-0123456789abcdefghijkl")
        except RuntimeError:
            log.exception("boom")
        messages = [r.getMessage() for r in caplog.records]
    finally:
        remove()
    assert messages[0] == f"value {REDACTED}"
    assert messages[1] == "after: plain-secret-value-42"
    assert "abc.def-ghi" not in messages[2]
    assert "sk-ant-0123456789abcdefghij" not in messages[3]
    assert "sk-0123456789abcdefghij" not in messages[4]
    assert "B" * 43 not in messages[4]
    assert "sk-0123456789abcdefghijkl" not in (caplog.records[5].exc_text or "")
    assert "RuntimeError" in (caplog.records[5].exc_text or "")
    # Removed: records are no longer scrubbed.
    caplog.clear()
    logging.getLogger("gateway.test").warning("sk-0123456789abcdefghij")
    assert caplog.records[0].getMessage() == "sk-0123456789abcdefghij"


def _vault_importers(roots: list[Path]) -> set[str]:
    import ast

    found = set()
    for root in roots:
        for path in root.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.ImportFrom)
                    and node.module == "gateway.vault"
                    and any(alias.name in ("Vault", "*") for alias in node.names)
                ):
                    found.add(path.name)
    return found


def test_only_the_provider_service_the_wiring_and_the_cli_hold_the_vault() -> None:
    repo = Path(__file__).resolve().parent.parent
    # benchmarks/provider_requests.py times the vault itself and is left out.
    assert _vault_importers([repo / "src", repo / "scripts"]) == {
        "providers.py",
        "app.py",
        "manage.py",
    }


def test_the_check_finds_another_importer(tmp_path: Path) -> None:
    (tmp_path / "chatty.py").write_text("from gateway.vault import Vault\n", encoding="utf-8")
    assert _vault_importers([tmp_path]) == {"chatty.py"}


def test_redaction_keeps_uvicorn_access_lines_working_and_reprs_scrubbed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """uvicorn's access formatter unpacks a record's arguments itself: they stay arguments
    (found by running the server; every access line had failed to format). An argument whose
    repr shows a secret is caught on the formatted message."""
    from uvicorn.logging import AccessFormatter

    class Leaky:
        def __repr__(self) -> str:
            return "Leaky(sk-0123456789abcdefghijkl)"

    caplog.set_level(logging.INFO)
    redactor = Redactor()
    remove = install(redactor)
    try:
        access = logging.getLogger("uvicorn.access")
        access.info(
            '%s - "%s %s HTTP/%s" %d',
            "127.0.0.1:5000",
            "GET",
            "/healthz?k=sk-0123456789abcdefghijkl",
            "1.1",
            200,
        )
        logging.getLogger("gateway.test").warning("object %r", Leaky())
    finally:
        remove()
    line = AccessFormatter("%(client_addr)s %(request_line)s %(status_code)s", use_colors=False)
    formatted = line.format(caplog.records[0])
    assert formatted.startswith("127.0.0.1:5000 GET /healthz?k=[redacted]")
    assert "sk-0123456789abcdefghijkl" not in formatted
    assert "sk-0123456789abcdefghijkl" not in caplog.records[1].getMessage()
