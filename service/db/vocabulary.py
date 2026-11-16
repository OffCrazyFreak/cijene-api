"""Per-epoch catalogue vocabulary that narrows relevance() to plausible variants."""

from array import array
from bisect import bisect_left
from collections import defaultdict
from collections.abc import Iterable
from sys import intern

from service.db.matching import (
    EDIT_MIN_LENGTH,
    PREFIX_MIN_LENGTH,
    STRICT,
    edit_budget,
    normalize,
    PackSize,
    pack_pairs,
    pack_size,
    pack_sizes,
    score_words,
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
        self.variant_words: list[tuple[tuple[str, ...], tuple[str, ...]]] = []
        self.chain_products: list[array] = []
        self.known_brands: set[str] = set()
        self.postings: dict[str, array] = defaultdict(lambda: array("I"))
        self.pack_postings: dict[PackSize, array] = defaultdict(lambda: array("I"))

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

        name, brand, quantity, unit = variant
        # Interning shares one string per distinct word across all variants.
        words = tuple(intern(word) for word in normalize(name + " " + brand).split())
        brand_words = tuple(intern(word) for word in normalize(brand).split())
        self.variant_words.append((words, brand_words))
        for word in set(words):
            self.postings[word].append(variant_id)
        for size in set(pack_sizes(words, quantity, unit)):
            self.pack_postings[size].append(variant_id)
        self.known_brands.update(brand_words)
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

    def _variants_with_pack(self, amount: str, unit: str) -> set[int]:
        size = pack_size(amount, unit)
        return set(self.pack_postings.get(size, ())) if size else set()

    def candidates(self, query: str) -> Iterable[int]:
        """Every variant that relevance() could accept, possibly with extras."""
        tokens = normalize(query).split()
        pairs = pack_pairs(tokens)
        satisfied_by_packs = {tokens[i + offset] for i in pairs for offset in (0, 1)}
        constrained = {token for token in tokens if token not in satisfied_by_packs}
        if not constrained and not pairs:
            return range(len(self.variants))

        word_matches = (self._variants_for(token) for token in constrained)
        pack_matches = (
            self._variants_with_pack(tokens[i], tokens[i + 1]) for i in pairs
        )
        matches = sorted([*word_matches, *pack_matches], key=len)
        result = matches[0]
        for other in matches[1:]:
            result &= other
        return result

    def match(self, query: str) -> tuple[list[int], list[int]]:
        """Chain product ids and their rank for every variant relevance() accepts."""
        tokens = normalize(query).split()
        ids: list[int] = []
        ranks: list[int] = []
        for variant_id in self.candidates(query):
            _, _, quantity, unit = self.variants[variant_id]
            words, brand_words = self.variant_words[variant_id]
            score = score_words(
                tokens, words, brand_words, quantity, unit, self.known_brands
            )
            if score is None:
                continue
            members = self.chain_products[variant_id]
            ids.extend(members)
            ranks.extend([score[0] * 10 + score[1]] * len(members))
        return ids, ranks
