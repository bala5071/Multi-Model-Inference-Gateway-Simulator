# 01 — Cost model

Step 1 adds the model profiles and a pure cost function. Code: `src/gateway/profiles.py`,
`src/gateway/cost_model.py`. Tests: `tests/test_cost_model.py`.

## The profiles are calibrated; prices and capacities are placeholders

The timing parameters are the **median of six calibration runs of the real models** on one
laptop with Ollama, the 8B model partly on the CPU (`docs/13-calibration.md`,
`benchmarks/calibration/calibrated_profiles.json`). The range is the lowest and highest value
over the six runs; a parameter whose range is more than 25% of its median is **unstable**: it
did not repeat. `tests/test_profiles.py` fails if `profiles.py` and the calibration file
disagree.

| Field                       | economy | standard | premium |
|-----------------------------|--------:|---------:|--------:|
| `name`                      | `llama-3.2-1b` | `llama-3.2-3b` | `llama-3.1-8b` |
| `prefill_ms_per_token`      | 0.1960 (0.1821 to 0.2141) | 0.3754 (0.3521 to 0.4221) | 0.8623 (0.7637 to 0.9577) |
| `decode_base_ms`            | 6.711 (6.481 to 6.741) | 15.298 (14.747 to 22.171) **unstable** | 45.138 (42.767 to 48.328) |
| `decode_ms_per_extra_seq`   | 2.676 (2.092 to 3.686) **unstable** | 4.133 (1.705 to 4.905) **unstable** | 24.760 (7.744 to 42.860) **unstable** |
| `per_request_overhead_ms`   | 90.1 (75.4 to 113.7) **unstable** | 88.4 (65.5 to 126.1) **unstable** | 161.9 (42.0 to 236.3) **unstable** |
| `max_batch_size`            | 4 | 4 | 4 |
| `price_per_1k_input_tokens` USD  | 0.0001 placeholder | 0.0003 placeholder | 0.0010 placeholder |
| `price_per_1k_output_tokens` USD | 0.0002 placeholder | 0.0006 placeholder | 0.0020 placeholder |
| `kv_capacity_tokens`        | 65,536 placeholder | 32,768 placeholder | 16,384 placeholder |

- **Premium is hardware-limited**: the 8B model did not fit the 8 GB GPU and ran 5% on the
  CPU, so its timings are of that laptop's limits as much as of the model.
- **`max_batch_size` is 4 on every tier**, the most requests the calibration sent together
  (`MEASURED_MAX_BATCH_SIZE`). Nothing the simulator reports batches more, so nothing
  extrapolates the fitted cost per extra sequence past what was measured. How these models
  behave with larger batches is not known.
- **Prices and `kv_capacity_tokens` were never measured.** They are
  `PLACEHOLDER_PRICE_PER_1K_INPUT_TOKENS`, `PLACEHOLDER_PRICE_PER_1K_OUTPUT_TOKENS` and
  `PLACEHOLDER_KV_CAPACITY_TOKENS`: operator-supplied, not data. They exist because the ledger
  and the KV accounting need a number. No document reports a cost, saving or ratio from them.
- Every result built on these profiles is a **simulated** result: this repo's scheduler with
  step costs measured on one machine, not a measurement of a model server.

`tests/helpers.py` keeps `TEST_PROFILES`: three hand-made fixtures, not data, with round
numbers so unit tests can check timings by hand. Only tests may import them
(`tests/test_profiles.py` enforces it), and no document quotes a result from them.

## Formulas

For a profile `p`, `n_in` new prefill tokens, `n_out` output tokens and batch size `b`:

```
prefill_ms     = n_in * p.prefill_ms_per_token
decode_step_ms = p.decode_base_ms + p.decode_ms_per_extra_seq * (b - 1)
cost_usd       = (n_in * p.price_per_1k_input_tokens
                  + n_out * p.price_per_1k_output_tokens) / 1000
overhead_ms    = p.per_request_overhead_ms
```

`estimate_cost` returns these values as a `CostEstimate`.

## Per-request overhead

Added after calibration (`docs/13-calibration.md`), which found that real servers spend a
fixed time on every request before its first token, whatever the prompt length, and that
the simulator had no term for it.

- **It is calibrated and unstable on every tier**: 90, 88 and 162 ms, ranging over 75 to 114,
  65 to 126 and 42 to 236 ms across the six runs (`docs/13-calibration.md`). So is the cost
  per extra sequence. Treat a result that depends on either as resting on a value that did
  not repeat.
- **It delays one request and does not occupy the model.** The workers let it pass between
  a request's arrival and its joining the queue; other requests are served meanwhile, and
  the overheads of requests that arrive together overlap. A request served alone gets its
  first token after `overhead_ms + prefill_ms + decode_step_ms`.
- **It counts as queue wait** (`start_ms - enqueue_ms`), and so is inside TTFT and total
  latency. Inter-token latency and throughput at saturation do not change.
- **It is not billed.** `cost_usd` is the same with and without it.
- A request still in its overhead counts as waiting for the queue limit of note 10.
- Whether the real overhead blocks the model or not was not measured. Treating it as
  non-blocking is an assumption: it is the kind of work (request handling, templating,
  tokenising, slot set-up) that happens outside the batched decode loop.

## Assumptions

- **Prefill is linear in new tokens and independent of batch size.** Real prefill has
  attention terms that grow with context length and is affected by concurrent work. Ignored.
- **A decode step is linear in batch size.** One step produces one token for every sequence
  in the batch and takes `decode_step_ms` for the whole batch. Context length does not affect
  step time.
- **The function returns per-step time, not total decode time.** Under continuous batching
  the batch size changes while a request is decoding, so the batcher must sum step times
  itself. At a constant batch size the total is `n_out * decode_step_ms`.
- **`b` counts the request itself** and must be in `1..max_batch_size`; anything else raises
  `ValueError` rather than being clamped.
- **Only new prefill tokens are billed as input.** Tokens served from the prefix cache cost
  nothing in this model. Whether cached tokens should be billed at a discount instead is a
  pricing decision left for the prefix-cache step.
- **Price does not depend on batch size or latency.**
- **`kv_capacity_tokens` and `max_batch_size` are carried on the profile but only
  `max_batch_size` is used here.** The KV capacity, a placeholder, is for the prefix-cache
  step.
- **Money is a `float`.** Good enough for a simulator; this is not a billing system.

## Output length

`OutputLengthGenerator(seed, min_tokens=16, max_tokens=256).length_for(prompt)` returns
`min_tokens + sha256(f"{seed}:" + prompt) mod (max_tokens - min_tokens + 1)`, using the first
8 bytes of the digest.

- It is a hash, not a stateful RNG: the length for a prompt does not depend on which other
  prompts were asked about, or in what order. That keeps simulations reproducible however
  asyncio schedules the tasks.
- It uses `hashlib`, not the builtin `hash()`, which changes between processes. A test pins
  concrete values to guard this.
- Lengths are roughly uniform over the range. Real output lengths are not uniform and do
  depend on the prompt's content; this stand-in only guarantees "same seed and prompt, same
  length". The modulo bias is negligible for ranges this small.

## No Clock here

Nothing in this step waits or reads the time, so nothing takes a `Clock`. The functions
return durations; the batcher that spends them will take the clock.
