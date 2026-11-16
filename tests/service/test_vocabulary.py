"""The vocabulary index must accept exactly what an exhaustive relevance() loop accepts."""

import json
import random
from pathlib import Path

from service.db.matching import normalize, relevance
from service.db.vocabulary import VocabularyIndex

WORDS = [
    "mlijeko",
    "čokolada",
    "kruh",
    "sir",
    "jogurt",
    "šunka",
    "piletina",
    "juha",
    "kava",
    "čaj",
    "sok",
    "jabuka",
    "limun",
    "krumpir",
    "rajčica",
    "paprika",
    "tjestenina",
    "šećer",
    "brašno",
    "vrhnje",
    "napolitanke",
    "pivo",
    "gazirana",
    "đumbir",
    "žitarice",
    "pašteta",
    "kobasica",
    "čips",
    "sladoled",
    "deterdžent",
    "šampon",
    "laktoze",
    "integralno",
    "svježe",
    "trajno",
]
BRANDS = ["Dukat", "Vindija", "Kraš", "Podravka", "Franck", "Ledo", "Milka", ""]
PACKS = [("1", "l"), ("500", "g"), ("1000", "ml"), ("0.5", "kg"), (None, None)]


def fixture_catalogue() -> list[tuple[int, str, str | None, str | None, str | None]]:
    cases = json.loads(Path(__file__).with_name("sorting-fixtures.json").read_text())
    rows = []
    for case in cases:
        for candidate in case["candidates"]:
            rows.append((candidate["name"], candidate.get("brand") or None, None, None))

    generator = random.Random(20261116)
    for _ in range(400):
        name = " ".join(generator.sample(WORDS, generator.randint(1, 4)))
        if generator.random() < 0.3:
            name += f" {generator.choice(['1l', '500 g', '2 kom', '0,5 kg'])}"
        quantity, unit = generator.choice(PACKS)
        rows.append(
            (name.capitalize(), generator.choice(BRANDS) or None, quantity, unit)
        )

    return [(i, *row) for i, row in enumerate(rows)]


def typos(word: str, generator: random.Random) -> list[str]:
    position = generator.randrange(len(word) - 1)
    letter = generator.choice("abcdefghijklmnoprstuvz")
    swapped = (
        word[:position] + word[position + 1] + word[position] + word[position + 2 :]
    )
    one_edit = [
        word[:position] + letter + word[position + 1 :],
        word[:position] + word[position + 1 :],
        word[:position] + letter + word[position:],
        swapped,
    ]
    two_edits = generator.choice(one_edit) + letter
    return [*one_edit, two_edits]


def generated_queries() -> list[str]:
    generator = random.Random(4)
    queries = []
    for word in WORDS:
        plain = normalize(word)
        queries += [word, word.upper(), plain, plain[:3], plain[:4], plain[:-1]]
        if len(plain) >= 4:
            queries += typos(plain, generator)
    for _ in range(120):
        first, second = generator.sample(WORDS, 2)
        queries.append(f"{first} {second[: generator.randint(3, len(second))]}")
    queries += [
        "mlijeko 1 l",
        "mlijeko 1l",
        "jogurt 500 g",
        "sok 1000 ml",
        "1 l",
        "500",
        "dukat mlijeko",
        "milka",
        "bez laktoze",
        "nan l",
        "mlijeko 1 l 1",
        "čokolada milka",
        "ockolada",
        "zz",
        "500 g",
        "0.5 kg",
        "1000 ml mlijeko",
        "6 l",
        "snan l",
    ]
    return queries


def exhaustive(index: VocabularyIndex, rows, query: str) -> set[tuple[int, int]]:
    accepted = set()
    for chain_product_id, name, brand, quantity, unit in rows:
        score = relevance(query, name, brand or "", quantity, unit, index.known_brands)
        if score is not None:
            accepted.add((chain_product_id, score[0] * 10 + score[1]))
    return accepted


def test_index_matches_exhaustive_relevance():
    rows = fixture_catalogue()
    index = VocabularyIndex(rows)
    cases = json.loads(Path(__file__).with_name("sorting-fixtures.json").read_text())
    queries = [case["q"] for case in cases] + generated_queries()
    assert len(queries) > 300

    expected_brands = {
        token for _, _, brand, _, _ in rows for token in normalize(brand or "").split()
    }
    assert index.known_brands == expected_brands

    mismatches = []
    for query in queries:
        ids, ranks = index.match(query)
        if set(zip(ids, ranks)) != exhaustive(index, rows, query):
            mismatches.append(query)
    assert mismatches == []


def test_candidates_shrink_the_scored_set():
    rows = fixture_catalogue()
    index = VocabularyIndex(rows)
    assert len(list(index.candidates("mlijeko"))) < len(index.variants) / 4
    assert len(list(index.candidates("1 l"))) < len(index.variants) / 2
    assert len(list(index.candidates("500 g"))) < len(index.variants) / 2


def test_matching_stops_after_its_deadline():
    import time

    import pytest

    index = VocabularyIndex(fixture_catalogue())
    with pytest.raises(TimeoutError):
        index.match("mljeko", time.monotonic() - 1)
    assert index.match("mljeko", time.monotonic() + 60) == index.match("mljeko")
