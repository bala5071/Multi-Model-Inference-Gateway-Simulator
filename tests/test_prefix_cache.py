from __future__ import annotations

import random

import pytest

from gateway.prefix_cache import CacheStats, PrefixCache, PrefixLease

A = (1, 2, 3, 4)
B = (5, 6, 7, 8)
C = (9, 10, 11, 12)
D = (13, 14, 15, 16)
E = (17, 18, 19, 20)


def _touch(cache: PrefixCache, tokens: tuple[int, ...]) -> None:
    cache.release(cache.acquire(tokens))


# --- match and insert ----------------------------------------------------------------------


def test_empty_cache_matches_nothing() -> None:
    cache = PrefixCache(100)
    assert cache.match(A) == 0
    assert cache.match(()) == 0
    assert cache.size_tokens == 0
    assert cache.node_count == 0


def test_match_returns_the_longest_cached_prefix() -> None:
    cache = PrefixCache(100)
    assert cache.insert(A) == 4

    assert cache.match(A) == 4
    assert cache.match((1, 2, 3, 4, 5, 6)) == 4  # longer than what is cached
    assert cache.match((1, 2)) == 2  # shorter: stops partway along the stored run
    assert cache.match((1, 2, 9)) == 2  # diverges partway along the stored run
    assert cache.match((9,)) == 0
    assert cache.match(()) == 0


def test_match_changes_nothing() -> None:
    cache = PrefixCache(100)
    cache.insert(A)
    cache.match((1, 2, 9))
    assert cache.node_count == 1  # no split
    assert cache.stats == CacheStats()  # no lookup counted


def test_inserting_what_is_already_cached_stores_nothing_new() -> None:
    cache = PrefixCache(100)
    cache.insert(A)
    assert cache.insert(A) == 0
    assert cache.insert((1, 2)) == 0
    assert cache.insert(()) == 0
    assert cache.size_tokens == 4


def test_shared_prefix_is_stored_once() -> None:
    cache = PrefixCache(100)
    assert cache.insert((1, 2, 3, 4)) == 4
    assert cache.insert((1, 2, 5, 6)) == 2

    assert cache.size_tokens == 6
    assert cache.node_count == 3  # [1,2] -> [3,4] and [5,6]
    assert cache.match((1, 2, 5, 6, 7)) == 4
    assert cache.match((1, 2, 3)) == 3
    assert cache.match((1, 2, 7)) == 2


def test_extending_a_cached_sequence_stores_only_the_extension() -> None:
    cache = PrefixCache(100)
    cache.insert((1, 2))
    assert cache.insert((1, 2, 3, 4)) == 2
    assert cache.size_tokens == 4
    assert cache.match((1, 2, 3, 4)) == 4


def test_matches_agree_with_a_brute_force_model() -> None:
    rng = random.Random(3)
    cache = PrefixCache(100_000)
    stored_prefixes: set[tuple[int, ...]] = set()

    def random_sequence() -> tuple[int, ...]:
        return tuple(rng.randrange(3) for _ in range(rng.randrange(13)))

    for _ in range(300):
        sequence = random_sequence()
        new_prefixes = {sequence[:n] for n in range(1, len(sequence) + 1)} - stored_prefixes
        assert cache.insert(sequence) == len(new_prefixes)
        stored_prefixes |= new_prefixes
        assert cache.size_tokens == len(stored_prefixes)

        for query in (random_sequence() for _ in range(5)):
            expected = max(
                (n for n in range(len(query) + 1) if n == 0 or query[:n] in stored_prefixes),
            )
            assert cache.match(query) == expected


# --- eviction order ------------------------------------------------------------------------


def test_least_recently_used_entry_is_evicted_first() -> None:
    cache = PrefixCache(8)
    cache.insert(A)
    cache.insert(B)

    cache.insert(C)

    assert (cache.match(A), cache.match(B), cache.match(C)) == (0, 4, 4)
    assert cache.size_tokens == 8
    assert (cache.stats.evicted_nodes, cache.stats.evicted_tokens) == (1, 4)


def test_acquiring_an_entry_makes_it_recently_used() -> None:
    cache = PrefixCache(8)
    cache.insert(A)
    cache.insert(B)
    _touch(cache, A)

    cache.insert(C)

    assert (cache.match(A), cache.match(B), cache.match(C)) == (4, 0, 4)


