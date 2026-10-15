import os
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from redis import Redis
from sunrise_infrastructure.security import RateGuard


def test_real_atomic_redis_threshold_and_resend():
    url = os.getenv("SUNRISE_TEST_REDIS_URL")
    if not url:
        pytest.skip("Real isolated Redis-compatible endpoint required")
    redis = Redis.from_url(url)
    prefix = "test:" + str(uuid4())
    guard = RateGuard(redis)
    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(lambda _: guard.fail(prefix), range(10)))
    assert sum(results) == 1
    assert int(redis.get("sunrise:fail:" + prefix)) == 10
    assert 3590 <= redis.ttl("sunrise:block:" + prefix) <= 3600
    guard.unblock(prefix)
