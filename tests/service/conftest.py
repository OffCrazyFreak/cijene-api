"""Opt-in real service tests; TEST_DATABASE_DSN must name a disposable server."""

import os
from contextlib import asynccontextmanager
from uuid import uuid4

import asyncpg
import pytest

from service.db.psql import PostgresDatabase


@pytest.fixture
def database():
    @asynccontextmanager
    async def create():
        dsn = os.environ.get("TEST_DATABASE_DSN")
        if not dsn:
            pytest.skip("set TEST_DATABASE_DSN to run PostgreSQL integration tests")
        admin = await asyncpg.connect(dsn)
        name = "test_" + uuid4().hex
        await admin.execute(f'CREATE DATABASE "{name}"')
        db = PostgresDatabase(dsn.rsplit("/", 1)[0] + "/" + name, 1, 3)
        try:
            await db.connect()
            yield db
        finally:
            await db.close()
            await admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
            await admin.close()

    return create


@pytest.fixture
def service_client():
    @asynccontextmanager
    async def create(db):
        from unittest.mock import patch

        import httpx

        from service import main
        from service.routers import auth, v1

        auth._auth_cache.clear()
        await db.close()
        with (
            patch.object(main, "db", db),
            patch.object(auth, "db", db),
            patch.object(v1, "db", db),
        ):
            try:
                async with main.app.router.lifespan_context(main.app):
                    async with httpx.AsyncClient(
                        transport=httpx.ASGITransport(app=main.app),
                        base_url="http://test",
                    ) as client:
                        yield client
            finally:
                auth._auth_cache.clear()

    return create
