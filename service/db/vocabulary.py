"""Per-epoch catalogue vocabulary that narrows relevance() to plausible variants."""

import asyncio
from array import array
from bisect import bisect_left
from collections import OrderedDict, defaultdict
from collections.abc import Awaitable, Callable, Iterable
from typing import Any
from weakref import WeakKeyDictionary

from service.db.matching import (
    EDIT_MIN_LENGTH,
    PREFIX_MIN_LENGTH,
    STRICT,
    edit_budget,
    normalize,
    pack_pairs,
    relevance,
    token_distance,
)

Variant = tuple[str, str, str | None, str | None]
CatalogueRow = tuple[int, str, str | None, str | None, str | None]


def letter_mask(word: str) -> int:
    mask = 0
    for character in word:
        mask |= 1 << (ord(character) & 63)
    return mask


class VocabularyIndex:
    """Distinct name, brand and pack variants with word, prefix and edit lookups."""

    def __init__(self, rows: Iterable[CatalogueRow]):
        self.variants: list[Variant] = []
        self.chain_products: list[array] = []
        self.known_brands: set[str] = set()
        self.postings: dict[str, array] = defaultdict(lambda: array("I"))

        variant_ids: dict[Variant, int] = {}
        for chain_product_id, name, brand, quantity, unit in rows:
            variant = (name, brand or "", quantity, unit)
            variant_id = variant_ids.get(variant)
            if variant_id is None:
                variant_id = self._add_variant(variant)
                variant_ids[variant] = variant_id
            self.chain_products[variant_id].append(chain_product_id)

        self.sorted_words = sorted(self.postings)
        self.words_by_length: dict[int, list[tuple[str, int]]] = defaultdict(list)
        for word in self.sorted_words:
            if word.isalpha():
                self.words_by_length[len(word)].append((word, letter_mask(word)))

    def _add_variant(self, variant: Variant) -> int:
        variant_id = len(self.variants)
        self.variants.append(variant)
        self.chain_products.append(array("q"))

        name, brand, _, _ = variant
        for word in set(normalize(name + " " + brand).split()):
            self.postings[word].append(variant_id)
        self.known_brands.update(normalize(brand).split())
        return variant_id

    def _words_for(self, token: str) -> Iterable[str]:
        if token in self.postings:
            yield token
        if token in STRICT or any(c.isdigit() for c in token):
            return

        if len(token) >= PREFIX_MIN_LENGTH:
            start = bisect_left(self.sorted_words, token)
            for word in self.sorted_words[start:]:
                if not word.startswith(token):
                    break
                yield word

        if len(token) >= EDIT_MIN_LENGTH:
            budget = edit_budget(token)
            mask = letter_mask(token)
            for length in range(len(token) - budget, len(token) + budget + 1):
                for word, word_mask in self.words_by_length.get(length, ()):
                    # One edit changes at most two letters of the letter set.
                    if (mask ^ word_mask).bit_count() > 2 * budget:
                        continue
                    if token_distance(token, word, budget) <= budget:
                        yield word

    def _variants_for(self, token: str) -> set[int]:
        found: set[int] = set()
        for word in self._words_for(token):
            found.update(self.postings[word])
        return found

    def candidates(self, query: str) -> Iterable[int]:
        """Every variant that relevance() could accept, possibly with extras."""
        tokens = normalize(query).split()
        satisfied_by_packs = {
            tokens[i + offset] for i in pack_pairs(tokens) for offset in (0, 1)
        }
        constrained = {token for token in tokens if token not in satisfied_by_packs}
        if not constrained:
            return range(len(self.variants))

        matches = sorted((self._variants_for(token) for token in constrained), key=len)
        result = matches[0]
        for other in matches[1:]:
            result &= other
        return result

    def match(self, query: str) -> tuple[list[int], list[int]]:
        """Chain product ids and their rank for every variant relevance() accepts."""
        ids: list[int] = []
        ranks: list[int] = []
        for variant_id in self.candidates(query):
            name, brand, quantity, unit = self.variants[variant_id]
            score = relevance(query, name, brand, quantity, unit, self.known_brands)
            if score is None:
                continue
            members = self.chain_products[variant_id]
            ids.extend(members)
            ranks.extend([score[0] * 10 + score[1]] * len(members))
        return ids, ranks


class VocabularyCache:
    """Keeps the newest indexes and lets one build per epoch finish after a timeout."""

    def __init__(self, size: int = 2):
        self.size = size
        self.indexes: OrderedDict[int, VocabularyIndex] = OrderedDict()
        self.pending: dict[int, asyncio.Task[VocabularyIndex]] = {}

    async def get(
        self, epoch: int, load: Callable[[], Awaitable[list[CatalogueRow]]]
    ) -> VocabularyIndex:
        if epoch in self.indexes:
            self.indexes.move_to_end(epoch)
            return self.indexes[epoch]

        build = self.pending.get(epoch)
        if build is None or build.get_loop() is not asyncio.get_running_loop():
            rows = await load()
            build = asyncio.create_task(asyncio.to_thread(VocabularyIndex, rows))
            self.pending[epoch] = build
            build.add_done_callback(lambda task: self._store(epoch, task))

        # A cancelled request must not discard a build the next request reuses.
        return await asyncio.shield(build)

    def _store(self, epoch: int, build: asyncio.Task[VocabularyIndex]) -> None:
        if self.pending.get(epoch) is build:
            del self.pending[epoch]
        if build.cancelled() or build.exception() is not None:
            return
        self.indexes[epoch] = build.result()
        while len(self.indexes) > self.size:
            del self.indexes[min(self.indexes)]


_caches: WeakKeyDictionary[Any, VocabularyCache] = WeakKeyDictionary()


def vocabulary_cache(database: Any) -> VocabularyCache:
    cache = _caches.get(database)
    if cache is None:
        cache = _caches[database] = VocabularyCache()
    return cache
