# Release v0.1.0: docs and release preparation

The plan for the docs PR before the first versioned release. No code changes, except tests that
keep a document accurate. No tag: the tag is the operator's, once the checklist below holds.

## Scope

1. **README as the project's front page:** what it is and why, the quick start that
   `scripts/readme_quickstart.py` runs, the architecture diagram, features grouped (routing and
   providers, identity and security, scale-out, settings, alerts, console and voice),
   screenshots with captions, a measured-results table where every number carries its caveat
   and its note, what it is not, the roadmap. The operator material it held moves to
   `docs/operating.md`; the architecture to `docs/architecture.md`.
2. **`docs/README.md`** as the index of every numbered note (00 to 49), the plans with their
   current status, and the reference documents.
3. **Release notes for v0.1.0:** migrations 14 to 18 and that an older SQLite database needs
   `scripts/manage.py migrate`, the TLS 1.2 floor, the console, and every behaviour change since
   the first release (the tag `v0.2-platform-foundation`, PR #1), each with its PR.
4. **`SECURITY.md`** (reporting, scope, the threat model and its limits) and **`CONTRIBUTING.md`**
   (plan first, mutation checks, measurement).
5. **Licences:** `LICENSE` and `docs/licenses.md` checked against the dependencies; the
   dashboard's packages, the MMLU sample and the Dockerfile's base images added, the first two
   pinned by tests.

## Rules for this PR

- **Nothing claimed without a test, a measurement or a report behind it.** A number measured at
  an earlier commit is marked dated with that commit or PR; nothing is re-measured here.
- **No voice feature exists.** The feature list says so rather than describe one.

## Before the tag

What must be true before `v0.1.0` is tagged:

1. This PR is merged and `main`'s CI is green.
2. **The version name is decided.** The existing tag `v0.2-platform-foundation` sorts after
   `v0.1.0` by version, though it is older and holds less. Choose: tag `v0.1.0` anyway and say so
   in the release text, delete or rename the old tag (a destructive change on the remote), or tag
   the new release higher (for example `v0.3.0`) and change `version` in `pyproject.toml` to
   match.
3. **`pyproject.toml`'s `version` matches the tag** (it is `0.1.0` today).
4. **GitHub private vulnerability reporting is enabled** (the repository's Settings, Security),
   since `SECURITY.md` points there. It is off today.
5. **The Dockerfile's base images are pinned** by digest (`node:20-slim`, `python:3.12-slim`,
   `ghcr.io/astral-sh/uv:latest`), with the provenance check of `docs/licenses.md`, or the
   release text says they are not.
6. The release text links the v0.1.0 section of `docs/release-notes.md` and repeats its
   "Upgrading: read first".
7. Nothing in the README's measured-results table has been contradicted by a later measurement.
