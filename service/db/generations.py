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


async def publish(conn: asyncpg.Connection) -> int | None:
    """Snapshot the validated workspace while the caller holds the writer lock."""
    parent = await conn.fetchval(
        "SELECT active_id FROM publication_state WHERE singleton"
    )
    if parent is None and not await conn.fetchval(
        "SELECT EXISTS(SELECT 1 FROM chains)"
    ):
        return None
    generation = await conn.fetchval(
        "INSERT INTO generations(origin,parent_id) VALUES('import',$1) RETURNING id",
        parent,
    )
    fields_by_kind = {
        "chain": "id,code,created_at",
        "product": "id,ean,brand,name,quantity,unit,created_at",
        "store": "id,chain_id,code,type,address,city,zipcode,lat,lon,phone,created_at",
        "chain_product": "id,chain_id,product_id,code,brand,name,category,unit,quantity,created_at",
        "offer": "id,chain_product_id,store_id,price_date,regular_price,special_price,unit_price,best_price_30,anchor_price,available,anchor_price_date,special_sale_type",
    }
    for kind, fields in fields_by_kind.items():
        source = "prices" if kind == "offer" else kind + "s"
        selection = fields
        suffix = ""
        if kind == "offer":
            selection = "DISTINCT ON (chain_product_id,store_id) " + fields
            suffix = " ORDER BY chain_product_id,store_id,price_date DESC"
        selected = f"(SELECT {selection} FROM {source}{suffix})"
        equality = " AND ".join(
            f"v.{f} IS NOT DISTINCT FROM w.{f}" for f in fields.split(",")
        )
        await conn.execute(
            f"INSERT INTO {kind}_versions({fields}) SELECT "
            + ",".join("w." + f for f in fields.split(","))
            + f" FROM {selected} w WHERE NOT EXISTS(SELECT 1 FROM {kind}_versions v WHERE {equality})"
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
            f"SELECT $1,w.id,v.version_id"
            + "".join(",w." + f for f in links.split(",") if f)
            + f" FROM {selected} w JOIN LATERAL (SELECT version_id FROM {kind}_versions v WHERE {equality} ORDER BY version_id LIMIT 1) v ON TRUE",
            generation,
        )
    await conn.execute(
        """INSERT INTO generation_stats
        SELECT $1,v.chain_product_id,min(least(regular_price,special_price)),
        max(least(regular_price,special_price)),avg(least(regular_price,special_price)),
        count(*),min(price_date),max(price_date)
        FROM generation_offers m JOIN offer_versions v USING(version_id)
        WHERE m.generation_id=$1 GROUP BY v.chain_product_id""",
        generation,
    )
    if parent is not None:
        changed = False
        for kind in fields_by_kind:
            changed = (
                bool(
                    await conn.fetchval(
                        f"SELECT EXISTS((SELECT id,version_id FROM generation_{kind}s WHERE generation_id=$1 EXCEPT SELECT id,version_id FROM generation_{kind}s WHERE generation_id=$2) UNION ALL (SELECT id,version_id FROM generation_{kind}s WHERE generation_id=$2 EXCEPT SELECT id,version_id FROM generation_{kind}s WHERE generation_id=$1))",
                        generation,
                        parent,
                    )
                )
                or changed
            )
        if not changed:
            for kind in [
                "stats",
                "offers",
                "chain_products",
                "stores",
                "products",
                "chains",
            ]:
                await conn.execute(
                    f"DELETE FROM generation_{kind} WHERE generation_id=$1", generation
                )
            await conn.execute("DELETE FROM generations WHERE id=$1", generation)
            return parent
    conflicts = await conn.fetchval(
        """
        SELECT EXISTS(SELECT 1 FROM generation_offers m
        JOIN offer_versions o USING(version_id)
        JOIN generation_chain_products cm ON cm.generation_id=m.generation_id AND cm.id=o.chain_product_id
        JOIN chain_product_versions cp ON cp.version_id=cm.version_id
        WHERE m.generation_id=$1 AND cp.product_id IS NOT NULL
        GROUP BY cp.product_id,o.store_id
        HAVING count(DISTINCT ROW(o.price_date,o.regular_price,o.special_price,cp.unit,cp.quantity))>1)
        """,
        generation,
    )
    if conflicts:
        raise ValueError("Conflicting canonical offers for one physical shop")
    await conn.execute(
        "UPDATE generations SET accepted_at=CURRENT_TIMESTAMP WHERE id=$1", generation
    )
    await conn.execute(
        "UPDATE publication_state SET previous_id=active_id,active_id=$1,epoch=epoch+1,baseline_complete=TRUE",
        generation,
    )
    return generation
