# Licenses of what the gateway ships with

Every runtime dependency of the `gateway` package, every package built into the dashboard and
the admin console that the gateway serves, and every piece of third-party data the repository
bundles, with its license. `tests/test_licenses.py` keeps this file honest: it walks the
installed runtime dependencies, fails on any license that is not allowed below, fails if a
package is missing from the table or listed with another license, checks the pinned versions
against `pyproject.toml`, checks the dashboard's packages against `frontend/package-lock.json`,
and checks each bundled file against its recorded sha256. Development tools (pytest, ruff, mypy,
matplotlib, Vite, TypeScript, Playwright, ...) are not shipped and are not listed.

The project's own code is under the [MIT license](../LICENSE).

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

## Added for scale-out PR 2 (Valkey counters), pinned exactly

| Package | Version | License | Why it is needed | Alternative considered |
|---|---|---|---|---|
| redis | 8.1.0 | MIT | The client for the shared counters in Valkey, through `redis.asyncio` (docs/scale-out-plan.md, PR 2) | A RESP client written here: one dependency fewer, but a protocol client is where framing and reconnection bugs live; `valkey-glide`: a Rust core, a larger supply chain |

Provenance, checked 2026-10-29: author "Redis Inc." `<oss@redis.com>`; project
`github.com/redis/redis-py` (since 2009, not a fork); uploaded 2026-07-30 (three months old);
wheel sha256 `a4fe1aac3d3b3cc791d4b3d5931c5a956045dc951ee74d1c913ee3ac4d2ee9fb`, sdist
`6e1a19beef9225c83efd689c7e6b7da2d5215b1f42cd13b7fc3714d0a09c7b25` (the same in `uv.lock`).
**No PyPI provenance attestation**: the project publishes with an API token, not trusted
publishing. In its place: release `v8.1.0` was published at 08:50:17Z, the repository's
`pypi-publish.yaml` run for it succeeded from 08:50:19Z, PyPI received the wheel at 08:50:58Z,
and **all 121 files of the wheel are byte-identical to the repository at tag `v8.1.0` (commit
`e013126df18e476fbb8b8215f040f39bd3242683`)**. The tag's `pyproject.toml` declares the same
dependencies as the metadata. Added with the operator's agreement given that evidence.

Its one dependency, `async-timeout` 5.0.1 (Apache-2.0, `aio-libs/async-timeout`, 2024-11-06), is
needed only on Python before 3.11.3; neither CI version installs it.

## Added for scale-out PR 3 (Postgres), pinned exactly

| Package | Version | License | Why it is needed | Alternative considered |
|---|---|---|---|---|
| asyncpg | 0.32.0 | Apache-2.0 | The Postgres client for the durable stores (docs/scale-out-plan.md, PR 3) | `psycopg` 3: LGPL-3.0, not on the allowed list. `pg8000`: synchronous; every store call would block the event loop or go to a thread |

Provenance, checked 2026-10-29: author MagicStack Inc; project `github.com/MagicStack/asyncpg`
(since 2016, not a fork); uploaded 2026-10-06 (23 days old). **PyPI provenance attestation on
every file installed here**: published by GitHub `MagicStack/asyncpg`, workflow `release.yml`,
environment `pypi`. sha256: Windows CPython 3.12 wheel
`cd7157a86817730c3239bc687abf8186a471525d695e225c187b9a523a808a98`, Windows 3.11
`6a1e671e67f4b0bef3c03f37a896d61706f769a83922c119070f1f04e415dc17`, manylinux 3.12
`54851411bee2aa51a30d0911524201fbb05f82cc0f7c248b140203db637c723d`, manylinux 3.11
`968c570c5913b7ce0995953d7239bd2367142d1af4359f87699f7a6ca75c4382`, sdist
`45e64e56714d888330b884aad1dfb363d0bf43fb343e3d1a8968525f3bade478` (the same in `uv.lock`).
The annotated tag `v0.32.0` points at commit `d701e11a392b`, the commit the attested release run
built. In the Windows 3.12 wheel all 22 Python files equal the tag's (line endings aside) or the
`pgproto` submodule's at its pinned commit `a29a6f6`; the other 60 are compiled or stubs. Its one
dependency, `async_timeout`, applies only before Python 3.11.

## Services used in tests, CI and compose (not shipped)

