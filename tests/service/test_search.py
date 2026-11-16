from service.db.matching import relevance, normalize


def test_matching():
    assert normalize("ČOKOLADA") == "cokolada"
    assert relevance("mlijeko", "Mlijeko svježe", "") is not None
    assert relevance("mljeko", "Mlijeko svježe", "") is not None
    assert relevance("limun pivo", "Limun svježi", "") is None
    ingredient = relevance("limun", "Limun svježi", "")
    flavored = relevance("limun", "Pivo limun", "")
    assert ingredient is not None and flavored is not None
    assert ingredient < flavored
    assert relevance("limun pivo", "Pivo limun", "") is not None
    assert relevance("bez laktoze", "Mlijeko laktoza", "") is None


def test_authored_query_retrieval_report():
    import json
    from pathlib import Path

    cases = json.loads(Path(__file__).with_name("sorting-fixtures.json").read_text())
    failures = []
    for case in cases:
        if case["q"].isdigit():
            continue
        ranked = sorted(
            (score, candidate["ean"])
            for candidate in case["candidates"]
            if (
                score := relevance(
                    case["q"], candidate["name"], candidate.get("brand", "")
                )
            )
            is not None
        )
        retrieved = {ean for _, ean in ranked}
        if case["acceptable"] and (
            not ranked or ranked[0][1] not in case["acceptable"]
        ):
            failures.append((case["id"], case["q"], "ranking", ranked))
        if not set(case["acceptable"]) <= retrieved:
            failures.append((case["id"], case["q"], "retrieval", ranked))
        if case["category"] == "negative" and retrieved:
            failures.append((case["id"], case["q"], "negative", ranked))
    print("Authored query fixture judgments:", failures)
    assert len(cases) == 80
    assert failures == []


def test_decimal_scope_dedup_and_conflict():
    import datetime
    from decimal import Decimal

    import pytest

    from service.db.search import evaluate

    row = dict(
        ean="1",
        name="Mlijeko",
        brand="Brand",
        category="Food",
        product_name=None,
        product_brand=None,
        product_quantity=None,
        product_unit=None,
        chain="test",
        store_code="1",
        city="City",
        code="1",
        quantity="1000",
        unit="g",
        price_date=datetime.date(2026, 11, 14),
        regular_price=Decimal("1.00"),
        special_price=Decimal("1.40"),
    )
    rows = [
        row,
        row.copy(),
        {**row, "store_code": "2", "regular_price": Decimal("1.40")},
    ]
    result = evaluate(
        rows,
        1,
        "scope",
        None,
        "mlijeko",
        None,
        "City",
        None,
        None,
        "price_asc",
        100,
        None,
    )
    product = result["products"][0]
    assert product["shop_count"] == 2 and product["chain_count"] == 1
    assert product["avg_price"] == Decimal("1.20")
    assert product["unit_prices"] == {"mass": Decimal("1.20")}
    assert (
        evaluate(
            rows,
            1,
            "scope",
            None,
            "mlijeko",
            None,
            "City",
            "Other",
            None,
            "relevance",
            100,
            None,
        )["total"]
        == 0
    )
    with pytest.raises(ValueError, match="Conflicting"):
        evaluate(
            [row, {**row, "regular_price": Decimal("0.50")}],
            1,
            "scope",
            None,
            "",
            None,
            None,
            None,
            None,
            "relevance",
            100,
            None,
        )


def test_cursor_retains_exact_decimal_comparator():
    import datetime
    from decimal import Decimal

    from service.db.search import decode_cursor, evaluate

    row = dict(
        ean="a",
        name="Mlijeko",
        brand="",
        category="",
        product_name=None,
        product_brand=None,
        product_quantity=None,
        product_unit=None,
        chain="test",
        store_code="1",
        city="City",
        code="1",
        quantity="1",
        unit="l",
        price_date=datetime.date(2026, 11, 14),
        regular_price=Decimal("1.20000000000000000000000001"),
        special_price=None,
    )
    rows = [
        row,
        {**row, "ean": "b", "regular_price": Decimal("1.20000000000000000000000002")},
    ]
    first = evaluate(
        rows, 1, "scope", None, "", None, None, None, None, "price_asc", 1, None
    )
    cursor = decode_cursor(first["next_cursor"])
    assert cursor["key"][2] == "1.20000000000000000000000001"
    second = evaluate(
        rows, 1, "scope", cursor, "", None, None, None, None, "price_asc", 1, None
    )
    assert [p["ean"] for p in first["products"] + second["products"]] == ["a", "b"]


def test_transposition_with_another_edit():
    from service.db.matching import token_distance

    assert token_distance("ockolada", "cokolade") == 2
    assert token_distance("abcd", "badc") == 2
    assert relevance("ockolada", "Cokolade", "") is not None


def test_impossible_lengths_skip_distance_matrix(monkeypatch):
    from service.db import matching

    def unexpected(*args, **kwargs):
        raise AssertionError("Impossible edit distance should be rejected first")

    monkeypatch.setattr(matching, "token_distance", unexpected)
    assert matching.relevance("x" * 200, "Mlijeko", "") is None


def test_long_query_rejected_before_database_access():
    import asyncio
    import pytest
    from service.db.search import search

    with pytest.raises(ValueError, match="256"):
        asyncio.run(search(None, q="x" * 257))


def test_http_long_query_rejected(database, service_client):
    import asyncio

    async def run():
        async with database() as db:
            async with service_client(db) as client:
                async with db._get_conn() as conn:
                    await conn.execute(
                        "INSERT INTO users(name,api_key) VALUES('fixture','length-fixture')"
                    )
                result = await client.get(
                    "/v1/products/",
                    params={"q": "x" * 257},
                    headers={"Authorization": "Bearer length-fixture"},
                )
                assert result.status_code == 422

    asyncio.run(run())
