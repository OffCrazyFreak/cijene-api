import asyncio
from secrets import token_hex


def test_authenticated_response_uses_real_database(database, service_client):
    async def run():
        async with database() as db:
            async with service_client(db) as client:
                token = token_hex(32)
                async with db._get_conn() as conn:
                    await conn.execute(
                        "INSERT INTO users (name, api_key) VALUES ('test', $1)", token
                    )
                async with db.import_transaction():
                    await db._fetchval(
                        "INSERT INTO chains (code) VALUES ('fixture') RETURNING id"
                    )
                for headers in ({}, {"Authorization": "Bearer invalid"}):
                    response = await client.get("/v1/chains/", headers=headers)
                    assert response.status_code == 403
                response = await client.get(
                    "/v1/chains/", headers={"Authorization": f"Bearer {token}"}
                )
                assert response.status_code == 200
                assert response.json() == {"chains": ["fixture"]}

    asyncio.run(run())
