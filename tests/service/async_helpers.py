"""Bounded task coordination for concurrent database regressions."""

import asyncio
from contextlib import asynccontextmanager


async def wait_for_event(event, worker):
    waiter = asyncio.create_task(event.wait())
    try:
        done, _ = await asyncio.wait(
            {waiter, worker}, timeout=5, return_when=asyncio.FIRST_COMPLETED
        )
        if not done:
            raise TimeoutError("Worker did not reach the handshake")
        if worker in done:
            await worker
            if not event.is_set():
                raise AssertionError("Worker completed without signaling")
        await waiter
    finally:
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)


@asynccontextmanager
async def task_scope():
    workers = []
    try:
        yield workers
    finally:
        for worker in workers:
            if not worker.done():
                worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
