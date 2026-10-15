import hashlib
import hmac

from redis.exceptions import RedisError
from sunrise_core.errors import DomainError


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class RateGuard:
    FAIL_SCRIPT = """
    local n = redis.call('INCR', KEYS[1])
    if n == 1 then redis.call('EXPIRE', KEYS[1], ARGV[1]) end
    if n >= tonumber(ARGV[2]) then
      redis.call('SET', KEYS[2], '1', 'EX', ARGV[3])
      return 1
    end
    return 0
    """

    def __init__(self, redis):
        self.redis = redis

    def check(self, context):
        try:
            blocked = self.redis.exists("sunrise:block:" + context)
        except RedisError:
            raise DomainError(
                "protection_unavailable", "Защита входа временно недоступна.", 503, True
            ) from None
        if blocked:
            raise DomainError("login_blocked", "Слишком много попыток. Повторите через час.", 429)

    def resend(self, subject):
        try:
            allowed = self.redis.set("sunrise:resend:" + subject, "1", nx=True, ex=120)
        except RedisError:
            raise DomainError(
                "protection_unavailable", "Защита входа временно недоступна.", 503, True
            ) from None
        if not allowed:
            raise DomainError("resend_cooldown", "Повторный код можно запросить через 120 секунд.", 429)

    def fail(self, context, threshold=10, window=3600):
        try:
            return bool(
                self.redis.eval(
                    self.FAIL_SCRIPT,
                    2,
                    "sunrise:fail:" + context,
                    "sunrise:block:" + context,
                    window,
                    threshold,
                    3600,
                )
            )
        except RedisError:
            raise DomainError(
                "protection_unavailable", "Защита входа временно недоступна.", 503, True
            ) from None

    def unblock(self, context):
        self.redis.delete("sunrise:fail:" + context, "sunrise:block:" + context)

    def missing_route(self, context, path):
        script = """
        local n=redis.call('INCR',KEYS[1])
        if n==1 then redis.call('EXPIRE',KEYS[1],300) end
        redis.call('SADD',KEYS[2],ARGV[1])
        redis.call('EXPIRE',KEYS[2],300)
        if n>=60 and redis.call('SCARD',KEYS[2])>=20 then
          if redis.call('SET',KEYS[3],'1','EX',3600,'NX') then return 1 end
        end
        return 0
        """
        try:
            return bool(
                self.redis.eval(
                    script,
                    3,
                    "sunrise:fail:" + context,
                    "sunrise:paths:" + context,
                    "sunrise:block:" + context,
                    path,
                )
            )
        except RedisError:
            raise DomainError("protection_unavailable", "Защита временно недоступна.", 503, True) from None


def verify_callback(raw: bytes, timestamp: str, signature: str, secret: str, unix_now: int):
    try:
        fresh = abs(unix_now - int(timestamp)) <= 300
    except (ValueError, TypeError):
        fresh = False
    expected = hmac.new(secret.encode(), timestamp.encode() + b"." + raw, hashlib.sha256).hexdigest()
    if not secret or not fresh or not hmac.compare_digest(expected, signature):
        raise DomainError("invalid_signature", "Подпись уведомления не подтверждена.", 403)


class NamespacedRedis:
    """Only Redis keys are namespaced; Lua arguments and atomic operations stay intact."""

    def __init__(self, client, prefix):
        import re

        if not re.fullmatch(r"[a-z0-9_-]+:[a-z0-9_-]+:", prefix):
            raise ValueError("production_redis_namespace_invalid")
        self._client, self._prefix = client, prefix

    def _key(self, key):
        if not isinstance(key, str) or not key:
            raise ValueError("production_redis_key_invalid")
        return self._prefix + key

    def get(self, key):
        return self._client.get(self._key(key))

    def getdel(self, key):
        return self._client.getdel(self._key(key))

    def set(self, key, value, **kwargs):
        return self._client.set(self._key(key), value, **kwargs)

    def exists(self, *keys):
        return self._client.exists(*(self._key(key) for key in keys))

    def delete(self, *keys):
        return self._client.delete(*(self._key(key) for key in keys))

    def eval(self, script, numkeys, *args):
        if type(numkeys) is not int or not 0 <= numkeys <= len(args):
            raise ValueError("production_redis_keys_invalid")
        return self._client.eval(
            script, numkeys, *(self._key(key) for key in args[:numkeys]), *args[numkeys:]
        )

    def lock(self, name, **kwargs):
        return self._client.lock(self._key(name), **kwargs)

    def ping(self):
        return self._client.ping()

    def close(self):
        return self._client.close()


def configured_redis(config, policy=None, *, client=None):
    from redis import Redis

    from .settings import runtime_policy

    policy = policy or runtime_policy(config)
    raw = client if client is not None else Redis.from_url(config.redis_url)
    if policy and policy.production:
        policy.require()
        return NamespacedRedis(raw, policy.scope.redis.key_prefix)
    return raw
