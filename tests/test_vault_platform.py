"""The vault's platform scope and the SMTP password (docs/48, the A5 plan's section 3).

- **End to end: no organization or provider key opens the SMTP password** (M43). Every
  ``Binding`` and ``ChannelBinding`` of two organizations, crafted ones included, under the
  master key and under the platform subkey; the ciphertexts swapped between the three tables;
  the SMTP ciphertext copied into a provider key row and test-connection run, which sends
  nothing. The control: the right ``PlatformBinding`` opens it.
- **Each protection alone:** the associated data of every scope differs for the crafted values
  (M41), and the key differs (M42): the platform ciphertext under the master key, with the right
  associated data, gives ``InvalidTag``.
- ``platform_secrets`` read by the platform secret stores only (A5-11, with its planted reader).
- ``set-smtp-password`` at the CLI: asked twice or read from a named variable, refused without a
  login or the settings, never printed, audited by kind; ``rotate-vault-key`` reseals it (A5-12).

Test values, not secrets.
"""

from __future__ import annotations

import base64
import sqlite3
import sys
from pathlib import Path

import pytest
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from gateway.audit import SqliteAuditStore
from gateway.clock import ClockEpoch, SimClock
from gateway.database import open_database
from gateway.migrations import MIGRATIONS, MigrationRunner
from gateway.platformsecrets import (
    SMTP_PASSWORD,
    InMemoryPlatformSecretStore,
    SqlitePlatformSecretStore,
    StoredPlatformSecret,
)
from gateway.state import run_now
from gateway.vault import (
    VAULT_KEY_ENV,
    VAULT_KEY_PREVIOUS_ENV,
    Binding,
    ChannelBinding,
    KeyUnreadableError,
    PlatformBinding,
    Vault,
    VaultIntegrityError,
    binding,
    channel_binding,
    master_key_id,
    platform_binding,
    platform_scope_key,
)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from manage import main as manage

MASTER = bytes(range(32))
OTHER = bytes(range(32, 64))
PASSWORD = "planted-smtp-password-4e1b9c"
HOST, PORT, USERNAME = "smtp.example.com", 587, "gateway"
WHERE = PlatformBinding(SMTP_PASSWORD, HOST, PORT, USERNAME)
ORIGIN = f"https://{HOST}:{PORT}"


def _crafted_bindings() -> list[Binding | ChannelBinding]:
    """Three organizations' bindings, with fields chosen to look like the SMTP server's: provider
    id, type and origin ``smtp`` and the SMTP host and port; organizations named ``smtp`` and
    ``smtp_password`` (so one provider binding has the platform binding's fields in its own
    layout: kind, host, port, the server's origin)."""
    found: list[Binding | ChannelBinding] = []
    for organization in ("north", "smtp", SMTP_PASSWORD):
        for provider_id, provider_type, url in (
            ("prv_0000000000000001", "openai_compatible", "https://api.example.com"),
            ("smtp", "smtp", ORIGIN),
            (SMTP_PASSWORD, SMTP_PASSWORD, f"https://{HOST}"),
            (HOST, str(PORT), ORIGIN),
        ):
            found.append(Binding(organization, provider_id, provider_type, url))
        for kind in ("webhook_url", "webhook_signing_key", "webhook_signing_key_previous"):
            for origin in ("https://hooks.example.com", ORIGIN, f"https://{HOST}"):
                found.append(ChannelBinding(organization, "chn_0000000000000001", kind, origin))
    return found


def test_the_associated_data_of_every_scope_differs_for_crafted_values() -> None:
    """M41: the platform prefix names the scope, so no other scope's bytes are the same."""
    key_id = master_key_id(MASTER)
    platform = platform_binding(SMTP_PASSWORD, HOST, PORT, USERNAME, key_id)
    assert platform.startswith(b"vault-platform-v1|")
    for other in _crafted_bindings():
        if isinstance(other, ChannelBinding):
            data = channel_binding(
                other.organization_id, other.channel_id, other.kind, other.url_origin, key_id
            )
        else:
            data = binding(
                other.organization_id, other.provider_id, other.provider_type, other.base_url,
                key_id,
            )  # fmt: skip
        assert data != platform, other
    # A "|" in the username cannot move a field: the key id (16 hex characters, never chosen by
    # anyone) is the last field, and the username is everything between the port and it.
    tricky = platform_binding(SMTP_PASSWORD, HOST, PORT, "a|b", key_id)
    assert tricky.rsplit(b"|", 1)[1] == key_id.encode()
    assert tricky != platform_binding(SMTP_PASSWORD, HOST, PORT, "a", key_id)
    assert len(key_id) == 16
    assert set(key_id) <= set("0123456789abcdef")


