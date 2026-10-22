# 24 — Password hashing, its runners, and the password policy

Step 1 of the identity plan (`docs/identity-plan.md`). It adds what later steps build on and
nothing a user can reach yet: the three dependencies, the argon2id hasher, the two ways it runs,
the password policy with its blocklist, and what hashing costs on this machine. No endpoint,
table or setting changed.

Code: `src/gateway/passwords.py`, `src/gateway/data/` (the blocklist and its license).
Tests: `tests/test_passwords.py`, `tests/test_licenses.py`. Benchmark:
`benchmarks/password_hashing.py`. Licenses: `docs/licenses.md`.

## What was decided

- **argon2id at OWASP's first configuration:** 19 MiB of memory, 2 iterations, 1 lane, a
  16-byte random salt per hash, 32-byte output, stored in the standard PHC string
  (`$argon2id$v=19$m=19456,t=2,p=1$...`). `needs_rehash` tells a later login step when a hash
  was made with other parameters. Verification is argon2-cffi's; a wrong password, an empty
  string or an unreadable hash is `False`, never an exception that could carry the password.
- **Two runners, chosen by the wiring** (plan section 2):
  - `InlineHashRunner` for `SimClock`: the hash runs synchronously with no `await` inside, so
    the loop is busy and simulated time cannot move.
    `test_inline_hashing_does_not_let_simulated_time_move` runs two full-cost operations with
    a sleeper waiting at 10 ms and finds the clock still at 0.
  - `ThreadHashRunner` for the `WallClock` server: a pool of 2 threads.
    `test_a_thread_would_let_simulated_time_jump_which_is_why_simclock_hashes_inline` shows the
    hazard it must not be used for: work on a thread that can only finish once simulated time
    has moved does finish, because the idle loop let the `SimClock` jump to 10 ms.
    `test_on_a_thread_the_event_loop_keeps_serving_while_hashing` and
    `test_the_pool_runs_at_most_max_workers_hashes_at_once` cover the rest.
- **Policy:** 12 to 1024 characters, not on the blocklist (compared without case), no
  composition rules. The error says which rule was broken, never the password.
- **The blocklist is SecLists' 10,000 most common passwords** (MIT; source, commit, sha256 and
  attribution in `docs/licenses.md`, as public data under the rule 7 exemption). **Only 10 of
  its 10,001 entries have 12 or more characters**, so after the length rule it refuses almost
  nothing; a test pins both numbers. A larger SecLists list (100,000 entries, about 800 KB)
  would refuse more; that is a decision for review, not made here.
- **Dependencies:** argon2-cffi 25.1.0, cryptography 50.0.2 and PyJWT 2.15.1, pinned exactly.
  The last two are not used until the token step; they are added now, as the plan's step 1
  says, so the license record is complete in one place. `tests/test_licenses.py` walks every
  runtime dependency, fails on a license outside the allowlist in `docs/licenses.md`, and
  fails if that file's table or pinned versions drift.

## What hashing costs here

Measured with `uv run python -m benchmarks.password_hashing` (20 hashes and 20 verifies after
one warm-up, then 8 logins submitted together to the 2-thread pool), run three times, on the
tree of the commit that added this note (parent `8f419eb`). Machine: Intel Core Ultra 7 155H,
22 logical cores, 15.4 GB RAM, Windows 11 (10.0.26200), Python 3.12.10, argon2-cffi 25.1.0.
Nearest-rank percentiles; ranges over the three runs. One laptop, one day.

| What | n | mean ms | p50 ms | p99 ms | max ms |
|---|---|---|---|---|---|
| hash, one at a time | 20 | 19.7 to 21.0 | 19.5 to 20.9 | 21.3 to 23.9 | 21.3 to 23.9 |
| verify, one at a time | 20 | 19.8 to 20.0 | 19.5 to 19.9 | 21.9 to 23.5 | 21.9 to 23.5 |
| a login when 8 arrive at once (2 threads) | 8 | 54.5 to 55.8 | 44.8 to 46.7 | 89.4 to 92.3 | 89.4 to 92.3 |

- **One verify costs about 20 ms of one core** and 19 MiB of memory while it runs; the pool of 2
  bounds a burst to 2 cores and about 38 MiB. Eight logins at once are served two at a time,
  so the last waits about four hash times (about 90 ms).
- **The event loop kept running during the logins:** the longest gap a 1 ms ticker saw was
  14.4 to 15.6 ms. That is the Windows timer's resolution (a 1 ms sleep takes about 15 ms
  there, note 06), so the measurement can only say no stall was longer than that, which is
  less than one 20 ms hash. Hashing on the loop would stall it for the whole hash, every
  time.
