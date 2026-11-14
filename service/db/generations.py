"""One-time immutable baseline for legacy price databases."""

import asyncpg


async def backfill_legacy(conn: asyncpg.Connection) -> int | None:
    """Caller holds the writer lock and startup transaction."""
    state = await conn.fetchrow("SELECT * FROM publication_state WHERE singleton")
    if state is None:
        raise RuntimeError("Publication state is not initialized")
    if state["baseline_complete"]:
        return state["active_id"]

    await conn.execute(
        "LOCK TABLE chains, products, stores, chain_products, prices "
        "IN SHARE ROW EXCLUSIVE MODE"
    )
    if not await conn.fetchval("SELECT EXISTS(SELECT 1 FROM prices)"):
        await conn.execute("UPDATE publication_state SET baseline_complete=TRUE")
        return None

    generation = await conn.fetchval(
        "INSERT INTO generations(origin) VALUES('legacy') RETURNING id"
    )
    columns = {
        "chain": "id,code,created_at",
        "product": "id,ean,brand,name,quantity,unit,created_at",
        "store": "id,chain_id,code,type,address,city,zipcode,lat,lon,phone,created_at",
        "chain_product": "id,chain_id,product_id,code,brand,name,category,unit,quantity,created_at",
        "offer": "id,chain_product_id,store_id,price_date,regular_price,special_price,unit_price,best_price_30,anchor_price,available,anchor_price_date,special_sale_type",
    }
    for kind, fields in columns.items():
        source = "prices" if kind == "offer" else f"{kind}s"
        selection = fields
        suffix = ""
        if kind == "offer":
            selection = f"DISTINCT ON (chain_product_id,store_id) {fields}"
            suffix = " ORDER BY chain_product_id,store_id,price_date DESC"
        await conn.execute(
            f"INSERT INTO {kind}_versions({fields}) SELECT {selection} FROM {source}{suffix}"
        )
        links = ""
        if kind in ("store", "chain_product"):
            links = ",chain_id"
        if kind == "chain_product":
            links += ",product_id"
        if kind == "offer":
            links = ",chain_product_id,store_id"
        await conn.execute(
            f"INSERT INTO generation_{kind}s(generation_id,id,version_id{links}) "
            f"SELECT $1,id,version_id{links} FROM {kind}_versions",
            generation,
        )

    await conn.execute(
        """INSERT INTO generation_stats
        SELECT $1, chain_product_id,
               min(least(regular_price,special_price)),
               max(least(regular_price,special_price)),
               avg(least(regular_price,special_price)),
               count(*),min(price_date),max(price_date)
        FROM offer_versions GROUP BY chain_product_id""",
        generation,
    )
    expected = await conn.fetchval(
        "SELECT count(*) FROM (SELECT DISTINCT chain_product_id,store_id FROM prices) offers"
    )
    actual = await conn.fetchval(
        "SELECT count(*) FROM generation_offers WHERE generation_id=$1", generation
    )
    if actual != expected:
        raise RuntimeError("Legacy generation offer count mismatch")
    await conn.execute(
        "UPDATE generations SET accepted_at=CURRENT_TIMESTAMP WHERE id=$1", generation
    )
    await conn.execute(
        "UPDATE publication_state SET active_id=$1, epoch=1, baseline_complete=TRUE",
        generation,
    )
    return generation