def test_reinserting_an_entry_makes_it_recently_used() -> None:
    cache = PrefixCache(8)
    cache.insert(A)
    cache.insert(B)
    cache.insert(A)

    cache.insert(C)

    assert (cache.match(A), cache.match(B), cache.match(C)) == (4, 0, 4)


def test_match_alone_does_not_protect_an_entry() -> None:
    cache = PrefixCache(8)
    cache.insert(A)
    cache.insert(B)
    cache.match(A)

    cache.insert(C)

    assert cache.match(A) == 0


def test_eviction_removes_leaves_and_keeps_the_shared_prefix() -> None:
    cache = PrefixCache(7)
    cache.insert((1, 2, 3, 4, 5))
    cache.insert((1, 2, 3, 6, 7))  # tree: [1,2,3] -> [4,5] and [6,7]; full

    cache.insert((8, 9))  # needs 2: only the older leaf [4,5] goes

    assert cache.match((1, 2, 3, 4, 5)) == 3
    assert cache.match((1, 2, 3, 6, 7)) == 5
    assert cache.match((8, 9)) == 2
    assert cache.size_tokens == 7


def test_inner_node_is_evicted_only_after_its_children() -> None:
    cache = PrefixCache(7)
    cache.insert((1, 2, 3, 4, 5))
    cache.insert((1, 2, 3, 6, 7))
    cache.insert((8, 9))  # evicts [4,5]

    cache.insert((10, 11, 12, 13, 14))  # needs 5: [6,7] goes, then [1,2,3], now a leaf

    assert cache.match((1, 2, 3)) == 0
    assert cache.match((8, 9)) == 2  # more recently used than the [1,2,3] branch
    assert cache.match((10, 11, 12, 13, 14)) == 5
    assert cache.size_tokens == 7


def test_evict_frees_whole_nodes_and_reports_what_it_freed() -> None:
    cache = PrefixCache(100)
    cache.insert(A)
    cache.insert(B)

    assert cache.evict(1) == 4  # asked for 1, a whole 4-token node went
    assert cache.match(A) == 0
    assert cache.evict(0) == 0
    assert cache.evict(100) == 4  # only B was left
    assert cache.size_tokens == 0


def test_sequence_longer_than_capacity_is_stored_as_far_as_it_fits() -> None:
    cache = PrefixCache(5)
    assert cache.insert(tuple(range(8))) == 5
    assert cache.match(tuple(range(8))) == 5
    assert cache.size_tokens == 5


# --- ref counting --------------------------------------------------------------------------


def test_leased_entry_survives_eviction_even_when_least_recently_used() -> None:
    cache = PrefixCache(8)
    cache.insert(A)
    cache.insert(B)
    lease = cache.acquire(A)
    _touch(cache, B)  # A is now the least recently used, but it is leased

    cache.insert(C)
    assert (cache.match(A), cache.match(B), cache.match(C)) == (4, 0, 4)

    cache.insert(D)
    assert (cache.match(A), cache.match(C), cache.match(D)) == (4, 0, 4)

    cache.release(lease)
    cache.insert(E)
    assert (cache.match(A), cache.match(D), cache.match(E)) == (0, 4, 4)


def test_nothing_is_stored_when_everything_is_leased() -> None:
    cache = PrefixCache(8)
    cache.insert(A)
    cache.insert(B)
    leases = [cache.acquire(A), cache.acquire(B)]
    assert cache.evictable_tokens == 0

    assert cache.insert(C) == 0
    assert cache.evict(4) == 0
    assert (cache.match(A), cache.match(B), cache.match(C)) == (4, 4, 0)

    for lease in leases:
        cache.release(lease)
    assert cache.evictable_tokens == 8
    assert cache.insert(C) == 4


def test_entry_stays_pinned_until_every_lease_is_released() -> None:
    cache = PrefixCache(8)
    cache.insert(A)
    first = cache.acquire(A)
    second = cache.acquire(A)

    cache.release(first)
    assert cache.evictable_tokens == 0
    cache.release(second)
    assert cache.evictable_tokens == 4