def test_the_platform_scope_has_a_key_of_its_own() -> None:
    """M42: the platform ciphertext opened with the master key and the right associated data,
    bypassing the vault, fails: the scope's subkey is not the master key."""
    subkey = platform_scope_key(MASTER)
    assert subkey != MASTER
    assert len(subkey) == 32
    assert subkey.hex()[:16] != master_key_id(MASTER)  # its label is not the key id's
    assert platform_scope_key(OTHER) != subkey
    sealed = Vault(MASTER).seal(PASSWORD, WHERE)
    aad = platform_binding(SMTP_PASSWORD, HOST, PORT, USERNAME, sealed.key_id)
    with pytest.raises(InvalidTag):
        AESGCM(MASTER).decrypt(sealed.nonce, sealed.ciphertext, aad)
    # The control: the subkey and the same associated data open it.
    assert AESGCM(subkey).decrypt(sealed.nonce, sealed.ciphertext, aad) == PASSWORD.encode()


def test_no_organization_or_provider_binding_opens_the_smtp_password() -> None:
    """M43, the vault's part: every crafted binding, through the vault and directly under both
    keys; and the other scopes' ciphertexts never open as the SMTP password."""
    vault = Vault(MASTER)
    sealed = vault.seal(PASSWORD, WHERE)
    assert sealed.last4 is None  # no last four characters of a platform secret
    key_id = sealed.key_id
    for other in _crafted_bindings():
        with pytest.raises(VaultIntegrityError):
            vault.open(sealed, other)
        aad = (
            channel_binding(
                other.organization_id, other.channel_id, other.kind, other.url_origin, key_id
            )
            if isinstance(other, ChannelBinding)
            else binding(
                other.organization_id,
                other.provider_id,
                other.provider_type,
                other.base_url,
                key_id,
            )
        )
        for key in (MASTER, platform_scope_key(MASTER)):
            with pytest.raises(InvalidTag):
                AESGCM(key).decrypt(sealed.nonce, sealed.ciphertext, aad)
    # Another server's binding: another host, port or username.
    for moved in (
        PlatformBinding(SMTP_PASSWORD, "smtp.example.net", PORT, USERNAME),
        PlatformBinding(SMTP_PASSWORD, HOST, 465, USERNAME),
        PlatformBinding(SMTP_PASSWORD, HOST, PORT, "someone-else"),
    ):
        with pytest.raises(VaultIntegrityError):
            vault.open(sealed, moved)
    # A provider key's and a channel secret's ciphertext, copied to the platform row.
    provider = vault.seal("sk-planted-provider-key-0000000000", _crafted_bindings()[1])
    channel = vault.seal("https://hooks.example.com/planted", _crafted_bindings()[5])
    for copied in (provider, channel):
        with pytest.raises(VaultIntegrityError):
            vault.open(copied, WHERE)
    # The control: the right binding opens it.
    assert vault.open(sealed, WHERE).reveal() == PASSWORD


def test_a_secret_is_never_rebound_into_another_scope() -> None:
    vault = Vault(MASTER)
    provider = _crafted_bindings()[0]
    sealed = vault.seal("sk-planted-provider-key-0000000000", provider)
    with pytest.raises(ValueError, match="own scope"):
        vault.rebind(sealed, provider, WHERE)


async def test_the_smtp_ciphertext_in_a_provider_key_row_is_never_sent() -> None:
    """M43, the provider path's part: the SMTP password's ciphertext copied into a provider's key
    row; test-connection refuses it, and nothing reaches the network."""
    from test_providers_api import MASTER as PROVIDER_MASTER
    from test_providers_api import _created, _probe_replies, rig

    async with rig() as r:
        alice = await r.login("alice")
        provider_id = await _created(r, alice)
        sealed = Vault(PROVIDER_MASTER).seal(PASSWORD, WHERE)
        await r.store.put_key(provider_id, sealed, 1.0)
        r.network.responses = _probe_replies()
        refused = await r.call(alice, "POST", f"/north/providers/{provider_id}/test")
        assert refused.status_code == 409, refused.text
        assert refused.json()["error"]["code"] == "key_unreadable"
        assert PASSWORD not in refused.text
        assert r.network.connections == []
        # The control: the same row with the provider's own key sealed for it does connect.
        config = await r.store.get(provider_id)
        assert config is not None
        await r.store.put_key(
            provider_id, Vault(PROVIDER_MASTER).seal("sk-planted-provider-0000", config.binding),
            2.0,
        )  # fmt: skip
        r.network.responses = _probe_replies()
        await r.call(alice, "POST", f"/north/providers/{provider_id}/test")
        assert r.network.connections != []


