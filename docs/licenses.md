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
| Source | SecLists, `Passwords/Common-Credentials/10k-most-common.txt` |
| URL | https://github.com/danielmiessler/SecLists/blob/913b327317496d062bcc7cace524aaad8a693be2/Passwords/Common-Credentials/10k-most-common.txt |
| Commit | `913b327317496d062bcc7cace524aaad8a693be2` (2026-09-08, the last change to the file when it was taken) |
| sha256 of the file | `68782d6a4a19a4768d5f15dd66bd534e7a33055cc755411e33f16d18c50fdcce` |
| License | MIT |
| Attribution | Copyright (c) 2018 Daniel Miessler; the license text is in `src/gateway/data/LICENSE-SecLists.txt` |
| Contents | 10,001 passwords, one per line, ASCII, at most 18 characters |

**Public data under the rule 7 exemption** (CLAUDE.md, hard rule 7): a public dataset with its
license attribution. It is not request traffic, not user data and not anyone's credentials;
the gateway only reads it to refuse these passwords.

Only 10 of its 10,001 entries have 12 or more characters, so with the 12-character minimum it
refuses very little (docs/24). SecLists has larger lists under the same license (100,000
entries, about 800 KB; 1,000,000, about 8.5 MB) if more is wanted.
