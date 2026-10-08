"""Prefix cache: a KV-cache stand-in built as a radix tree over token ids.

It stores no tensors. It tracks which token prefixes would be in a model's KV cache, how
many tokens that takes, and what may be evicted. See docs/04-prefix-cache.md.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class CacheStats:
    """Counters for one cache. Lookups are counted by ``acquire``, not by ``match``."""

    lookups: int = 0
    hits: int = 0
    prompt_tokens: int = 0
    cached_tokens: int = 0
    evicted_nodes: int = 0
    evicted_tokens: int = 0

    @property
    def hit_rate(self) -> float:
        """Share of lookups that matched at least one token."""
        return self.hits / self.lookups if self.lookups else 0.0

    @property
    def token_hit_rate(self) -> float:
        """Share of looked-up prompt tokens that were already cached."""
        return self.cached_tokens / self.prompt_tokens if self.prompt_tokens else 0.0


@dataclass(eq=False, slots=True)
class _Node:
    """One edge of the radix tree: a run of tokens shared by everything below it."""

    node_id: int
    tokens: tuple[int, ...]
    parent: _Node | None
    last_used: int
    ref_count: int = 0
    children: dict[int, _Node] = field(default_factory=dict)


@dataclass(eq=False, slots=True)
class PrefixLease:
    """Pins a matched prefix so it cannot be evicted while a request is using it."""

    matched_tokens: int
    prompt_tokens: int
    _node: _Node
    _cache: PrefixCache
    _released: bool = False


class PrefixCache:
    def __init__(self, capacity_tokens: int) -> None:
        if capacity_tokens < 1:
            raise ValueError("capacity_tokens must be at least 1")
        self._capacity_tokens = capacity_tokens
        self._root = _Node(node_id=0, tokens=(), parent=None, last_used=0)
        self._nodes: dict[int, _Node] = {}
        self._next_node_id = 1
        self._tick = 0
        self._size_tokens = 0
        self._pinned_tokens = 0
        self._stats = CacheStats()

    @property
    def capacity_tokens(self) -> int:
        return self._capacity_tokens

    @property
    def size_tokens(self) -> int:
        """Tokens held. A prefix shared by several sequences is counted once."""
        return self._size_tokens

    @property
    def evictable_tokens(self) -> int:
        """Tokens not pinned by any lease."""
        return self._size_tokens - self._pinned_tokens

    @property
    def node_count(self) -> int:
        return len(self._nodes)

    @property
    def stats(self) -> CacheStats:
        return self._stats

    def match(self, tokens: Sequence[int]) -> int:
        """Length of the longest cached prefix of ``tokens``. Changes nothing."""
        _, matched, _, partial = self._walk(tokens)
        return matched + partial

    def acquire(self, tokens: Sequence[int]) -> PrefixLease:
        """Match ``tokens``, pin the matched prefix and mark it as just used.

        Counts as one lookup in the stats. Every lease must be given back with
        ``release`` or ``abandon``.
        """
        node, matched = self._node_for_longest_prefix(tokens)
        self._touch(node)
        self._pin(node)
        self._stats = _replace_stats(
            self._stats,
            lookups=1,
            hits=1 if matched else 0,
            prompt_tokens=len(tokens),
            cached_tokens=matched,
        )
        return PrefixLease(matched, len(tokens), node, self)

    def release(self, lease: PrefixLease) -> None:
        if lease._cache is not self:
            raise ValueError("lease belongs to a different cache")
        if lease._released:
            raise ValueError("lease was already released")
        lease._released = True
        self._unpin(lease._node)

    def abandon(self, lease: PrefixLease) -> None:
        """Release a lease and take its lookup back out of the stats.

        For a request that looked up its prefix but could not be admitted yet and will
        look it up again.
        """
        self.release(lease)
        self._stats = _replace_stats(
            self._stats,
            lookups=-1,
            hits=-1 if lease.matched_tokens else 0,
            prompt_tokens=-lease.prompt_tokens,
            cached_tokens=-lease.matched_tokens,
        )

    def insert(self, tokens: Sequence[int]) -> int:
        """Cache ``tokens`` and return how many tokens were newly stored.

        Evicts least-recently-used unpinned leaves to make room. If there is still not
        enough room, stores as long a prefix of ``tokens`` as fits.
        """
        node, matched = self._node_for_longest_prefix(tokens)
        self._touch(node)
        missing = len(tokens) - matched
        if missing == 0:
            return 0

        # Keep the path we are extending out of the eviction below.
        self._pin(node)
        free = self._capacity_tokens - self._size_tokens
        if missing > free:
            self.evict(missing - free)
            free = self._capacity_tokens - self._size_tokens
        stored = min(missing, free)
        if stored:
            leaf = self._new_node(tuple(tokens[matched : matched + stored]), node)
            node.children[leaf.tokens[0]] = leaf
            self._size_tokens += stored
        self._unpin(node)
        return stored

    def evict(self, min_tokens: int) -> int:
        """Evict least-recently-used unpinned leaves until ``min_tokens`` are freed.

        Whole nodes are removed, so more than ``min_tokens`` may be freed. Returns the
        number freed, which is less than asked if everything left is pinned.
        """
        freed = 0
        while freed < min_tokens:
            victim = min(
                (n for n in self._nodes.values() if not n.children and n.ref_count == 0),
                key=lambda n: (n.last_used, n.node_id),
                default=None,
            )
            if victim is None:
                break
            assert victim.parent is not None
            del victim.parent.children[victim.tokens[0]]
            del self._nodes[victim.node_id]
            self._size_tokens -= len(victim.tokens)
            freed += len(victim.tokens)
            self._stats = _replace_stats(
                self._stats, evicted_nodes=1, evicted_tokens=len(victim.tokens)
            )
        return freed

    def _walk(self, tokens: Sequence[int]) -> tuple[_Node, int, _Node | None, int]:
        """Follow ``tokens`` down the tree.

        Returns ``(node, matched, child, partial)``: ``node`` is the deepest node whose
        whole edge matched, covering ``matched`` tokens. If the match continues part of the
        way along one of its children, that is ``child`` and ``partial`` tokens of it.
        """
        node = self._root
        matched = 0
        while matched < len(tokens):
            child = node.children.get(tokens[matched])
            if child is None:
                break
            common = 0
            limit = min(len(child.tokens), len(tokens) - matched)
            while common < limit and child.tokens[common] == tokens[matched + common]:
                common += 1
            if common < len(child.tokens):
                return node, matched, child, common
            node = child
            matched += common
        return node, matched, None, 0

    def _node_for_longest_prefix(self, tokens: Sequence[int]) -> tuple[_Node, int]:
        """The node ending exactly at the longest cached prefix, splitting an edge if needed."""
        node, matched, child, partial = self._walk(tokens)
        if child is not None and partial:
            node = self._split(child, partial)
            matched += partial
        return node, matched

    def _split(self, node: _Node, at: int) -> _Node:
        """Cut ``node``'s edge after ``at`` tokens; return the new upper node.

        ``node`` keeps its identity as the lower part, so leases that end at it stay valid
        and still pin the whole path.
        """
        assert node.parent is not None
        upper = self._new_node(node.tokens[:at], node.parent)
        upper.last_used = node.last_used
        upper.ref_count = node.ref_count
        node.parent.children[upper.tokens[0]] = upper
        node.tokens = node.tokens[at:]
        node.parent = upper
        upper.children[node.tokens[0]] = node
        return upper

    def _new_node(self, tokens: tuple[int, ...], parent: _Node) -> _Node:
        node = _Node(self._next_node_id, tokens, parent, self._tick)
        self._nodes[node.node_id] = node
        self._next_node_id += 1
        return node

    def _touch(self, node: _Node) -> None:
        self._tick += 1
        current: _Node | None = node
        while current is not None:
            current.last_used = self._tick
            current = current.parent

    def _pin(self, node: _Node) -> None:
        current: _Node | None = node
        while current is not None:
            if current.ref_count == 0:
                self._pinned_tokens += len(current.tokens)
            current.ref_count += 1
            current = current.parent

    def _unpin(self, node: _Node) -> None:
        current: _Node | None = node
        while current is not None:
            current.ref_count -= 1
            if current.ref_count == 0:
                self._pinned_tokens -= len(current.tokens)
            current = current.parent


def _replace_stats(stats: CacheStats, **deltas: int) -> CacheStats:
    return CacheStats(
        lookups=stats.lookups + deltas.get("lookups", 0),
        hits=stats.hits + deltas.get("hits", 0),
        prompt_tokens=stats.prompt_tokens + deltas.get("prompt_tokens", 0),
        cached_tokens=stats.cached_tokens + deltas.get("cached_tokens", 0),
        evicted_nodes=stats.evicted_nodes + deltas.get("evicted_nodes", 0),
        evicted_tokens=stats.evicted_tokens + deltas.get("evicted_tokens", 0),
    )
