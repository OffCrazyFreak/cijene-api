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


class CatalogueBuildAbandoned(Exception):
    """The request that started a build stopped before handing it off; retry."""


@dataclass(frozen=True)
class Ordering:
    keys: list[SortKey]
    facets: dict[str, dict[str, int]]
    outside: int
    partial: bool


@dataclass
class SearchCache:
    catalogues: dict[int, Catalogue] = field(default_factory=dict)
    pending: dict[int, asyncio.Future[Catalogue]] = field(default_factory=dict)
    orderings: OrderedDict[tuple[int, str], Ordering] = field(
        default_factory=OrderedDict
    )

    async def catalogue(self, epoch: int, conn: Any) -> Catalogue:
        loop = asyncio.get_running_loop()
        while epoch not in self.catalogues:
            pending = self.pending.get(epoch)
            if pending is not None and pending.get_loop() is loop:
                try:
                    # A cancelled request must not discard a build the next request reuses.
                    return await asyncio.shield(pending)
                except CatalogueBuildAbandoned:
                    continue

            return await self._build(epoch, conn, loop)
        return self.catalogues[epoch]

    async def _build(
        self, epoch: int, conn: Any, loop: asyncio.AbstractEventLoop
    ) -> Catalogue:
        # Reserved before the first await so concurrent cold requests share this build.
        pending: asyncio.Future[Catalogue] = loop.create_future()
        self.pending[epoch] = pending
        try:
            chain_products, chains, cities = await fetch_catalogue(conn)
        except BaseException:
            self._settle(epoch, pending, error=CatalogueBuildAbandoned())
            raise

        build = asyncio.create_task(
            asyncio.to_thread(Catalogue.build, chain_products, chains, cities)
        )
        build.add_done_callback(lambda task: self._finish(epoch, pending, task))
        return await asyncio.shield(pending)

    def _finish(
        self, epoch: int, pending: asyncio.Future[Catalogue], build: asyncio.Task
    ) -> None:
        if build.cancelled():
            self._settle(epoch, pending, error=CatalogueBuildAbandoned())
        elif build.exception() is not None:
            self._settle(epoch, pending, error=build.exception())
        else:
            self.catalogues[epoch] = build.result()
            while len(self.catalogues) > CATALOGUE_EPOCHS:
                del self.catalogues[min(self.catalogues)]
            self._settle(epoch, pending, result=build.result())

    def _settle(
        self,
        epoch: int,
        pending: asyncio.Future[Catalogue],
        result: Catalogue | None = None,
        error: BaseException | None = None,
    ) -> None:
        if self.pending.get(epoch) is pending:
            del self.pending[epoch]
        if error is None and result is not None:
            pending.set_result(result)
            return
        pending.set_exception(error or CatalogueBuildAbandoned())
        # Waiters may all be gone; marking the error retrieved avoids a spurious log.
        pending.exception()

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
