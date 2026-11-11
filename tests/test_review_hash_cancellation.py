"""Review regression: a slot covers actual executor work, not only its awaiter."""
import asyncio
import threading

import pytest

from gateway import local_auth


async def test_repeated_cancellation_does_not_release_running_hash_slot(monkeypatch):
    slots = asyncio.Semaphore(1)
    monkeypatch.setattr(local_auth, '_hash_slots', slots)
    entered, release = threading.Event(), threading.Event()

    def blocked_hash():
        entered.set()
        release.wait(timeout=5)
        return 'done'

    operation = asyncio.create_task(local_auth.derive(blocked_hash))
    try:
        for _ in range(100):
            if entered.is_set():
                break
            await asyncio.sleep(0.005)
        assert entered.is_set()
        operation.cancel()
        await asyncio.sleep(0)
        operation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await operation
        assert slots.locked(), 'Hash slot released while executor is still deriving'
    finally:
        release.set()
        await asyncio.sleep(0.05)


@pytest.mark.parametrize('raises', [False, True])
async def test_hash_completion_releases_exactly_one_slot(monkeypatch, raises):
    slots = asyncio.Semaphore(1)
    monkeypatch.setattr(local_auth, '_hash_slots', slots)
    def work():
        if raises:
            raise ValueError('synthetic failure')
        return 'done'
    if raises:
        with pytest.raises(ValueError):
            await local_auth.derive(work)
    else:
        assert await local_auth.derive(work) == 'done'
    await asyncio.sleep(0)
    assert slots._value == 1
    assert not local_auth._hash_jobs


async def test_detached_hash_failure_consumed_and_slot_held(monkeypatch):
    slots = asyncio.Semaphore(1)
    monkeypatch.setattr(local_auth, '_hash_slots', slots)
    entered, release = threading.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler()
    errors = []
    loop.set_exception_handler(lambda _loop, context: errors.append(context))
    def work():
        entered.set()
        release.wait(timeout=5)
        raise ValueError('synthetic detached failure')
    task = asyncio.create_task(local_auth.derive(work))
    try:
        for _ in range(100):
            if entered.is_set():
                break
            await asyncio.sleep(0.005)
        assert entered.is_set()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert slots.locked()
        assert local_auth._hash_jobs
        release.set()
        for _ in range(100):
            if not local_auth._hash_jobs:
                break
            await asyncio.sleep(0.005)
        assert slots._value == 1
        assert not local_auth._hash_jobs
        assert not errors
    finally:
        release.set()
        loop.set_exception_handler(previous)
