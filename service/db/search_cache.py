"""Per-database caches that stay valid for one import epoch."""

import asyncio
from collections import OrderedDict, defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any
from weakref import WeakKeyDictionary

from service.db.matching import normalize
from service.db.vocabulary import VocabularyIndex

SortKey = tuple[int, int, Decimal, int, int, str]

CATALOGUE_EPOCHS = 2
ORDERING_ENTRIES = 64
ORDERING_KEYS = 500_000


@dataclass(frozen=True)
class Catalogue:
    """Vocabulary plus the raw facet values behind each normalized filter value."""

    vocabulary: VocabularyIndex
    facet_values: dict[str, dict[str, list[str]]]

    @classmethod
    def build(
        cls, chain_products: list[Any], chains: list[str], cities: list[str]
    ) -> "Catalogue":
        raw_values = {
            "chain": chains,
            "city": cities,
            "brand": [row["brand"] for row in chain_products],
            "category": [row["category"] for row in chain_products],
        }
        facet_values: dict[str, dict[str, list[str]]] = {}
        for facet, values in raw_values.items():
            grouped: dict[str, set[str]] = defaultdict(set)
            for value in values:
                if value:
                    grouped[normalize(value)].add(value)
            facet_values[facet] = {key: sorted(raw) for key, raw in grouped.items()}

        vocabulary = VocabularyIndex(
            (row["id"], row["name"], row["brand"], row["quantity"], row["unit"])
            for row in chain_products
        )
        return cls(vocabulary, facet_values)

    def resolve(self, facet: str, wanted: set[str]) -> list[str]:
        values = self.facet_values[facet]
        return sorted(raw for key in wanted for raw in values.get(key, ()))


async def fetch_catalogue(conn: Any) -> tuple[list[Any], list[str], list[str]]:
    chain_products = await conn.fetch(
        "SELECT id,name,brand,quantity,unit,category FROM chain_products"
    )
    chains = [row["code"] for row in await conn.fetch("SELECT code FROM chains")]
    cities = [
        row["city"]
        for row in await conn.fetch("SELECT DISTINCT city FROM stores")
        if row["city"]
    ]
    return chain_products, chains, cities


@dataclass(frozen=True)
class Ordering:
    keys: list[SortKey]
    facets: dict[str, dict[str, int]]
    outside: int


@dataclass
class SearchCache:
    catalogues: dict[int, Catalogue] = field(default_factory=dict)
    pending: dict[int, asyncio.Task[Catalogue]] = field(default_factory=dict)
    orderings: OrderedDict[tuple[int, str], Ordering] = field(
        default_factory=OrderedDict
    )

    async def catalogue(self, epoch: int, conn: Any) -> Catalogue:
        if epoch in self.catalogues:
            return self.catalogues[epoch]

        build = self.pending.get(epoch)
        if build is None or build.get_loop() is not asyncio.get_running_loop():
            chain_products, chains, cities = await fetch_catalogue(conn)
            build = asyncio.create_task(
                asyncio.to_thread(Catalogue.build, chain_products, chains, cities)
            )
            self.pending[epoch] = build
            build.add_done_callback(lambda task: self._store(epoch, task))

        # A cancelled request must not discard a build the next request reuses.
        return await asyncio.shield(build)

    def _store(self, epoch: int, build: asyncio.Task[Catalogue]) -> None:
        if self.pending.get(epoch) is build:
            del self.pending[epoch]
        if build.cancelled() or build.exception() is not None:
            return
        self.catalogues[epoch] = build.result()
        while len(self.catalogues) > CATALOGUE_EPOCHS:
            del self.catalogues[min(self.catalogues)]

    def ordering(self, epoch: int, scope: str) -> Ordering | None:
        ordering = self.orderings.get((epoch, scope))
        if ordering is not None:
            self.orderings.move_to_end((epoch, scope))
        return ordering

    def remember(self, epoch: int, scope: str, ordering: Ordering) -> None:
        if len(ordering.keys) > ORDERING_KEYS:
            return
        self.orderings[(epoch, scope)] = ordering
        while len(self.orderings) > ORDERING_ENTRIES or (
            sum(len(entry.keys) for entry in self.orderings.values()) > ORDERING_KEYS
        ):
            self.orderings.popitem(last=False)


_caches: WeakKeyDictionary[Any, SearchCache] = WeakKeyDictionary()


def search_cache(database: Any) -> SearchCache:
    cache = _caches.get(database)
    if cache is None:
        cache = _caches[database] = SearchCache()
    return cache
