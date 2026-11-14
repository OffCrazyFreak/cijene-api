"""Opt-in integration tests against an explicitly selected disposable server."""

import asyncio
from contextlib import asynccontextmanager
from uuid import uuid4

import asyncpg
import pytest

from service.db.psql import PostgresDatabase


class ServiceTestDatabase(PostgresDatabase):
    def __init__(self, dsn, database_name):
        super().__init__(dsn, 1, 3)
        self.database_name = database_name

    async def connect(self):
        self.pool = await asyncpg.create_pool(
            dsn=self.dsn,
            database=self.database_name,
            min_size=self.min_size,
            max_size=self.max_size,
        )


async def cleanup_database(admin, db, name):
    try:
        if db is not None:
            await db.close()
    finally:
        try:
            await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        finally:
            await admin.close()


@pytest.fixture
def database(request):
    @asynccontextmanager
    async def create():
        dsn = request.config.getoption("--service-test-dsn")
        if not dsn:
            pytest.skip("pass --service-test-dsn to run PostgreSQL integration tests")
        name = "test_" + uuid4().hex
        admin = await asyncpg.connect(dsn)
        db = None
        try:
            await admin.execute(f'CREATE DATABASE "{name}"')
            db = ServiceTestDatabase(dsn, name)
            await db.connect()
            yield db
        finally:
            cleanup = asyncio.create_task(cleanup_database(admin, db, name))
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                await cleanup
                raise

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
