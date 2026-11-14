import asyncio

import pytest


async def assert_index(db):
    async with db._get_conn() as conn:
        await conn.execute("""
            INSERT INTO chains (code) VALUES ('index-fixture');
            INSERT INTO chain_products (chain_id, code, name)
            SELECT id, 'milk', 'Mlijeko čokoladno' FROM chains
            WHERE code = 'index-fixture';
            SET enable_seqscan = off;
        """)
        plan = await conn.fetchval("""
            EXPLAIN SELECT * FROM chain_products
            WHERE lower(immutable_unaccent(name || ' ' || COALESCE(brand, '')))
                LIKE '%mlijeko%'
        """)
        rows = await conn.fetch("""
            EXPLAIN SELECT * FROM chain_products
            WHERE lower(immutable_unaccent(name || ' ' || COALESCE(brand, '')))
                LIKE '%mlijeko%'
        """)
        assert (
            await conn.fetchval("""
            SELECT name FROM chain_products
            WHERE lower(immutable_unaccent(name || ' ' || COALESCE(brand, '')))
                LIKE '%mlijeko%'
        """)
            == "Mlijeko čokoladno"
        )
        assert plan
        assert "idx_chain_products_name_trgm" in str(rows)
        assert (
            await conn.fetchval("SELECT immutable_unaccent('Čokolada')") == "Cokolada"
        )


def test_fresh_repeated_and_old_wrapper_upgrade(database):
    async def run():
        async with database() as db:
            await db.create_tables()
            await db.create_tables()
            async with db._get_conn() as conn:
                await conn.execute("""
                    CREATE OR REPLACE FUNCTION immutable_unaccent(text)
                    RETURNS text LANGUAGE sql IMMUTABLE PARALLEL SAFE STRICT
                    AS $$ SELECT unaccent('unaccent', $1) $$
                """)
            await db.create_tables()
            await assert_index(db)

    asyncio.run(run())


@pytest.mark.parametrize("relocated", [False, True])
def test_relocated_extension_and_hostile_search_path(database, relocated):
    async def run():
        async with database() as db:
            async with db._get_conn() as conn:
                await conn.execute("""
                    CREATE SCHEMA extensions;
                """)
                if relocated:
                    await conn.execute("CREATE EXTENSION unaccent SCHEMA extensions")
            await db.create_tables()
            async with db._get_conn() as conn:
                await conn.execute("""
                    CREATE SCHEMA hostile;
                    CREATE FUNCTION hostile.unaccent(regdictionary, text)
                    RETURNS text LANGUAGE sql IMMUTABLE AS $$ SELECT 'shadowed'::text $$;
                    CREATE TEXT SEARCH DICTIONARY hostile.unaccent
                        (TEMPLATE = pg_catalog.simple);
                    SET search_path = hostile, public, extensions, pg_catalog;
                """)
                assert (
                    await conn.fetchval("SELECT public.immutable_unaccent('Čaj')")
                    == "Caj"
                )
            await assert_index(db)

    asyncio.run(run())
