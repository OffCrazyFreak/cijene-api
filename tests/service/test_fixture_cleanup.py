import asyncio

import asyncpg
import pytest


class FakePool:
    def __init__(self, failure=False):
        self.failure = failure
        self.closed = False

    async def close(self):
        self.closed = True
        if self.failure:
            raise RuntimeError("pool close failed")


class FakeAdmin:
    def __init__(self, fail_create=False, fail_drop=False):
        self.fail_create = fail_create
        self.fail_drop = fail_drop
        self.drop_attempted = False
        self.closed = False

    async def execute(self, sql):
        if sql.startswith("CREATE") and self.fail_create:
            raise RuntimeError("create failed")
        if sql.startswith("DROP"):
            self.drop_attempted = True
            if self.fail_drop:
                raise RuntimeError("drop failed")

    async def close(self):
        self.closed = True


def fake_connections(monkeypatch, request, dsn, admin, pool):
    captured = {}
    monkeypatch.setattr(request.config.option, "service_test_dsn", dsn)

    async def connect(value):
        assert value == dsn
        return admin

    async def create_pool(*args, **kwargs):
        captured.update(kwargs)
        return pool

    monkeypatch.setattr(asyncpg, "connect", connect)
    monkeypatch.setattr(asyncpg, "create_pool", create_pool)
    return captured


@pytest.mark.parametrize(
    "dsn",
    [
        "postgresql://test@localhost/postgres?sslmode=require",
        "postgresql:///postgres?host=/var/run/postgresql",
    ],
)
def test_database_override_preserves_connection_options(
    monkeypatch, request, database, dsn
):
    admin, pool = FakeAdmin(), FakePool()
    captured = fake_connections(monkeypatch, request, dsn, admin, pool)

    async def run():
        async with database():
            assert captured["dsn"] == dsn
            assert captured["database"].startswith("test_")
        assert pool.closed and admin.drop_attempted and admin.closed

    asyncio.run(run())


@pytest.mark.parametrize("failure", ["create", "pool", "drop"])
def test_cleanup_attempts_all_resources_on_failure(
    monkeypatch, request, database, failure
):
    admin = FakeAdmin(fail_create=failure == "create", fail_drop=failure == "drop")
    pool = FakePool(failure == "pool")
    fake_connections(monkeypatch, request, "postgresql://test/postgres", admin, pool)

    async def run():
        with pytest.raises(RuntimeError):
            async with database():
                pass
        assert admin.drop_attempted and admin.closed
        if failure != "create":
            assert pool.closed

    asyncio.run(run())


def test_cancellation_still_cleans_database(monkeypatch, request, database):
    admin, pool = FakeAdmin(), FakePool()
    fake_connections(monkeypatch, request, "postgresql://test/postgres", admin, pool)

    async def run():
        entered = asyncio.Event()

        async def worker():
            async with database():
                entered.set()
                await asyncio.sleep(3600)

        task = asyncio.create_task(worker())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert pool.closed and admin.drop_attempted and admin.closed

    asyncio.run(run())
