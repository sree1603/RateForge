from redis.asyncio import BlockingConnectionPool, Redis


def create_redis_client(
    url: str, *, max_connections: int, timeout_seconds: float, decode_responses: bool = True
) -> Redis:
    """Redis client for the gateway.

    A *blocking* pool is used on purpose. The default pool raises immediately when every
    connection is busy, which under a traffic spike would surface as errors (and, with the
    rate limiter failing closed, as 503s) even though Redis itself is healthy. A blocking
    pool makes a request wait briefly for a free connection instead, and only gives up after
    `timeout_seconds`.

    The same timeout bounds connecting and reading, so a hung Redis fails fast.
    """
    pool = BlockingConnectionPool.from_url(
        url,
        max_connections=max_connections,
        timeout=timeout_seconds,
        socket_connect_timeout=timeout_seconds,
        socket_timeout=timeout_seconds,
        decode_responses=decode_responses,
    )
    return Redis(connection_pool=pool)
