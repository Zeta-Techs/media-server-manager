from __future__ import annotations

from contextlib import contextmanager

from redis import Redis

from apps.api.config import get_settings


@contextmanager
def plex_lock(tenant_id: str, server_id: int, *, timeout: int = 900):
    client = Redis.from_url(get_settings().redis_url, decode_responses=True)
    lock = client.lock(f"tenant:{tenant_id}:plex:{server_id}", timeout=timeout, blocking_timeout=30)
    acquired = lock.acquire(blocking=True)
    if not acquired:
        raise TimeoutError("Plex server lock is busy")
    try:
        yield
    finally:
        try:
            lock.release()
        except Exception:
            pass
