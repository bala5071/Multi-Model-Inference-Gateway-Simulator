# 04 — Prefix cache

Step 4 adds a per-model prefix cache and wires it into the continuous batching worker. A
request whose prompt starts with something the model has already processed is prefilled,
and billed, only for the rest.

Code: `prefix_cache.py`, `tokenizer.py`, changes in `continuous.py`, `backends/`,
`schema.py`.
Tests: `test_prefix_cache.py`, `test_prefix_reuse.py`, `test_tokenizer.py`.

It is a **stand-in**: it stores no tensors. It tracks which token prefixes would be in the
model's KV cache, how many tokens they occupy, and which may be evicted. No performance
numbers are claimed in this note; the worked example below is arithmetic on the test
profile, checked by a test.

## The radix tree

`PrefixCache` is a radix tree (compressed trie) over token ids. Each node holds a run of
tokens; a path from the root spells a cached prefix. Two prompts with the same first 40
tokens share one 40-token node and each add only their own tail, so a shared prefix is
stored, and counted against capacity, once.

| Operation         | What it does                                                             |
|-------------------|--------------------------------------------------------------------------|
| `match(tokens)`   | Length of the longest cached prefix. Read-only: no LRU update, no stats. |
| `insert(tokens)`  | Stores what is missing; returns the number of new tokens.                |
| `acquire(tokens)` | `match`, then pins the matched prefix, marks it used, counts a lookup.   |
| `release(lease)`  | Unpins.                                                                  |
| `abandon(lease)`  | Unpins and removes the lookup from the stats.                            |
| `evict(n)`        | Evicts LRU unpinned leaves until at least `n` tokens are free.           |

Matching is exact to the token. If a match ends partway along a node, `acquire` and `insert`
split that node there, so a lease or a new branch always attaches at a node boundary.

### Eviction

- **Least recently used, at leaf level.** The victim is always a leaf: the unpinned leaf
  whose last use is oldest. Removing a leaf can turn its parent into a leaf, which is then
  eligible in turn. A shared prefix therefore outlives the tails that hang off it.
- **"Used" means `acquire` or `insert`**, which stamp the whole path from the node to the
  root. Recency is a counter of operations, not a clock reading, so the cache needs no
  `Clock` and behaves identically under `SimClock` and `WallClock`.
- **Whole nodes are evicted**, so `evict(n)` may free more than `n`.
- **Ref counting.** A lease increments a count on every node of its matched path and
  `release` decrements it. Nodes with a non-zero count are never evicted. Since a lease pins
  its whole path, an unpinned node never has a pinned descendant.
- **If everything is pinned**, `insert` stores as long a prefix as fits, possibly nothing.

### Stats

`cache.stats` is a `CacheStats` snapshot per cache, hence per model:

- `hit_rate = hits / lookups`, where a hit is a lookup that matched at least one token;
- `token_hit_rate = cached_tokens / prompt_tokens` over all lookups;
- `evicted_nodes`, `evicted_tokens`.

Both rates are 0.0 before any lookup. Each `RequestResult` also carries `cached_tokens`.

## Integration with the worker

`ContinuousBatchingWorker(..., prefix_cache=PrefixCache(profile.kv_capacity_tokens))`.
Without the argument the worker behaves exactly as in step 3, which keeps the step 3
comparison reproducible and allows with/without-cache comparisons.
`build_continuous_registry` gives every model its own cache by default.

At admission:

1. `acquire` the prompt's tokens: `cached` tokens match and are pinned.
2. Reserve the request's private KV tokens: `(tokens_in - cached) + tokens_out`.
3. If the model's capacity would be exceeded, evict unpinned cached prefixes to make room.
   If even that is not enough, give the lease back and wait for a running request to finish.
4. Prefill takes `(tokens_in - cached) * prefill_ms_per_token`, and the input price is
   charged on `tokens_in - cached` tokens.

At finish: `insert` the prompt's tokens, release the lease, drop the reservation.

