"""Redis client for query caching and refresh-token storage."""

import json
import logging

import redis

from app.core.settings import settings

logger = logging.getLogger(__name__)


if settings.ENVIRONMENT == "production":
    from upstash_redis import Redis

    redis_client = Redis(
        url=settings.PROD_REDIS_URL,
        token=settings.REDIS_TOKEN,
        decode_responses=True,
        socket_timeout=5,
    )

    refresh_redis = Redis(
        url=settings.PROD_REDIS_URL.replace(
            f"/{settings.PROD_REDIS_URL.rsplit('/', 1)[-1]}",
            f"/{settings.REDIS_REFRESH_DB}",
        ),
        token=settings.REDIS_TOKEN,
        decode_responses=True,
        socket_timeout=5,
    )
else:
    redis_client = redis.Redis.from_url(
        settings.REDIS_URL, decode_responses=True, socket_timeout=5
    )

    refresh_redis = redis.Redis.from_url(
        settings.REDIS_URL.replace(f"/{settings.REDIS_URL.rsplit('/', 1)[-1]}", f"/{settings.REDIS_REFRESH_DB}"),
        decode_responses=True,
        socket_timeout=5,
    )


def gen_cache_key(config: dict) -> str:
    """Deterministic cache key from a normalized payload dict."""
    raw = json.dumps(config, sort_keys=True, separators=(",", ":"))
    import hashlib

    return hashlib.sha256(raw.encode()).hexdigest()


def ping() -> bool:
    try:
        return bool(redis_client.ping())
    except redis.RedisError as exc:
        logger.warning("Redis unavailable: %s", exc)
        return False
