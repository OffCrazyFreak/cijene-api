#!/usr/bin/env python3
"""
Time the vocabulary index over synthetic product names.

Builds the index over N generated names, then times each query through the
index and, with --exhaustive, through a plain relevance() loop for comparison.

    uv run python scripts/benchmark_vocabulary.py --names 500000 --exhaustive
"""

import argparse
import random
import resource
import time

from service.db.matching import relevance
from service.db.vocabulary import VocabularyIndex

WORDS = (
    "mlijeko trajno svježe čokolada mliječna tamna kruh bijeli raženi sir gauda "
    "jogurt voćni šunka piletina juha kava mljevena čaj sok jabuka naranča limun "
    "krumpir mladi rajčica paprika tjestenina šećer brašno vrhnje napolitanke pivo "
    "svijetlo vino crno voda gazirana đumbir žitarice pašteta kobasica čips "
    "sladoled vanilija deterdžent šampon sapun keksi integralni bez laktoze"
).split()
BRANDS = ["Dukat", "Vindija", "Kraš", "Podravka", "Franck", "Ledo", "Milka", ""]
QUERIES = [
    "mlijeko",
    "mljeko",
    "čokolada",
    "cokolada mlijecna",
    "jog",
    "mlijeko 1 l",
    "dukat jogurt",
    "naranca sok",
    "piletna",
    "nepostojeci proizvod",
]


def synthetic_rows(count: int, seed: int):
    generator = random.Random(seed)
    for chain_product_id in range(count):
        words: list[str] = generator.sample(WORDS, generator.randint(2, 5))
        words.append(f"{generator.choice([1, 2, 5, 10, 50, 100, 250, 500])}")
        name = " ".join(words) + generator.choice(["g", "ml", "l", "kg", " kom"])
        name += f" {generator.randrange(1000)}"
        unit = generator.choice(["l", "kg", "g", None])
        quantity = "1" if unit else None
        yield chain_product_id, name, generator.choice(BRANDS) or None, quantity, unit


def peak_megabytes() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--names", type=int, default=500_000)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--exhaustive", action="store_true")
    args = parser.parse_args()

    rows = list(synthetic_rows(args.names, args.seed))
    started = time.perf_counter()
    index = VocabularyIndex(rows)
    print(
        f"build names={len(rows)} variants={len(index.variants)} "
        f"words={len(index.sorted_words)} seconds={time.perf_counter() - started:.2f} "
        f"peak_rss_mb={peak_megabytes():.0f}"
    )

    for query in QUERIES:
        started = time.perf_counter()
        ids, _ = index.match(query)
        indexed = time.perf_counter() - started
        line = f"query={query!r} accepted={len(ids)} indexed_ms={indexed * 1000:.1f}"

        if args.exhaustive:
            started = time.perf_counter()
            expected = sum(
                relevance(query, name, brand or "", quantity, unit, index.known_brands)
                is not None
                for _, name, brand, quantity, unit in index_rows(index)
            )
            line += f" exhaustive_s={time.perf_counter() - started:.2f} exhaustive_accepted={expected}"
        print(line)


def index_rows(index: VocabularyIndex):
    for variant_id, (name, brand, quantity, unit) in enumerate(index.variants):
        for chain_product_id in index.chain_products[variant_id]:
            yield chain_product_id, name, brand, quantity, unit


if __name__ == "__main__":
    main()
