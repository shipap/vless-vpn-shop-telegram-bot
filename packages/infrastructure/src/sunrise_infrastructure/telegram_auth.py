"""Подписанные Telegram-личности для общей серверной сессии."""

import base64
import hashlib
import hmac
import json
import logging
import re
import secrets
import time
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit

import httpx
import jwt
from redis.exceptions import RedisError
from sunrise_core.errors import DomainError

from .legal import validate_documents
from .security import digest

OIDC_ISSUER = "https://oauth.telegram.org"
FLOW_COOKIE = "sunrise_telegram_flow"
CALLBACK_PATH = "/api/v1/auth/telegram/callback"
LOGGER = logging.getLogger(__name__)


def invalid_telegram():
    return DomainError("telegram_auth_invalid", "Не удалось подтвердить вход через Telegram.", 400)


def unique_json(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


@dataclass(frozen=True)
class TelegramIdentity:
    telegram_id: int
    username: str | None
    auth_date: int
    replay_key: str


def validated_user(user):
    if not isinstance(user, dict):
        raise invalid_telegram()
    tid, username = user.get("id"), user.get("username")
    if type(tid) is not int or not 0 < tid < 2**63:
        raise invalid_telegram()
    if username is not None and (
        not isinstance(username, str) or not re.fullmatch(r"[A-Za-z0-9_]{5,32}", username)
    ):
        raise invalid_telegram()
    return tid, username


def validate_miniapp(init_data, bot_token, unix_now):
    try:
        if (
            not isinstance(init_data, str)
            or not 1 <= len(init_data) <= 16384
            or re.search(r"%(?![0-9A-Fa-f]{2})", init_data)
        ):
            raise ValueError("malformed data")
        pairs = parse_qsl(
            init_data, keep_blank_values=True, strict_parsing=True, errors="strict", max_num_fields=64
        )
        fields = dict(pairs)
        if len(fields) != len(pairs):
            raise ValueError("duplicate field")
        signature = fields.pop("hash")
        if not re.fullmatch(r"[0-9a-f]{64}", signature):
            raise ValueError("invalid hash")
        check = "\n".join(key + "=" + fields[key] for key in sorted(fields))
        secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
        expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise ValueError("invalid signature")
        if not re.fullmatch(r"[0-9]{1,12}", fields["auth_date"]):
            raise ValueError("invalid date")
        auth_date = int(fields["auth_date"])
        if not unix_now - 300 <= auth_date <= unix_now + 30:
            raise ValueError("expired data")
        user = json.loads(fields["user"], object_pairs_hook=unique_json)
        tid, username = validated_user(user)
        replay = hashlib.sha256((bot_token.split(":", 1)[0] + ":" + signature).encode()).hexdigest()
        return TelegramIdentity(tid, username, auth_date, replay)
    except (ValueError, KeyError, TypeError, UnicodeError):
        raise invalid_telegram() from None


class TelegramAuth:
    def __init__(self, accounts, transport=None):
        self.accounts, self.settings, self.redis = accounts, accounts.settings, accounts.guard.redis
        self.transport = transport
        from .settings import runtime_policy

        self.runtime_policy = runtime_policy(self.settings)

    @property
    def live(self):
        return bool(self.runtime_policy and self.runtime_policy.production)

    @property
    def miniapp_enabled(self):
        token = self.settings.bot_test_token or ""
        bot_ids = set(filter(None, (x.strip() for x in self.settings.test_bot_ids.split(","))))
        recipients = self._recipients()
        if self.live:
            token = self.settings.bot_token or ""
            bot_ids = {self.runtime_policy.scope.bot_id}
        return bool(
            self.settings.telegram_miniapp_enabled
            and re.fullmatch(r"[1-9][0-9]{0,18}:[A-Za-z0-9_-]{20,}", token)
            and token.split(":", 1)[0] in bot_ids
            and (self.live or recipients)
        )

    def _recipients(self):
        values = set(filter(None, (x.strip() for x in self.settings.test_telegram_recipients.split(","))))
        return (
            values if all(re.fullmatch(r"[1-9][0-9]{0,18}", v) and int(v) < 2**63 for v in values) else set()
        )

    @property
    def login_enabled(self):
        client_id = self.settings.telegram_login_client_id or ""
        redirect = urlsplit(self.settings.telegram_login_redirect_uri or "")
        origin = redirect.scheme + "://" + redirect.netloc
        return bool(
            re.fullmatch(r"[1-9][0-9]{0,18}", client_id)
            and client_id
            in (
                {self.runtime_policy.scope.bot_id}
                if self.live
                else {x.strip() for x in self.settings.test_bot_ids.split(",")}
            )
            and self.settings.telegram_login_client_secret
            and (self.live or self._recipients())
            and origin in self.settings.allowed_origins
            and redirect.scheme in {"https", "http"}
            and not redirect.username
            and not redirect.password
            and redirect.path == CALLBACK_PATH
            and not redirect.query
            and not redirect.fragment
        )

    def _claim_miniapp(self, identity):
        try:
            claimed = self.redis.set("sunrise:miniapp-used:" + identity.replay_key, "1", nx=True, ex=360)
        except RedisError:
            raise DomainError(
                "protection_unavailable", "Защита входа временно недоступна.", 503, True
            ) from None
        if not claimed:
            raise DomainError(
                "telegram_auth_replay", "Этот вход уже подтверждён. Откройте Mini App заново.", 409
            )

    def miniapp(
        self, init_data, ip, *, legal_accepted=False, documents=None, referral_code=None, browser_label=None
    ):
        if not self.miniapp_enabled:
            raise DomainError("telegram_auth_unavailable", "Вход через Telegram временно недоступен.", 503)
        if self.live:
            self.runtime_policy.require()
        self.accounts.guard.check("ip:" + digest(ip))
        try:
            identity = validate_miniapp(
                init_data,
                self.settings.bot_token if self.live else self.settings.bot_test_token,
                int(time.time()),
            )
            if not self.live and str(identity.telegram_id) not in self._recipients():
                raise DomainError(
                    "telegram_identity_not_allowed",
                    "Этот Telegram-аккаунт не разрешён на тестовом стенде.",
                    403,
                )
        except DomainError:
            self.accounts.guard.fail("ip:" + digest(ip), threshold=30, window=600)
            raise
        return self.accounts.telegram_login(
            identity,
            ip,
            legal_accepted=legal_accepted,
            documents=documents,
            allow_current=True,
            referral_code=referral_code,
            claim=lambda: self._claim_miniapp(identity),
            auth_method="telegram_miniapp",
            browser_label=browser_label,
        )

    def start(self, documents, legal_accepted, ip, *, referral_code=None):
        if not self.login_enabled:
            raise DomainError("telegram_auth_unavailable", "Вход через Telegram пока не настроен.", 503)
        self.accounts.guard.check("ip:" + digest(ip))
        self._budget("start", ip)
        if legal_accepted is not True or not documents:
            raise DomainError(
                "legal_consent_required",
                "Прочитайте документы и подтвердите согласие перед входом.",
                409,
                next_action="legal",
            )
        from .commerce import Commerce

        supplied = validate_documents(documents)
        current = Commerce(self.accounts.db).legal.public()
        if not current["configured"]:
            raise DomainError("legal_not_configured", "Документы сервиса ещё не настроены.", 503)
        if supplied != current["documents"]:
            raise DomainError(
                "legal_versions_stale",
                "Документы обновились. Прочитайте текущие версии.",
                409,
                next_action="legal",
            )
        state, browser, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(4))
        flow = {
            "browser_hash": digest(browser),
            "nonce": nonce,
            "verifier": verifier,
            "documents": supplied,
            "referral_code": referral_code,
            "expires_at": int(time.time()) + 600,
        }
        try:
            if not self.redis.set("sunrise:oidc-flow:" + digest(state), json.dumps(flow), ex=600, nx=True):
                raise DomainError("protection_unavailable", "Защита входа временно недоступна.", 503, True)
        except RedisError:
            raise DomainError(
                "protection_unavailable", "Защита входа временно недоступна.", 503, True
            ) from None
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        params = {
            "client_id": self.settings.telegram_login_client_id,
            "redirect_uri": self.settings.telegram_login_redirect_uri,
            "response_type": "code",
            "scope": "openid profile",
            "state": state,
            "nonce": nonce,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        return {"authorization_url": OIDC_ISSUER + "/auth?" + urlencode(params)}, browser

    def _budget(self, operation, ip):
        script = """
        local n = redis.call('INCR', KEYS[1])
        if n == 1 then redis.call('EXPIRE', KEYS[1], 60) end
        return n
        """
        try:
            attempts = self.redis.eval(script, 1, "sunrise:oidc-budget:" + operation + ":" + digest(ip))
        except RedisError:
            raise DomainError(
                "protection_unavailable", "Защита входа временно недоступна.", 503, True
            ) from None
        if attempts > 20:
            raise DomainError(
                "telegram_auth_rate_limited", "Слишком много запросов. Повторите через минуту.", 429
            )

    def callback(self, code, state, browser, ip, *, browser_label=None):
        if not self.login_enabled:
            raise DomainError("telegram_auth_unavailable", "Вход через Telegram пока не настроен.", 503)
        self.accounts.guard.check("ip:" + digest(ip))
        self._budget("callback", ip)
        try:
            return self._callback(code, state, browser, ip, browser_label=browser_label)
        except DomainError as exc:
            if exc.code in {"telegram_auth_invalid", "telegram_identity_not_allowed"}:
                self.accounts.guard.fail("ip:" + digest(ip), threshold=30, window=600)
            raise

    def _callback(self, code, state, browser, ip, *, browser_label=None):
        diagnostic = {"stage": "callback_input"}
        try:
            return self._callback_flow(code, state, browser, ip, diagnostic, browser_label=browser_label)
        except Exception as error:
            reason = (
                {
                    "telegram_auth_invalid": "auth_invalid",
                    "telegram_auth_unavailable": "provider_unavailable",
                    "protection_unavailable": "protection_unavailable",
                    "telegram_identity_not_allowed": "identity_denied",
                    "legal_versions_stale": "legal_versions_stale",
                    "legal_consent_required": "legal_consent_required",
                }.get(error.code, "application_denied")
                if isinstance(error, DomainError)
                else "unexpected_error"
            )
            LOGGER.warning("telegram_oidc_callback_denied %s", json.dumps(diagnostic | {"reason": reason}))
            raise

    def _callback_flow(self, code, state, browser, ip, diagnostic, *, browser_label=None):
        if not all(isinstance(v, str) and 1 <= len(v) <= 2048 for v in (code, state, browser)):
            raise invalid_telegram()
        diagnostic["stage"] = "browser_flow"
        key = "sunrise:oidc-flow:" + digest(state)
        try:
            raw = self.redis.get(key)
            if not raw:
                raise invalid_telegram()
            flow = json.loads(raw)
            if (
                not hmac.compare_digest(flow["browser_hash"], digest(browser))
                or flow["expires_at"] <= time.time()
            ):
                raise invalid_telegram()
            if self.redis.getdel(key) != raw:
                raise invalid_telegram()
        except RedisError:
            raise DomainError(
                "protection_unavailable", "Защита входа временно недоступна.", 503, True
            ) from None
        except (ValueError, KeyError, TypeError):
            raise invalid_telegram() from None
        transport = self.transport or TelegramOIDCTransport()
        diagnostic["stage"] = "token_exchange"
        token = transport.token(
            code=code,
            verifier=flow["verifier"],
            client_id=self.settings.telegram_login_client_id,
            client_secret=self.settings.telegram_login_client_secret,
            redirect_uri=self.settings.telegram_login_redirect_uri,
        )
        diagnostic["stage"] = "jwks_fetch"
        keys = transport.keys()
        diagnostic["stage"] = "id_token_validation"
        identity = validate_oidc(
            token, keys, self.settings.telegram_login_client_id, flow["nonce"], int(time.time())
        )
        diagnostic["stage"] = "identity_admission"
        if self.live:
            self.runtime_policy.require()
        if not self.live and str(identity.telegram_id) not in self._recipients():
            raise DomainError(
                "telegram_identity_not_allowed", "Этот Telegram-аккаунт не разрешён на тестовом стенде.", 403
            )
        diagnostic["stage"] = "session_creation"
        return self.accounts.telegram_login(
            identity,
            ip,
            legal_accepted=True,
            documents=flow["documents"],
            referral_code=flow.get("referral_code"),
            auth_method="telegram_oidc",
            browser_label=browser_label,
        )


def validate_oidc(token, keys, client_id, nonce, unix_now):
    metadata = dict.fromkeys(
        (
            "alg_present",
            "alg_string",
            "alg_rs256",
            "kid_present",
            "kid_string",
            "claims_parsed",
            "id_present",
            "id_integer",
            "id_string",
            "id_canonical_decimal",
            "nonce_present",
            "nonce_string",
            "nonce_matches",
            "aud_present",
            "aud_string",
            "aud_matches",
            "issuer_matches",
            "iat_integer",
            "iat_in_window",
            "exp_integer",
            "exp_valid",
            "jwt_validated",
            "id_in_range",
            "username_present",
            "username_string",
            "username_current_pattern_matches",
        ),
        False,
    )
    reason = "token_shape"
    try:
        if not isinstance(token, str) or not 1 <= len(token) <= 16384 or not isinstance(keys, dict):
            raise ValueError("invalid token")
        header = jwt.get_unverified_header(token)
        metadata.update(
            alg_present="alg" in header,
            alg_string=isinstance(header.get("alg"), str),
            alg_rs256=header.get("alg") == "RS256",
            kid_present="kid" in header,
            kid_string=isinstance(header.get("kid"), str),
        )
        reason = "algorithm"
        if header.get("alg") != "RS256" or any(key in header for key in ("jku", "x5u", "crit")):
            raise ValueError("unsupported algorithm")
        # Unverified claims are projected to booleans only; the original verification below still decides access.
        reason = "jwt_payload"
        hint = jwt.decode(token, options={"verify_signature": False})
        metadata.update(
            claims_parsed=True,
            id_present="id" in hint,
            id_integer=type(hint.get("id")) is int,
            id_string=isinstance(hint.get("id"), str),
            id_canonical_decimal=isinstance(hint.get("id"), str)
            and re.fullmatch(r"[1-9][0-9]{0,18}", hint["id"]) is not None
            and int(hint["id"]) < 2**63,
            nonce_present="nonce" in hint,
            nonce_string=isinstance(hint.get("nonce"), str),
            nonce_matches=hint.get("nonce") == nonce,
            aud_present="aud" in hint,
            aud_string=isinstance(hint.get("aud"), str),
            aud_matches=hint.get("aud") == client_id,
            issuer_matches=hint.get("iss") == OIDC_ISSUER,
            iat_integer=type(hint.get("iat")) is int,
            iat_in_window=type(hint.get("iat")) is int and unix_now - 600 <= hint["iat"] <= unix_now + 30,
            exp_integer=type(hint.get("exp")) is int,
            exp_valid=type(hint.get("exp")) is int and hint["exp"] > unix_now,
            id_in_range=type(hint.get("id")) is int and 0 < hint["id"] < 2**63,
            username_present="preferred_username" in hint,
            username_string=isinstance(hint.get("preferred_username"), str),
            username_current_pattern_matches=isinstance(hint.get("preferred_username"), str)
            and re.fullmatch(r"[A-Za-z0-9_]{5,32}", hint["preferred_username"]) is not None,
        )
        reason = "key_set"
        kid, jwks = header.get("kid"), keys.get("keys")
        if (
            not isinstance(kid, str)
            or not 1 <= len(kid) <= 200
            or not isinstance(jwks, list)
            or not 1 <= len(jwks) <= 20
        ):
            raise ValueError("invalid key set")
        matching = [
            k
            for k in jwks
            if isinstance(k, dict)
            and k.get("kid") == kid
            and k.get("kty") == "RSA"
            and k.get("alg", "RS256") == "RS256"
            and k.get("use", "sig") == "sig"
        ]
        reason = "key_match"
        if len(matching) != 1:
            raise ValueError("unknown key")
        reason = "key_material"
        public_key = jwt.PyJWK.from_dict(matching[0], algorithm="RS256").key
        reason = "weak_key"
        if public_key.key_size < 2048:
            raise ValueError("weak key")
        reason = "jwt_validation"
        claims = jwt.decode(
            token,
            public_key,
            algorithms=["RS256"],
            audience=client_id,
            issuer=OIDC_ISSUER,
            options={
                "require": ["exp", "iat", "iss", "aud", "sub", "nonce", "id"],
                "strict_aud": True,
                "verify_iat": False,
            },
        )
        metadata["jwt_validated"] = True
        reason = "claim_values"
        if (
            type(claims["iat"]) is not int
            or not unix_now - 600 <= claims["iat"] <= unix_now + 30
            or type(claims["exp"]) is not int
            or claims["exp"] <= unix_now
            or not isinstance(claims["sub"], str)
            or not 1 <= len(claims["sub"]) <= 255
            or not isinstance(claims["nonce"], str)
            or not hmac.compare_digest(claims["nonce"], nonce)
        ):
            raise ValueError("invalid claims")
        reason = "profile"
        profile_id = claims["id"]
        if isinstance(profile_id, str):
            if not re.fullmatch(r"[1-9][0-9]{0,18}", profile_id) or int(profile_id) >= 2**63:
                raise invalid_telegram()
            profile_id = int(profile_id)
        tid, username = validated_user({"id": profile_id, "username": claims.get("preferred_username")})
        return TelegramIdentity(tid, username, claims["iat"], digest(token))
    except DomainError:
        LOGGER.warning("telegram_oidc_validation_denied %s", json.dumps(metadata | {"reason": reason}))
        raise
    except (jwt.PyJWTError, ValueError, KeyError, TypeError, AttributeError) as error:
        if isinstance(error, jwt.MissingRequiredClaimError):
            reason = {
                claim: "missing_" + claim for claim in ("exp", "iat", "iss", "aud", "sub", "nonce", "id")
            }.get(error.claim, "missing_claim")
        elif isinstance(error, jwt.InvalidSignatureError):
            reason = "signature"
        elif isinstance(error, jwt.InvalidAudienceError):
            reason = "audience"
        elif isinstance(error, jwt.InvalidIssuerError):
            reason = "issuer"
        elif isinstance(error, jwt.ExpiredSignatureError):
            reason = "expired"
        LOGGER.warning("telegram_oidc_validation_denied %s", json.dumps(metadata | {"reason": reason}))
        raise invalid_telegram() from None


class TelegramOIDCTransport:
    def _json(self, response):
        if response.status_code != 200:
            raise DomainError(
                "telegram_auth_unavailable", "Сервис входа Telegram временно недоступен.", 503, True
            )
        body = bytearray()
        for chunk in response.iter_bytes(chunk_size=16384):
            if len(body) + len(chunk) > 65536:
                raise ValueError("oversized Telegram response")
            body.extend(chunk)
        value = json.loads(body)
        if not isinstance(value, dict):
            raise ValueError("invalid Telegram response")
        return value

    def token(self, *, code, verifier, client_id, client_secret, redirect_uri):
        try:
            with httpx.Client(timeout=5, follow_redirects=False) as client:
                with client.stream(
                    "POST",
                    OIDC_ISSUER + "/token",
                    auth=(client_id, client_secret),
                    data={
                        "grant_type": "authorization_code",
                        "code": code,
                        "redirect_uri": redirect_uri,
                        "client_id": client_id,
                        "code_verifier": verifier,
                    },
                ) as response:
                    value = self._json(response)
            token = value["id_token"]
            if not isinstance(token, str) or not 1 <= len(token) <= 16384:
                raise ValueError("invalid ID token")
            return token
        except (httpx.HTTPError, ValueError, KeyError):
            raise DomainError(
                "telegram_auth_unavailable", "Сервис входа Telegram временно недоступен.", 503, True
            ) from None

    def keys(self):
        try:
            with httpx.Client(timeout=5, follow_redirects=False) as client:
                with client.stream("GET", OIDC_ISSUER + "/.well-known/jwks.json") as response:
                    return self._json(response)
        except (httpx.HTTPError, ValueError):
            raise DomainError(
                "telegram_auth_unavailable", "Сервис входа Telegram временно недоступен.", 503, True
            ) from None