| Service | Version | License | Where | Provenance |
|---|---|---|---|---|
| Valkey | `valkey/valkey:9.0.7@sha256:7e1deda01b64ece3167d370ab5be3452d29734feec22cbcbc69b3a64f6859d6d` | BSD-3-Clause | `docker-compose.valkey.yml`, the CI `valkey` job | Built and pushed by `valkey-io/valkey-container`'s CI from its `mainline` branch (the push run of 2026-10-27 00:13Z matches the tag dates); tags are rebuilt daily, so it is pinned by digest. **An exception to the two-week rule, agreed 2026-10-29:** every current tag was two days old, a security release on all lines (GHSA-f4q9-j22m-xqq9), and the newest older tag, 9.0.6, lacks the fix |
| PostgreSQL | `postgres:18.6-trixie@sha256:74935e72241653ca55e0414067e6d8763aceb8a810eb51b452253ec3dcfc4336` | PostgreSQL License | `docker-compose.postgres.yml`, the CI `postgres` job | The Docker Official Image, built from `docker-library/postgres`; this digest is the build of 2026-10-07 recorded in `docker-library/repo-info` (22 days old when pinned, PostgreSQL package `18.6-1.pgdg13+2`). The current tags were rebuilt 2026-10-28 for Debian base layers with the same PostgreSQL; the older digest was chosen to meet the two-week rule (agreed 2026-10-29) |

## Images the Dockerfile builds from (not published by this repository)

`docker compose up --build` builds an image on the user's machine from these. **They are not
pinned by version or digest**, so the image changes when they do.

| Image | Used for | License of what it adds | In the final image |
|---|---|---|---|
| `node:20-slim` | building the dashboard (`npm ci`, `npm run build`) | Node.js: MIT; the image's Debian packages carry their own licenses | No: only the built files are copied out |
| `python:3.12-slim` | the base of the gateway's image | CPython: PSF-2.0; the image's Debian packages carry their own licenses | Yes |
| `ghcr.io/astral-sh/uv:latest` | the `uv` binary that installs `uv.lock` | uv: MIT OR Apache-2.0 | Yes (`/usr/local/bin/uv`) |

## Every runtime dependency

As resolved in `uv.lock` for Python 3.11 and 3.12 (`async-timeout`, Apache-2.0, only before 3.11.3).

| Package | License |
|---|---|
| annotated-doc | MIT |
| annotated-types | MIT |
| anyio | MIT |
| argon2-cffi | MIT |
| asyncpg | Apache-2.0 |
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
| redis | MIT |
| starlette | BSD-3-Clause |
| typing-extensions | PSF-2.0 |
| typing-inspection | MIT |
| uvicorn | BSD-3-Clause |

## Built into the dashboard and the console

`npm run build` bundles these into `src/gateway/dashboard/` (not tracked), which the gateway
serves at `/dashboard` and the Docker image ships. They are the only packages of
`frontend/package-lock.json` that are not development-only.

| Package | Version | License |
|---|---|---|
| react | 19.3.0 | MIT |
| react-dom | 19.3.0 | MIT |
| scheduler | 0.28.0 | MIT |
| uplot | 1.6.32 | MIT |

No font, icon set or other asset of a third party is bundled; the console's icon is an inline
SVG written here, and so is the dashboard's.

## Development and test packages of the frontend

Packages the console's UI work (docs/52) adds to `frontend/package.json`'s development
dependencies, and what they bring with them. None is bundled into what the gateway serves; they
run in the test suites only. `tests/test_licenses.py` checks each row against
`frontend/package-lock.json` (version, licence, development-only) and fails on a direct
development dependency that is neither here nor among those the frontend had before this table.

| Package | Version | License |
|---|---|---|
| @axe-core/playwright | 4.14.0 | MPL-2.0 |
| axe-core | 4.14.0 | MPL-2.0 |

`@axe-core/playwright` runs the axe accessibility rules in the e2e suites (docs/52, Q6; docs/53);
`axe-core` is the rule engine it injects into the page under test. Both are used unmodified, so
MPL-2.0's file-level terms ask nothing of this repository.

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

## Benchmark data (in the repository, not shipped)

### MMLU sample

| | |
|---|---|
| Files | `benchmarks/quality/mmlu_sample.json`, `benchmarks/quality/LICENSE-MMLU.txt` |
| Source | MMLU, "Measuring Massive Multitask Language Understanding", Hendrycks et al., 2021 (arXiv:2009.03300); the copy `cais/mmlu` on Hugging Face, config `all`, split `test` |
| What is stored | 100 questions drawn at random, with their answers |
| sha256 of the stored files | `f6e1f66693df6d4328396c451361e1c2a01db15641cb6f542cde293736c1b7ca` (`mmlu_sample.json`), `5d841b9eee8bf0d721359b79fbae956a8b426d3139bfefee32149c7d4f2a1a9c` (`LICENSE-MMLU.txt`) |
| License | MIT, Copyright (c) 2020 Dan Hendrycks; the license text is beside the data. Checked on 2026-10-09 in the authors' repository and on the dataset card |
| Used by | `benchmarks/quality_eval.py` and note 14; exempt from hard rule 7 as a public dataset with its attribution, not request traffic |

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
