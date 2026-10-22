# 23 — Docs for the current server, and a README that CI runs

Steps 5 and 6 of the platform plan (`docs/platform-plan.md`), combined: step 2 had already
done what step 5 listed (load control, simulated live mode, the demo, the Playground and the
load generator with keys) and step 4 the e2e specs and the pictures. What was left of step 5
was one browser test.

## What was decided

- **The README's Quickstart is a script CI runs.** Its shell blocks marked
  `<!-- quickstart -->` are run in order, exactly as written, by
  `scripts/readme_quickstart.py` in the `readme-quickstart` CI job: a throwaway pepper in a
  git-ignored `gateway.env`, the demo applications, a key from `scripts/manage.py`, the
  server, one request with `Authorization: Bearer`, a restart, and the usage read back. The
  runner then checks that the request was served by the premium model and that the usage
  (one request) survived the restart. If a README command stops working, CI fails.
  - The runner isolates what the README would touch: a temporary data folder for the
    database, no inherited `GATEWAY_*` variables, and it refuses to overwrite an existing
    `gateway.env` and deletes the one it made.
  - It refuses to start if something already answers on the port: the development machine
    had other gateway servers on 8000, and the README's health check would have talked to
    one of them. `--port` substitutes another port in the commands, for local runs only.
  - Run on the development machine (`--port 8123`, Git Bash on Windows 11) on the tree of the
    commit that added this note, it passed. That run found that `manage.py` printed the key
    with `\r\n` on Windows, which `KEY=$(...)` in bash keeps as part of the key; it now
    prints a bare `\n`.
- **Every curl example is for the current server**: keys, no `tenant_id`, usage at
  `/v1/applications/me/usage`. Older notes keep their examples, under an "updated by" header
  that says what changed (notes 06, 07, 08, 09, 10, 11, 12, 15, 16, 17, 18).
- **The report says "application"** in the four sentences that said "tenant" (three of them
  reach `docs/REPORT.md`; the fourth is printed only without quality results). The
  `REPORT.md` sum in the protected baseline was updated through `--update`, and no other sum
  changed.
- **The known gaps are stated the same way** in the README ("Known gaps") and in the plan
  (section 10): unauthenticated monitoring and load control, `127.0.0.1` only, one process
  per database, no pepper rotation without downtime.
- **One browser test was added**: the Playground sends a request as an application in
  simulated live mode, without an `Authorization` header, and shows the answer.
- **CI lists skipped tests**: `pytest -ra --junitxml`, the JUnit file uploaded per Python
  version, and a notice naming the skipped tests. On Linux it names only the four
  real-model tests, so the POSIX permission tests run there.
