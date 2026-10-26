# Licenses of what the gateway ships with

Every runtime dependency of the `gateway` package, and every piece of third-party data it
bundles, with its license. `tests/test_licenses.py` keeps this file honest: it walks the
installed runtime dependencies, fails on any license that is not allowed below, fails if a
package is missing from the table or listed with another license, and checks the pinned
versions against `pyproject.toml`. Development tools (pytest, ruff, mypy, matplotlib, ...) are
not shipped and are not listed.

## Allowed licenses

Allowed: MIT, MIT-0, BSD-2-Clause, BSD-3-Clause, Apache-2.0, ISC, PSF-2.0, MPL-2.0

All are permissive except MPL-2.0, a file-level copyleft that applies only to changes to the
MPL-licensed files themselves (here `certifi`, used unmodified). A package offered under a
choice of licenses (`A OR B`) is allowed if one of them is.

## Added for identity (A2), pinned exactly

| Package | Version | License | Why it is needed | Alternative considered |
|---|---|---|---|---|
| argon2-cffi | 25.1.0 | MIT | argon2id password hashing (docs/identity-plan.md, section 2) | stdlib `hashlib.scrypt`: in the standard library, but not argon2id, which the plan requires and OWASP ranks first. `passlib`: unmaintained |
| cryptography | 50.0.2 | Apache-2.0 OR BSD-3-Clause | Ed25519 keys for signing access tokens (section 6) | None comparable: the standard library has no Ed25519 |
| pyjwt | 2.15.1 | MIT | Encoding and verifying the signed access tokens, with the algorithm fixed to EdDSA (section 6) | A hand-written JWS on `cryptography` alone: one dependency fewer, but token parsing is where algorithm-confusion bugs live |

Their own dependencies come with them and are pinned by `uv.lock`: argon2-cffi-bindings
26.1.0 (MIT), cffi 2.1.1 (MIT-0), pycparser 3.11 (BSD-3-Clause).

## Every runtime dependency

As resolved in `uv.lock` for Python 3.11 and 3.12.

| Package | License |
|---|---|
| annotated-doc | MIT |
| annotated-types | MIT |
| anyio | MIT |
| argon2-cffi | MIT |
| argon2-cffi-bindings | MIT |
| certifi | MPL-2.0 |
| cffi | MIT-0 |
| click | BSD-3-Clause |
| cryptography | Apache-2.0 OR BSD-3-Clause |
| fastapi | MIT |
| h11 | MIT |
| httpcore | BSD-3-Clause |
| httpx | BSD-3-Clause |
| idna | BSD-3-Clause |
| opentelemetry-api | Apache-2.0 |
| pycparser | BSD-3-Clause |
| pydantic | MIT |
| pydantic-core | MIT |
| pyjwt | MIT |
| pyyaml | MIT |
| starlette | BSD-3-Clause |
| typing-extensions | PSF-2.0 |
| typing-inspection | MIT |
| uvicorn | BSD-3-Clause |

## Bundled data

### Common-password blocklist

| | |
|---|---|
| File | `src/gateway/data/common-passwords.txt` |
| Source | SecLists, `Passwords/Common-Credentials/100k-most-used-passwords-NCSC.txt` |
| URL | https://github.com/danielmiessler/SecLists/blob/1a7bb9127eca9e6ff2fc0301c597fe6e16a0cb56/Passwords/Common-Credentials/100k-most-used-passwords-NCSC.txt |
| Commit | `1a7bb9127eca9e6ff2fc0301c597fe6e16a0cb56` (2025-11-19, the last change to the file when it was taken) |
| sha256 of the source file | `c2e5696882c603b76bb67a47ee970897e5a76fc4c3f5547abe3d0ca340c576e0` (99,839 passwords, one per line, UTF-8) |
| What is stored | Only its entries of 12 or more characters, in the source's order: the shorter ones are already refused by the length rule. 1,212 entries; 1,197 once case is ignored |
| sha256 of the stored file | `4a5f7c5ddf4ae43e7207e8c810bf01d1402fa088ead34ffddd229555ac3904ed` |
| License | MIT (SecLists) |
| Attribution | Copyright (c) 2018 Daniel Miessler; the license text is in `src/gateway/data/LICENSE-SecLists.txt` |
| Provenance | The list is the UK National Cyber Security Centre's publication of the 100,000 most common passwords in Have I Been Pwned's breach corpus. SecLists distributes it under its MIT license and does not state the upstream terms; we rely on SecLists' license, and record the origin here |

**Public data under the rule 7 exemption** (CLAUDE.md, hard rule 7): a public dataset with its
license attribution. It is not request traffic, not user data and not anyone's credentials:
the passwords are not linked to any account, and the gateway only reads the list to refuse
them.

It replaced SecLists' `10k-most-common.txt` (commit `913b3273`), of whose 10,001 entries only
10 had 12 or more characters.

## Test data (not shipped)

### OpenAI chat-completions wire examples

| | |
|---|---|
| Files | `tests/fixtures/openai_wire/chat_completions.json`, `tests/fixtures/openai_wire/LICENSE-openai-openapi.txt` |
| Source | OpenAI's published API description, `openapi.json` in `github.com/openai/openai-openapi` |
| URL | https://github.com/openai/openai-openapi/blob/234829e2b634b8fb159df7fcddbffad204173ffd/openapi.json |
| Commit | `234829e2b634b8fb159df7fcddbffad204173ffd` (2026-10-07, at least two weeks old when taken) |
| sha256 of the source file | `dde9abb7f7ca1e260fa05b4828984851e77f35ab89f60d0f7bbc1f44859f4724` |
| What is stored | Only the documented examples "Default", "Streaming" and "Functions" of `POST /chat/completions` (request bodies, responses, stream chunks), unchanged, and the `required` field lists of the response, chunk, message, tool-call, usage and error schemas |
| License | MIT, Copyright (c) OpenAI; the license text is beside the fixture |
| Used by | `tests/test_chat_api.py`, which checks the gateway's chat endpoint against them (docs/31) |

## Considered and not added

| Package | Why not | Evidence |
|---|---|---|
| `openai` 3.26.0 (dev only, for testing the chat endpoint with the official SDK) | Its required `httpx2` and `httpcore2` could not be shown to be canonical: no PyPI provenance attestation for either, no `httpcore2` repository, not mentioned by `encode/httpx`, and released days apart. The rule for this case is not to add it, so the OpenAI wire format is tested with plain `httpx` against the documented examples above | `docs/provider-plan.md`, section 8 (metadata, hashes, attestation and repository checks of 2026-10-22) |
