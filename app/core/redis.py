import redis.asyncio as aioredis
from typing import Optional
from app.core.config import settings

redis_pool: Optional[aioredis.Redis] = None


def get_redis() -> aioredis.Redis:
    """Returns a singleton Redis client instance configured with settings.REDIS_URL."""
    global redis_pool
    if redis_pool is None:
        redis_pool = aioredis.from_url(
            settings.REDIS_URL,
            encoding="utf-8",
            decode_responses=True
        )
    return redis_pool


async def close_redis() -> None:
    """Closes active Redis connection pool."""
    global redis_pool
    if redis_pool is not None:
        await redis_pool.aclose()
        redis_pool = None


# Proxy property to support direct module-level imports
class _RedisProxy:
    def __getattr__(self, name):
        client = get_redis()
        return getattr(client, name)


redis_client = _RedisProxy()