def test_lease_pins_only_the_matched_prefix() -> None:
    cache = PrefixCache(6)
    cache.insert(A)
    lease = cache.acquire((1, 2, 99))

    assert lease.matched_tokens == 2
    assert cache.evictable_tokens == 2  # [3,4] is not in use

    cache.insert(B)  # needs 4, 2 free: [3,4] is evicted, [1,2] is not
    assert cache.match(A) == 2
    assert cache.match(B) == 4
    assert cache.size_tokens == 6


def test_lease_still_pins_its_whole_prefix_after_a_later_split() -> None:
    cache = PrefixCache(100)
    cache.insert(A)
    lease = cache.acquire(A)

    cache.insert((1, 2, 5))  # splits the leased run after [1,2]

    assert cache.evictable_tokens == 1
    assert cache.evict(100) == 1  # only [5]
    assert cache.match(A) == 4
    cache.release(lease)
    assert cache.evictable_tokens == 4


def test_lease_on_a_miss_pins_nothing() -> None:
    cache = PrefixCache(8)
    cache.insert(A)
    lease = cache.acquire(B)
    assert lease.matched_tokens == 0
    assert cache.evictable_tokens == 4
    cache.release(lease)


def test_lease_cannot_be_released_twice_or_to_another_cache() -> None:
    cache = PrefixCache(8)
    other = PrefixCache(8)
    lease = cache.acquire(A)

    with pytest.raises(ValueError, match="different cache"):
        other.release(lease)
    cache.release(lease)
    with pytest.raises(ValueError, match="already released"):
        cache.release(lease)
    with pytest.raises(ValueError, match="already released"):
        cache.abandon(lease)


def test_random_operations_keep_the_cache_within_capacity_and_leases_intact() -> None:
    rng = random.Random(11)
    cache = PrefixCache(40)
    held: list[tuple[tuple[int, ...], PrefixLease]] = []

    for _ in range(2000):
        sequence = tuple(rng.randrange(4) for _ in range(rng.randrange(1, 15)))
        action = rng.random()
        if action < 0.5:
            cache.insert(sequence)
        elif action < 0.75 and len(held) < 3:
            held.append((sequence, cache.acquire(sequence)))
        elif held:
            _, lease = held.pop(rng.randrange(len(held)))
            cache.release(lease)

        assert 0 <= cache.size_tokens <= cache.capacity_tokens
        assert 0 <= cache.evictable_tokens <= cache.size_tokens
        for tokens, lease in held:
            assert cache.match(tokens) >= lease.matched_tokens

    for _, lease in held:
        cache.release(lease)
    assert cache.evictable_tokens == cache.size_tokens
    cache.evict(cache.capacity_tokens)
    assert cache.size_tokens == 0
    assert cache.node_count == 0


# --- stats ---------------------------------------------------------------------------------


def test_hit_rate_math() -> None:
    cache = PrefixCache(100)
    _touch(cache, A)  # miss: nothing cached yet, 4 prompt tokens
    cache.insert(A)
    _touch(cache, (1, 2, 3, 4, 5, 6))  # hit: 4 of 6
    _touch(cache, (1, 2, 99))  # hit: 2 of 3
    _touch(cache, (7,))  # miss: 0 of 1

    stats = cache.stats
    assert (stats.lookups, stats.hits) == (4, 2)
    assert (stats.prompt_tokens, stats.cached_tokens) == (14, 6)
    assert stats.hit_rate == 0.5
    assert stats.token_hit_rate == 6 / 14


def test_rates_are_zero_before_any_lookup() -> None:
    stats = PrefixCache(100).stats
    assert stats == CacheStats()
    assert stats.hit_rate == 0.0
    assert stats.token_hit_rate == 0.0


def test_abandoned_lookup_is_not_counted() -> None:
    cache = PrefixCache(100)
    cache.insert(A)
    _touch(cache, A)
    before = cache.stats

    cache.abandon(cache.acquire((1, 2, 99)))
    cache.abandon(cache.acquire((7, 8)))

    assert cache.stats == before
    assert cache.evictable_tokens == cache.size_tokens


def test_capacity_must_be_positive() -> None:
    with pytest.raises(ValueError, match="capacity_tokens"):
        PrefixCache(0)
