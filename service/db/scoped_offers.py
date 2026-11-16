"""SQL for one search scope: ordering keys with facets, and the details of one page."""

import json
from pathlib import Path
from typing import Any

LATEST_OFFERS = Path(__file__).with_name("latest.sql").read_text()
FACETS = ("chain", "city", "brand", "category")
PRICE_KEYS = {"relevance": "0", "price_asc": "min_price", "price_desc": "-min_price"}

AGGREGATE = """
    min(effective) min_price,max(effective) max_price,avg(effective) avg_price,
    count(*) shop_count,count(DISTINCT chain) chain_count,
    min(price_date) min_price_date,max(price_date) max_price_date,max(price_date) price_date,
    avg(effective / NULLIF(quantity::numeric * CASE lower(unit) WHEN 'g' THEN .001 ELSE 1 END,0)) FILTER(WHERE lower(unit) IN ('g','kg') AND quantity ~ '^[0-9]+([.][0-9]+)?$') mass,
    avg(effective / NULLIF(quantity::numeric * CASE lower(unit) WHEN 'ml' THEN .001 ELSE 1 END,0)) FILTER(WHERE lower(unit) IN ('ml','l') AND quantity ~ '^[0-9]+([.][0-9]+)?$') volume,
    avg(effective / NULLIF(quantity::numeric,0)) FILTER(WHERE lower(unit)='kom' AND quantity ~ '^[0-9]+([.][0-9]+)?$') pieces
"""
PAGE_METADATA = (
    "code",
    "name",
    "brand",
    "category",
    "quantity",
    "unit",
    "product_name",
    "product_brand",
    "product_quantity",
    "product_unit",
)
CANONICAL_SHOP = (
    "SELECT DISTINCT ON (ean,chain,store_code) * FROM {source} WHERE {where} "
    "ORDER BY ean,chain,store_code,price_date DESC,chain_product_id"
)


class Statement:
    """Positional arguments together with the filter clauses that reference them."""

    def __init__(self, filters: dict[str, list[str] | None]):
        self.args: list[Any] = []
        self.clauses = {
            facet: "TRUE"
            if values is None
            else f"{facet}=ANY({self.bind(values)}::text[])"
            for facet, values in filters.items()
        }

    def bind(self, value: Any) -> str:
        self.args.append(value)
        return f"${len(self.args)}"

    def where(self, excluding: str | None = None) -> str:
        return " AND ".join(
            clause for facet, clause in self.clauses.items() if facet != excluding
        )


def ordering_sql(
    statement: Statement,
    identity: str | None,
    matched: tuple[list[int], list[int]] | None,
    city_labels: dict[str, str],
    order: str,
) -> str:
    identity_param = statement.bind(identity)
    if matched is None:
        candidates = f"SELECT id product_id,0 rank FROM products WHERE ({identity_param}::text IS NULL OR ean={identity_param})"
    else:
        ids, ranks = matched
        candidates = f"""
            SELECT cp.product_id,min(m.rank) rank
            FROM unnest({statement.bind(ids)}::bigint[],{statement.bind(ranks)}::int[]) m(id,rank)
            JOIN chain_products cp ON cp.id=m.id
            WHERE cp.product_id IS NOT NULL
            GROUP BY cp.product_id"""

    labels_param = statement.bind(json.dumps(city_labels))
    facets = []
    for facet in FACETS:
        value = (
            f"COALESCE({labels_param}::jsonb->>city,city)" if facet == "city" else facet
        )
        # Each facet counts the relation with every selection except its own.
        facets.append(f"""(
            SELECT COALESCE(json_object_agg(value,count ORDER BY value),'{{}}')
            FROM (
                SELECT {value} AS value,count(DISTINCT ean) AS count FROM matching
                WHERE {statement.where(excluding=facet)} AND {facet} IS NOT NULL AND {facet}<>''
                GROUP BY 1
            ) counted
        ) AS {facet}""")

    keys = ",".join(
        f"ARRAY(SELECT {column} FROM ordered ORDER BY position) AS {column}"
        for column in ("ean", "rank", "min_price", "shop_count", "chain_count")
    )
    return f"""
        WITH candidates AS MATERIALIZED ({candidates}),
        matching AS MATERIALIZED (
            SELECT p.ean,c.code AS chain,s.code AS store_code,s.city,cp.brand,cp.category,
                   cp.id AS chain_product_id,o.price_date,
                   least(o.regular_price,NULLIF(o.special_price,0)) AS effective,candidates.rank
            {LATEST_OFFERS}
            JOIN candidates ON candidates.product_id=p.id
            WHERE ({identity_param}::text IS NULL OR p.ean={identity_param})
        ),
        eligible AS ({CANONICAL_SHOP.format(source="matching", where=statement.where())}),
        keys AS (
            SELECT ean,min(rank) rank,min(effective) min_price,
                   count(*) shop_count,count(DISTINCT chain) chain_count
            FROM eligible GROUP BY ean
        ),
        ordered AS (
            SELECT *,row_number() OVER (
                ORDER BY rank,{PRICE_KEYS[order]},shop_count DESC,chain_count DESC,ean COLLATE "C"
            ) AS position
            FROM keys
        )
        SELECT {",".join(facets)},
            (SELECT count(DISTINCT ean) FROM matching WHERE {statement.where(excluding="city")}) AS outside,
            {keys}
    """


def page_sql(statement: Statement, eans: list[str]) -> str:
    eans_param = statement.bind(eans)
    metadata = ",".join(
        f"(array_agg({column} ORDER BY chain,code,store_code))[1] AS {column}"
        for column in PAGE_METADATA
    )
    offers = f"""(
        SELECT p.ean,p.name AS product_name,p.brand AS product_brand,
               p.quantity AS product_quantity,p.unit AS product_unit,
               c.code AS chain,s.code AS store_code,s.city,
               cp.id AS chain_product_id,cp.code,cp.name,cp.brand,cp.category,cp.quantity,cp.unit,
               o.price_date,least(o.regular_price,NULLIF(o.special_price,0)) AS effective
        {LATEST_OFFERS}
        WHERE p.ean=ANY({eans_param}::text[])
    ) offers"""
    return f"""
        WITH eligible AS ({CANONICAL_SHOP.format(source=offers, where=statement.where())})
        SELECT ean,chain,{metadata},{AGGREGATE}
        FROM eligible
        GROUP BY GROUPING SETS ((ean),(ean,chain))
        ORDER BY ean,chain NULLS FIRST
    """
