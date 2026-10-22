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
- **Policy:** 12 to 1024 characters; not on the blocklist; not containing the username (if
  it has 3 or more characters, so that "al" does not refuse every password with "al" in it)
  or the product's name ("gateway"); every comparison ignores case. No composition rules. The
  error says which rule was broken, never the password.
- **The blocklist is the 100,000 passwords the UK NCSC published from Have I Been Pwned**, as
  distributed by SecLists (MIT; source, commit, both sha256s, attribution and provenance in
  `docs/licenses.md`, as public data under the rule 7 exemption). Only its 1,212 entries of 12
  or more characters are stored, since the length rule refuses the others anyway; that is
  1,197 distinct passwords once case is ignored, every one of which the length rule alone
  would allow. A test pins those numbers. (The first version used SecLists' 10,000-entry list,
  of which only 10 entries were long enough to matter.)
- **Dependencies:** argon2-cffi 25.1.0, cryptography 50.0.2 and PyJWT 2.15.1, pinned exactly.
  The last two are not used until the token step; they are added now, as the plan's step 1
  says, so the license record is complete in one place. `tests/test_licenses.py` walks every
  runtime dependency, fails on a license outside the allowlist in `docs/licenses.md`, and
  fails if that file's table or pinned versions drift.

## Configurable parameters, and upgrades

Memory, iterations and lanes come from `GATEWAY_ARGON2_MEMORY_KIB`, `GATEWAY_ARGON2_TIME_COST`
and `GATEWAY_ARGON2_PARALLELISM` (read by the wiring, parsed by `parameters_from`), defaulting
to the current setting. argon2 stores the parameters in each hash, so a setting can change at
any time: `Passwords.check` verifies against whatever the stored hash says, and if the password
is right and the hash was made with other parameters, it returns a new hash made with the
current ones, for the login step (step 4) to store. Tested: a hash made with other parameters
verifies, is upgraded, and the upgrade needs no further upgrade; a wrong password is never
upgraded.

## What hashing costs here

Measured with `uv run python -m benchmarks.password_hashing` (each setting in a process of its
own: 10 hashes after one warm-up, then 8 logins submitted together to the 2-thread pool), run
three times, on the tree of the commit that added this text (parent `06f68ca`). Machine: Intel
Core Ultra 7 155H, 22 logical cores, 15.4 GB RAM, Windows 11 (10.0.26200), Python 3.12.10,
argon2-cffi 25.1.0. "Peak memory" is the process's peak working set above what it used before
any hashing, which is reached while two hashes run at once. Nearest-rank percentiles; ranges
over the three runs. One laptop, one day.

| Setting (memory, iterations, lanes) | One hash, p50 | One hash, max | 8 logins at once: p50 | 8 at once: the last | Peak memory, 2 threads |
|---|---|---|---|---|---|
| 19 MiB, 2, 1 (current; OWASP's first) | 18.9 to 19.9 ms | 21.1 to 22.3 ms | 45.5 to 50.3 ms | 90.0 to 99.8 ms | 37.6 to 37.7 MiB |
| 46 MiB, 1, 1 (OWASP's equivalent) | 31.3 to 31.7 ms | 33.4 to 33.8 ms | 72.9 to 74.9 ms | 143.3 to 148.7 ms | 91.5 to 91.6 MiB |
| 64 MiB, 3, 1 (RFC 9106's second recommendation, one lane) | 107.3 to 108.1 ms | 109.7 to 111.5 ms | 237.8 to 249.6 ms | 477.4 to 494.5 ms | 126.5 to 126.8 MiB |
| 128 MiB, 3, 1 | 227.2 to 232.9 ms | 230.9 to 242.8 ms | 531.1 to 552.2 ms | 1,064 to 1,093 ms | 254.3 to 254.5 MiB |

- **Memory is two hashes' worth:** with 2 threads, the peak is twice the setting, as expected.
- **Logins queue behind the 2 threads:** 8 at once are served two at a time, so the last waits
  about four hash times.
- **The event loop kept running in every case:** the longest gap a 1 ms ticker saw was 15 to
  17 ms, the Windows timer's resolution (note 06); so no stall was longer than that, which is
  less than one hash at any setting.

**Recommendation: 64 MiB, 3 iterations, 1 lane.** It is RFC 9106's second recommended setting
(with one lane instead of four, since the pool already runs two hashes in parallel), about five
times the work and over three times the memory of the current setting, so an offline attack on
a stolen hash costs that much more. A login takes about 110 ms here, 8 logins at once finish
within half a second, and the two threads need about 127 MiB at peak. 128 MiB doubles all of
that again (a burst of 8 takes over a second) for a smaller gain. **The default is not changed
in this commit:** it stays at 19 MiB, 2, 1 until the recommendation is accepted, and setting
`GATEWAY_ARGON2_*` changes it now; existing hashes are upgraded at each user's next login
either way.
