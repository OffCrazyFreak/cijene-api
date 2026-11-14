import asyncio
from secrets import token_hex
from unittest.mock import patch

from tests.service.import_fixtures import importer, write_day


def test_latest_http_contract(database, service_client, tmp_path):
    async def run():
        async with database() as db:
            async with service_client(db) as client:
                token = token_hex(32)
                await db._fetchval(
                    "INSERT INTO users(name,api_key) VALUES('test',$1) RETURNING id",
                    token,
                )
                with patch.object(importer, "db", db):
                    await importer.import_directory(
                        write_day(tmp_path, "1.20", "Mlijeko"), False
                    )
                headers = {"Authorization": f"Bearer {token}"}
                response = await client.get(
                    "/v1/products/",
                    params={"q": "mlijeko", "page_size": 137},
                    headers=headers,
                )
                assert response.status_code == 200, response.text
                data = response.json()
                assert data["total"] == 1 and data["generation"] is not None
                assert data["products"][0]["shop_count"] == 1
                assert data["products"][0]["chains"][0]["price_date"] == "2026-11-14"
                for params in [
                    {"q": "m"},
                    {"q": "mlijeko", "date": "2026-11-13"},
                    {"q": "mlijeko", "limit": 4, "page_size": 5},
                    {"q": "mlijeko", "cursor": "malformed"},
                ]:
                    assert (
                        await client.get(
                            "/v1/products/", params=params, headers=headers
                        )
                    ).status_code == 400
                assert (
                    await client.get(
                        "/v1/products/", params={"page_size": 10001}, headers=headers
                    )
                ).status_code == 422
                for path in [
                    "/v1/products/12345678/",
                    "/v1/stores/",
                    "/v1/test/stores/",
                    "/v1/chain-stats/",
                    "/v1/prices/?eans=12345678",
                ]:
                    response = await client.get(path, headers=headers)
                    assert response.status_code == 200, response.text

    asyncio.run(run())


def test_request_keeps_generation_during_publication(database, tmp_path):
    from contextlib import asynccontextmanager

    from service.db.search import SQL, search

    async def run():
        async with database() as db:
            await db.create_tables()
            with patch.object(importer, "db", db):
                day = write_day(tmp_path, "2.00", "Mlijeko")
                await importer.import_directory(day, False)
                original = db._get_conn
                pinned, published = asyncio.Event(), asyncio.Event()

                class Connection:
                    def __init__(self, conn):
                        self.conn = conn

                    def __getattr__(self, name):
                        return getattr(self.conn, name)

                    async def fetch(self, query, *args):
                        if query == SQL:
                            pinned.set()
                            await asyncio.wait_for(published.wait(), 10)
                        return await self.conn.fetch(query, *args)

                @asynccontextmanager
                async def connection():
                    async with original() as conn:
                        yield Connection(conn)

                async def correct():
                    await asyncio.wait_for(pinned.wait(), 10)
                    try:
                        await importer.import_directory(
                            write_day(tmp_path, "3.00", "Mlijeko"), False
                        )
                    finally:
                        published.set()

                with patch.object(db, "_get_conn", connection):
                    writer = asyncio.create_task(correct())
                    try:
                        result = await search(db, q="mlijeko")
                        await writer
                    finally:
                        if not writer.done():
                            writer.cancel()
                        await asyncio.gather(writer, return_exceptions=True)
                assert result["products"][0]["avg_price"] == 2
                assert (await search(db, q="mlijeko"))["products"][0]["avg_price"] == 3

    asyncio.run(run())
