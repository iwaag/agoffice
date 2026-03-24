from collections.abc import AsyncGenerator

import redis.asyncio as aioredis
from agoffice_infra.config import get_redis_settings


def _client() -> aioredis.Redis:
    return aioredis.from_url(get_redis_settings().url, decode_responses=True)


async def publish(channel: str, message: str) -> None:
    r = _client()
    try:
        await r.publish(channel, message)
    finally:
        await r.aclose()


async def subscribe(channel: str) -> AsyncGenerator[str, None]:
    r = _client()
    try:
        async with r.pubsub() as ps:
            await ps.subscribe(channel)
            async for raw in ps.listen():
                if raw["type"] == "message":
                    yield raw["data"]
    finally:
        await r.aclose()


def session_channel(session_id: str) -> str:
    return f"session:{session_id}"
