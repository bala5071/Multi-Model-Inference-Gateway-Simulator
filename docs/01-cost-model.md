# 01 — Cost model

Step 1 adds the model profiles and a pure cost function. Code: `src/gateway/profiles.py`,
`src/gateway/cost_model.py`. Tests: `tests/test_cost_model.py`.

## The numbers are illustrative

**Nothing in the profiles was measured.** Prices, per-token times, batch limits and KV
capacities are invented to give the three tiers plausible, clearly different shapes. They are
not benchmarks of Llama 3.2 1B, Llama 3.2 3B or Llama 3.1 8B, and they are not anyone's real
prices. Do not quote them as performance or pricing figures. A later step calibrates them
against real runs; until then, any result built on them is a simulated result.

| Field                            | economy        | standard       | premium        |
|----------------------------------|----------------|----------------|----------------|
| `name`                           | `llama-3.2-1b` | `llama-3.2-3b` | `llama-3.1-8b` |
| `price_per_1k_input_tokens` USD  | 0.0001         | 0.0003         | 0.0010         |
| `price_per_1k_output_tokens` USD | 0.0002         | 0.0006         | 0.0020         |
| `prefill_ms_per_token`           | 0.05           | 0.15           | 0.60           |
| `decode_base_ms`                 | 6.0            | 12.0           | 30.0           |
| `decode_ms_per_extra_seq`        | 0.2            | 0.6            | 2.5            |
| `max_batch_size`                 | 32             | 16             | 8              |
| `kv_capacity_tokens`             | 65,536         | 32,768         | 16,384         |

## Why the shapes differ

The tiers are not one profile scaled by a constant. Going up a tier:

- prefill gets much slower (3x, then 4x per token), so prefix reuse is worth more;
- each decode step gets slower (2x, then 2.5x);
- adding a sequence to the batch costs relatively more (3.3%, 5%, 8.3% of the base step);
- the batch limit halves and the KV capacity halves, as if the same memory budget were
  shared with a larger model.

So batching helps the small model most and the cache matters most for the large one. That is
what makes later routing and batching decisions non-trivial.

## Formulas

For a profile `p`, `n_in` new prefill tokens, `n_out` output tokens and batch size `b`:

```
prefill_ms     = n_in * p.prefill_ms_per_token
decode_step_ms = p.decode_base_ms + p.decode_ms_per_extra_seq * (b - 1)
cost_usd       = (n_in * p.price_per_1k_input_tokens
                  + n_out * p.price_per_1k_output_tokens) / 1000
```

`estimate_cost` returns these three values as a `CostEstimate`.

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
  `max_batch_size` is used here.** The KV capacity is for the prefix-cache step.
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