**One capacity for both.** `kv_capacity_tokens` (a placeholder in the default profiles,
operator-supplied, not data: KV capacity was never measured) bounds *cached tokens + tokens reserved by
running requests*. The cache is not a second budget on top of the KV memory; it is the idle
part of it. Consequences: cached prefixes are evicted to admit new work; a prefix in use
cannot be evicted, so a request may wait instead; and a cache hit makes a request smaller,
so more requests fit. A finished request's prompt always fits in the cache without eviction,
because it takes over space the request was already holding.

### Worked example

On the hand-made test profile (`EXACT` in `tests/helpers.py`: a fixture, not data; 0.5 ms
prefill per token, 4 ms decode step, 2 output tokens), from
`test_shared_system_prompt_cuts_prefill_time_and_cost_exactly`. It checks the mechanism; it
is not a result:

| Request | Prompt                          | Cached | Prefill          | Served     |
|---------|---------------------------------|-------:|------------------|------------|
| r1      | 40 system + 10 user tokens      |      0 | 50 x 0.5 = 25 ms | t=0 to 33  |
| r2      | same 40 system + 6 other tokens |     40 | 6 x 0.5 = 3 ms   | t=33 to 44 |

Without the cache r2 prefills 46 tokens (23 ms) and finishes at t=64.

## Tokenizer

`Tokenizer` is a protocol with one method, `encode(text) -> tuple[int, ...]`.
`SimulatedBackend` takes one and returns the prompt's token ids in `Generation`
(`tokens_in` is now their count). A real tokenizer can be passed in instead.

`WordTokenizer`, the default, yields one token per whitespace-separated word. A word's id
is a 64-bit BLAKE2 hash of its text: stable across processes, no vocabulary to build, and
distinct words collide with negligible probability. It is not a word-piece tokenizer: it
does not split words, and case and punctuation make different tokens. Token counts are the
same as in steps 2 and 3.

## Relation to real systems

This summarises the published designs as I understand them; check the projects' current
documentation before relying on a detail.

**SGLang RadixAttention** is the direct model for this step. It keeps a radix tree over
token sequences whose nodes map to KV-cache tensors, reuses the longest matching prefix
automatically, evicts least-recently-used leaves when memory runs out, and uses reference
counts so that nodes used by running requests are not evicted. Same structure and policy
here.

**vLLM automatic prefix caching** reaches a similar result differently. KV memory is
managed in fixed-size blocks of tokens, and a block is identified by a hash of the tokens in
it together with everything before it. A new request reuses every leading block whose hash
is already present. Blocks are reference-counted and unreferenced ones are reclaimed when
memory is needed. There is no tree; the hash chain plays that role.

### Where this stand-in differs

- **No tensors and no memory layout.** One token is one unit of capacity. Real memory per
  token depends on layer count, heads, dimensions and dtype, and is allocated in pages or
  blocks.
- **Token-exact matching.** vLLM reuses whole blocks only, so a shared prefix is rounded
  down to a block boundary. Here every matched token counts.
- **A prefix is reusable only after its request has finished.** `insert` happens at finish,
  as specified for this step. Two requests with the same system prompt admitted together
  into a cold cache both pay full prefill
  (`test_prefix_becomes_reusable_only_after_its_request_is_served`). Real systems can share
  a prefix as soon as it has been computed, including within one batch.
- **Generated tokens are not cached.** Only the prompt is inserted, because the simulator
  generates no token ids for the output. Real systems keep the KV entries for the output
  too, which is what makes the next turn of a chat cheap.
- **A full match costs zero prefill.** A real engine must still run at least the last
  prompt token to get logits for the first output token.
- **A hit is free.** No lookup, copy or bookkeeping time is modelled.
- **No cache-aware scheduling.** Admission stays first come, first served. SGLang can order
  the queue to favour requests with longer cached prefixes.
- **Cancelled requests insert nothing**, although their prefill was "computed".
- **Eviction scans all nodes** for the LRU leaf. Fine for a simulator; a real cache would
  keep an ordered structure. Nodes left with a single child after an eviction are not merged
  back.
- **Cached tokens are billed at zero.** Real price lists usually charge cached input at a
  discount rather than nothing. This is a pricing choice, still open from step 1.
- **Output length is known at admission**, as in step 3, so the reservation is exact.