# --- the stores -----------------------------------------------------------------------------------


def _stored(vault: Vault, at: float = 1.0) -> StoredPlatformSecret:
    return StoredPlatformSecret(
        SMTP_PASSWORD, vault.seal(PASSWORD, WHERE), HOST, PORT, USERNAME, at
    )


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
async def test_the_platform_secret_store_keeps_one_row_a_kind(kind: str) -> None:
    if kind == "memory":
        store: InMemoryPlatformSecretStore | SqlitePlatformSecretStore = (
            InMemoryPlatformSecretStore()
        )
    else:
        connection = open_database(":memory:")
        MigrationRunner(connection, MIGRATIONS, ClockEpoch(SimClock())).migrate()
        store = SqlitePlatformSecretStore(connection)
    vault = Vault(MASTER)
    assert await store.get(SMTP_PASSWORD) is None
    first, second = _stored(vault, 1.0), _stored(vault, 2.0)
    async with store.write_transaction():
        await store.put(first)
    await store.put(second)
    found = await store.get(SMTP_PASSWORD)
    assert found == second
    assert await store.all() == [second]
    assert found is not None
    assert vault.open(found.sealed, found.binding).reveal() == PASSWORD
    assert PASSWORD not in repr(found)


def test_only_the_platform_secret_stores_read_platform_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A5-11: the SQL naming ``platform_secrets`` is the stores', the schemas' and the copy
    between databases'. The control: a planted reader is found."""
    import test_alert_channel_stores as grep

    assert grep._sql_using("platform_secrets") == {
        ("platformsecrets.py", "SqlitePlatformSecretStore"),
        ("postgres.py", "PostgresPlatformSecretStore"),
        ("migrations/m0018_email_channels.py", "<module>"),
        ("postgres.py", "<module>"),
        ("pg_migrate.py", "<module>"),
    }
    monkeypatch.setattr(grep, "REPO", tmp_path)
    (tmp_path / "src" / "gateway").mkdir(parents=True)
    (tmp_path / "src" / "gateway" / "nosy.py").write_text(
        'class Provider:\n    SQL = "SELECT ciphertext FROM platform_secrets"\n', encoding="utf-8"
    )
    assert grep._sql_using("platform_secrets") == {("nosy.py", "Provider")}


# --- the CLI --------------------------------------------------------------------------------------


