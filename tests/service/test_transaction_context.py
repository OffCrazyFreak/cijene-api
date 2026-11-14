import asyncio

import pytest


def test_inherited_context_rejected_even_after_exit(database):
    async def run():
        async with database() as db:
            await db.create_tables()
            release = asyncio.Event()

            async def child(wait):
                if wait:
                    await release.wait()
                with pytest.raises(RuntimeError, match="owner"):
                    await db._fetchval(
                        "INSERT INTO chains(code) VALUES('child') RETURNING id"
                    )

            async with db.import_transaction():
                await asyncio.create_task(child(False))
                later = asyncio.create_task(child(True))
            release.set()
            await later
            assert await db._fetchval("SELECT count(*) FROM chains") == 0
            assert await db._fetchval("SELECT 1") == 1

    asyncio.run(run())


def test_cancel_resets_context_and_rolls_back(database):
    async def run():
        async with database() as db:
            await db.create_tables()

            async def writer():
                try:
                    async with db.import_transaction():
                        await db._fetchval(
                            "INSERT INTO chains(code) VALUES('cancel') RETURNING id"
                        )
                        raise asyncio.CancelledError
                except asyncio.CancelledError:
                    assert await db._fetchval("SELECT count(*) FROM chains") == 0
                    async with db.import_transaction():
                        assert await db._fetchval("SELECT 1") == 1

            await asyncio.create_task(writer())

    asyncio.run(run())


def test_nested_import_rejected(database):
    async def run():
        async with database() as db:
            await db.create_tables()
            async with db.import_transaction():
                with pytest.raises(RuntimeError):
                    async with db.import_transaction():
                        pass

    asyncio.run(run())


def test_independent_reader_sees_only_committed_rows(database):
    async def run():
        async with database() as db:
            await db.create_tables()
            written = asyncio.Event()
            read = asyncio.Event()

            async def reader():
                await written.wait()
                assert await db._fetchval("SELECT count(*) FROM chains") == 0
                read.set()

            task = asyncio.create_task(reader())
            async with db.import_transaction():
                await db._fetchval(
                    "INSERT INTO chains(code) VALUES('private') RETURNING id"
                )
                written.set()
                await read.wait()
            await task
            assert await db._fetchval("SELECT count(*) FROM chains") == 1

    asyncio.run(run())