def _key(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _environ(path: Path, **extra: str) -> dict[str, str]:
    return {
        "GATEWAY_DATABASE": str(path),
        VAULT_KEY_ENV: _key(MASTER),
        "GATEWAY_SMTP_HOST": HOST,
        "GATEWAY_SMTP_USERNAME": USERNAME,
        "GATEWAY_SMTP_FROM": "alerts@example.com",
        "GATEWAY_ALERT_EMAIL_DOMAINS": "example.com",
        **extra,
    }


def _row(path: Path) -> StoredPlatformSecret | None:
    connection = open_database(path)
    try:
        return run_now(SqlitePlatformSecretStore(connection).get(SMTP_PASSWORD))
    finally:
        connection.close()


def _audit(path: Path) -> list[tuple[str, str, str, str]]:
    connection = open_database(path)
    try:
        return connection.execute(
            "SELECT action, target_type, target_id, details FROM audit_log"
            " WHERE action = 'smtp_password_set'"
        ).fetchall()
    finally:
        connection.close()


def test_set_smtp_password_asks_twice_seals_it_and_prints_nothing_of_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "gateway.db"
    asked: list[str] = []

    def prompt(question: str) -> str:
        asked.append(question)
        return PASSWORD

    assert manage(["set-smtp-password"], _environ(path), read_password=prompt) == 0
    out, err = capsys.readouterr()
    assert len(asked) == 2
    assert PASSWORD not in out + err
    stored = _row(path)
    assert stored is not None
    assert PASSWORD.encode() not in stored.sealed.ciphertext
    assert (stored.host, stored.port, stored.username) == (HOST, PORT, USERNAME)
    assert Vault(MASTER).open(stored.sealed, WHERE).reveal() == PASSWORD
    [row] = _audit(path)
    assert row[1:3] == ("platform_secret", SMTP_PASSWORD)
    assert PASSWORD not in str(row)


def test_set_smtp_password_reads_a_named_variable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "gateway.db"

    def never(_: str) -> str:
        raise AssertionError("no prompt with --from-env")

    environ = _environ(path, SMTP_PASSWORD_FOR_SETUP=PASSWORD)
    assert manage(["set-smtp-password", "--from-env", "SMTP_PASSWORD_FOR_SETUP"], environ,
                  read_password=never) == 0  # fmt: skip
    out, err = capsys.readouterr()
    assert PASSWORD not in out + err
    stored = _row(path)
    assert stored is not None
    assert Vault(MASTER).open(stored.sealed, WHERE).reveal() == PASSWORD


@pytest.mark.parametrize(
    ("argv", "change", "answers", "words"),
    [
        (["set-smtp-password"], {"GATEWAY_SMTP_USERNAME": ""}, [PASSWORD], "without a login"),
        (["set-smtp-password"], {"GATEWAY_SMTP_HOST": ""}, [PASSWORD], "GATEWAY_SMTP_HOST"),
        (["set-smtp-password"], {}, [PASSWORD, PASSWORD + "x"], "differ"),
        (["set-smtp-password"], {}, ["", ""], "empty"),
        (["set-smtp-password", "--from-env", "MISSING_VAR"], {}, [], "MISSING_VAR"),
    ],
)
def test_set_smtp_password_refuses_and_stores_nothing(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    change: dict[str, str],
    answers: list[str],
    words: str,
) -> None:
    path = tmp_path / "gateway.db"
    feed = iter(answers)
    assert manage(argv, _environ(path, **change), read_password=lambda _: next(feed)) != 0
    out, err = capsys.readouterr()
    assert words in err
    assert PASSWORD not in out + err
    assert _row(path) is None
    assert _audit(path) == []


def test_rotate_vault_key_reseals_the_platform_scope(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A5-12: sealed under the old master key, the SMTP password is resealed under the new one
    (its subkey from the new master), in the rotation's transaction."""
    path = tmp_path / "gateway.db"
    old = _environ(path) | {VAULT_KEY_ENV: _key(OTHER)}
    assert manage(["set-smtp-password"], old, read_password=lambda _: PASSWORD) == 0
    before = _row(path)
    assert before is not None
    assert before.sealed.key_id == master_key_id(OTHER)
    capsys.readouterr()
    environ = _environ(path) | {VAULT_KEY_PREVIOUS_ENV: _key(OTHER)}
    assert manage(["rotate-vault-key"], environ) == 0
    err = capsys.readouterr().err
    assert "1 platform secret(s)" in err
    assert PASSWORD not in err
    after = _row(path)
    assert after is not None
    assert after.sealed.key_id == master_key_id(MASTER)
    assert Vault(MASTER).open(after.sealed, WHERE).reveal() == PASSWORD
    with pytest.raises(KeyUnreadableError):  # the old master alone no longer opens it
        Vault(OTHER).open(after.sealed, WHERE)


def test_rotate_vault_key_reports_an_unreadable_platform_secret(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "gateway.db"
    lost = bytes(range(64, 96))
    assert manage(["set-smtp-password"], _environ(path) | {VAULT_KEY_ENV: _key(lost)},
                  read_password=lambda _: PASSWORD) == 0  # fmt: skip
    capsys.readouterr()
    environ = _environ(path) | {VAULT_KEY_PREVIOUS_ENV: _key(OTHER)}
    assert manage(["rotate-vault-key"], environ) == 0
    err = capsys.readouterr().err
    assert "smtp_credential_unreadable" in err
    assert SMTP_PASSWORD in err
    stored = _row(path)
    assert stored is not None
    assert stored.sealed.key_id == master_key_id(lost)  # left as it was


def test_the_audit_store_takes_only_the_kind_as_a_platform_secrets_target() -> None:
    connection = open_database(":memory:")
    MigrationRunner(connection, MIGRATIONS, ClockEpoch(SimClock())).migrate()
    from gateway.audit import AuditAction, AuditEvent, AuditLog, Outcome, TargetType

    log = AuditLog(SqliteAuditStore(connection))
    with pytest.raises(ValueError, match="target"):
        run_now(
            log.write(
                AuditEvent(AuditAction.SMTP_PASSWORD_SET, Outcome.SUCCESS,
                           TargetType.PLATFORM_SECRET, PASSWORD, None, {}),
                1.0,
            )
        )  # fmt: skip
    assert isinstance(connection, sqlite3.Connection)
