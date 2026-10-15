"""Разделение платёжных callback без хранения событий или доступа к БД."""

import hashlib
import hmac
import json
import re
import secrets
import time
from contextlib import asynccontextmanager
from urllib.parse import urlparse
from uuid import UUID

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sunrise_core.errors import DomainError
from sunrise_infrastructure.scoped_test import load_scoped_test_scope
from sunrise_infrastructure.security import verify_callback


def remna_resource_digest(secret, kind, identifier):
    if kind not in {"user", "node"} or not isinstance(identifier, str) or not 1 <= len(identifier) <= 64:
        raise ValueError("remna_resource_invalid")
    return hmac.new(
        secret.encode(), f"sunrise:remna:resource:v1:{kind}:{identifier}".encode(), hashlib.sha256
    ).hexdigest()


def _remna_probe_signature(secret, domain, raw, timestamp, nonce):
    if (
        not isinstance(secret, str)
        or len(secret) < 16
        or not isinstance(raw, bytes)
        or not 1 <= len(raw) <= 1024
        or not isinstance(timestamp, str)
        or not re.fullmatch(r"[0-9]{1,12}", timestamp)
        or not isinstance(nonce, str)
        or not re.fullmatch(r"[a-f0-9]{16,64}", nonce)
    ):
        raise ValueError("remna_probe_invalid")
    payload = f"sunrise:remna:{domain}:v1:{timestamp}.{nonce}.".encode() + raw
    return hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()


def sign_remna_probe_request(secret, raw, timestamp, nonce):
    return _remna_probe_signature(secret, "probe-request", raw, timestamp, nonce)


def verify_remna_probe_request(secret, raw, timestamp, nonce, signature, current_time):
    expected = sign_remna_probe_request(secret, raw, timestamp, nonce)
    if (
        abs(current_time - int(timestamp)) > 30
        or not isinstance(signature, str)
        or not re.fullmatch(r"[a-f0-9]{64}", signature)
        or not hmac.compare_digest(signature, expected)
    ):
        raise ValueError("remna_probe_invalid")


def sign_remna_probe_response(secret, raw_response, timestamp, nonce):
    return _remna_probe_signature(secret, "probe-response", raw_response, timestamp, nonce)


class RemnawaveRouterSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SUNRISE_REMNA_ROUTER_", extra="forbid")
    signing_secret: SecretStr = Field(min_length=16, repr=False)
    scoped_test_admission_file: str
    scoped_test_admission_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    primary_url: str
    primary_host: str
    test_url: str
    test_host: str
    membership_url: str
    membership_host: str
    allowed_backend_hosts: list[str] = Field(min_length=1)
    ingress_hosts: list[str] = Field(min_length=1)
    max_body_bytes: int = Field(default=262144, ge=1024, le=1048576)
    max_age_seconds: int = Field(default=300, ge=30, le=900)

    @model_validator(mode="after")
    def fixed_targets(self):
        for value, path in (
            (self.primary_url, "/api/v1/webhooks/remnawave"),
            (self.test_url, "/api/v1/webhooks/remnawave"),
            (self.membership_url, "/api/v1/internal/remnawave-membership"),
        ):
            parsed = urlparse(value)
            if (
                parsed.scheme != "http"
                or parsed.hostname not in self.allowed_backend_hosts
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
                or parsed.port != 8000
                or parsed.path != path
            ):
                raise ValueError("remna_router_target_denied")
        target, probe = urlparse(self.test_url), urlparse(self.membership_url)
        if self.primary_url == self.test_url or (target.hostname, target.port) != (
            probe.hostname,
            probe.port,
        ):
            raise ValueError("remna_router_target_denied")
        for host in [
            self.primary_host,
            self.test_host,
            self.membership_host,
            *self.ingress_hosts,
            *self.allowed_backend_hosts,
        ]:
            if not isinstance(host, str) or not re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,252}", host):
                raise ValueError("remna_router_host_denied")
        return self


def create_remnawave_router(settings=None, *, transport=None, clock=time.time):
    """Изолированный relay Remnawave; платёжный router ниже не меняется."""
    from starlette.middleware.trustedhost import TrustedHostMiddleware
    from sunrise_infrastructure.panel import PanelReview
    from sunrise_infrastructure.panel_events import NODE_EVENTS, USER_EVENTS, unique_object
    from sunrise_infrastructure.panel_v3 import instant, numeric_id

    try:
        config = settings or RemnawaveRouterSettings()
        initial_scope, _ = load_scoped_test_scope(config)
    except (ValueError, OSError):
        raise RuntimeError("remna_router_configuration_denied") from None
    secret = config.signing_secret.get_secret_value()

    @asynccontextmanager
    async def lifespan(app):
        async with httpx.AsyncClient(
            transport=transport, timeout=10, follow_redirects=False, trust_env=False
        ) as client:
            app.state.forwarder = client
            yield

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=config.ingress_hosts)

    def current_scope():
        actual, _ = load_scoped_test_scope(config)
        if actual != initial_scope:
            raise ValueError("remna_router_scope_changed")
        return actual

    @app.get("/health")
    def health():
        try:
            scope = current_scope()
            return {
                "status": "ok",
                "role": "remnawave_callback_router",
                "scope_id": scope.scope_id,
                "admission_sha256": config.scoped_test_admission_sha256,
            }
        except (ValueError, OSError):
            return JSONResponse({"code": "test_backend_not_admitted"}, status_code=503)

    async def forward(request, raw, headers, test=False):
        target = config.test_url if test else config.primary_url
        async with request.app.state.forwarder.stream(
            "POST",
            target,
            content=raw,
            headers={
                **headers,
                "host": config.test_host if test else config.primary_host,
                "content-type": "application/json",
            },
        ) as response:
            body = bytearray()
            async for chunk in response.aiter_bytes():
                if len(body) + len(chunk) > 65536:
                    raise ValueError("remna_backend_response_large")
                body.extend(chunk)
            return Response(
                bytes(body),
                status_code=response.status_code,
                media_type=response.headers.get("content-type", "application/json"),
            )

    async def membership(request, resource_kind, identifier):
        scope = current_scope()
        payload = {
            "scope_id": scope.scope_id,
            "admission_sha256": config.scoped_test_admission_sha256,
            "resource_kind": resource_kind,
            "resource_digest": remna_resource_digest(secret, resource_kind, identifier),
        }
        raw = json.dumps(payload, separators=(",", ":")).encode()
        timestamp, nonce = str(int(clock())), secrets.token_hex(16)
        signature = sign_remna_probe_request(secret, raw, timestamp, nonce)
        async with request.app.state.forwarder.stream(
            "POST",
            config.membership_url,
            content=raw,
            headers={
                "host": config.membership_host,
                "content-type": "application/json",
                "x-sunrise-remna-timestamp": timestamp,
                "x-sunrise-remna-nonce": nonce,
                "x-sunrise-remna-signature": signature,
            },
        ) as response:
            if response.status_code != 200:
                raise ValueError("remna_probe_unavailable")
            content = bytearray()
            async for chunk in response.aiter_bytes():
                if len(content) + len(chunk) > 1024:
                    raise ValueError("remna_probe_invalid")
                content.extend(chunk)
            actual_signature = response.headers.get("x-sunrise-remna-response-signature", "")
        expected = sign_remna_probe_response(secret, bytes(content), timestamp, nonce)
        if not hmac.compare_digest(actual_signature, expected) or abs(int(clock()) - int(timestamp)) > 30:
            raise ValueError("remna_probe_invalid")
        body = json.loads(content, object_pairs_hook=unique_object)
        if (
            not isinstance(body, dict)
            or set(body) != set(payload) | {"admitted"}
            or any(body.get(key) != value for key, value in payload.items())
            or type(body.get("admitted")) is not bool
        ):
            raise ValueError("remna_probe_invalid")
        current_scope()
        return body["admitted"]

    @app.post("/api/v1/webhooks/remnawave")
    async def callback(request: Request):
        raw = bytearray()
        async for chunk in request.stream():
            if len(raw) + len(chunk) > config.max_body_bytes:
                return JSONResponse({"code": "body_too_large"}, status_code=413)
            raw.extend(chunk)
        raw = bytes(raw)
        signature = request.headers.get("x-remnawave-signature", "")
        if not re.fullmatch(r"[a-f0-9]{64}", signature) or not hmac.compare_digest(
            signature, hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        ):
            return JSONResponse({"code": "invalid_signature"}, status_code=401)
        try:
            payload = json.loads(raw, object_pairs_hook=unique_object)
            stamp = payload.get("timestamp") if isinstance(payload, dict) else None
            if (
                not isinstance(stamp, str)
                or stamp != request.headers.get("x-remnawave-timestamp")
                or abs(clock() - instant(stamp).timestamp()) > config.max_age_seconds
                or not isinstance(payload.get("event"), str)
                or not isinstance(payload.get("data"), dict)
                or payload.get("scope") != payload["event"].split(".")[0]
            ):
                raise ValueError("remna_event_invalid")
            kind, data = payload["event"], payload["data"]
            resource = None
            if kind in USER_EVENTS:
                user = data.get("user") if kind.startswith("user_hwid_devices.") else data
                resource = ("user", numeric_id(user.get("id"))) if isinstance(user, dict) else None
                if resource is None:
                    raise ValueError("remna_event_invalid")
                if kind.startswith("user_hwid_devices."):
                    device = data.get("hwidUserDevice")
                    if not isinstance(device, dict) or numeric_id(device.get("userId")) != resource[1]:
                        raise ValueError("remna_event_invalid")
            elif kind in NODE_EVENTS:
                identifier = data.get("uuid")
                if isinstance(identifier, str) and str(UUID(identifier)) == identifier:
                    resource = ("node", identifier)
        except (ValueError, TypeError, UnicodeError, RecursionError, AttributeError, PanelReview):
            return JSONResponse({"code": "event_invalid"}, status_code=400)
        forwarded_headers = {"x-remnawave-signature": signature, "x-remnawave-timestamp": stamp}
        try:
            if resource is None:
                return await forward(request, raw, forwarded_headers)
            try:
                admitted = await membership(request, *resource)
            except (httpx.HTTPError, ValueError, OSError, TypeError, RecursionError):
                # Основной поток получает исходное событие и при отказе тестовой проверки.
                await forward(request, raw, forwarded_headers)
                return JSONResponse({"code": "membership_unavailable"}, status_code=503)
            if resource[0] == "user":
                return await forward(request, raw, forwarded_headers, test=admitted)
            primary = await forward(request, raw, forwarded_headers)
            if not admitted:
                return primary
            test = await forward(request, raw, forwarded_headers, test=True)
            return (
                primary
                if 200 <= primary.status_code < 300 and 200 <= test.status_code < 300
                else JSONResponse({"code": "backend_unavailable"}, status_code=503)
            )
        except (httpx.HTTPError, ValueError, TypeError):
            return JSONResponse({"code": "backend_unavailable"}, status_code=503)

    return app


class CallbackRouterSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SUNRISE_CALLBACK_ROUTER_", extra="forbid")
    signing_secret: SecretStr = Field(repr=False)
    scoped_test_admission_file: str
    scoped_test_admission_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    primary_url: str
    primary_host: str
    test_url: str
    test_host: str
    allowed_backend_hosts: list[str]

    @model_validator(mode="after")
    def fixed_targets(self):
        for value in (self.primary_url, self.test_url):
            parsed = urlparse(value)
            if (
                parsed.scheme != "http"
                or parsed.hostname not in self.allowed_backend_hosts
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
                or parsed.port not in {8000}
                or parsed.path != "/api/v1/webhooks/rollypay"
            ):
                raise ValueError("callback_router_target_denied")
        if not self.signing_secret.get_secret_value() or self.primary_url == self.test_url:
            raise ValueError("callback_router_configuration_denied")
        for host in (self.primary_host, self.test_host):
            if not host or any(ch not in "abcdefghijklmnopqrstuvwxyz0123456789.-" for ch in host):
                raise ValueError("callback_router_host_denied")
        return self


def create_router(settings=None, *, transport=None):
    try:
        config = settings or CallbackRouterSettings()
        initial_scope, _ = load_scoped_test_scope(config)
    except (ValueError, OSError):
        raise RuntimeError("callback_router_configuration_denied") from None

    @asynccontextmanager
    async def lifespan(app):
        async with httpx.AsyncClient(
            transport=transport, timeout=10, follow_redirects=False, trust_env=False
        ) as client:
            app.state.forwarder = client
            yield

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.post("/api/v1/webhooks/rollypay")
    async def callback(request: Request):
        raw = bytearray()
        async for chunk in request.stream():
            if len(raw) + len(chunk) > 65536:
                return JSONResponse({"code": "body_too_large"}, status_code=413)
            raw.extend(chunk)
        raw = bytes(raw)
        timestamp, signature = request.headers.get("x-timestamp", ""), request.headers.get("x-signature", "")
        try:
            verify_callback(
                raw, timestamp, signature, config.signing_secret.get_secret_value(), int(time.time())
            )
        except DomainError:
            return JSONResponse({"code": "invalid_signature"}, status_code=403)

        def unique_fields(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate_field")
                result[key] = value
            return result

        try:
            event = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_fields)
            if not isinstance(event, dict):
                raise ValueError("invalid_event")
        except (ValueError, UnicodeError):
            return JSONResponse({"code": "event_invalid"}, status_code=400)
        order_id = event.get("order_id")
        test_order = isinstance(order_id, str) and order_id.startswith(initial_scope.order_prefix)
        if test_order:
            try:
                current_scope, _ = load_scoped_test_scope(config)
                if current_scope != initial_scope:
                    raise ValueError("callback_router_scope_changed")
            except (ValueError, OSError):
                return JSONResponse({"code": "test_backend_not_admitted"}, status_code=503)
        else:
            try:
                if not isinstance(order_id, str) or str(UUID(order_id)) != order_id:
                    raise ValueError("callback_order_unclassified")
            except ValueError:
                # An unclassifiable event must not leak into the primary inbox.
                # The provider retries; no false acknowledgement is returned.
                return JSONResponse({"code": "callback_order_unclassified"}, status_code=503)
        target = config.test_url if test_order else config.primary_url
        headers = {
            "host": config.test_host if test_order else config.primary_host,
            "content-type": "application/json",
            "x-timestamp": timestamp,
            "x-signature": signature,
        }
        try:
            result = await request.app.state.forwarder.post(target, content=raw, headers=headers)
        except httpx.HTTPError:
            # Результат запроса может быть неизвестен; повтор доверяет idempotency backend.
            return JSONResponse({"code": "backend_unavailable"}, status_code=503)
        return Response(
            content=result.content,
            status_code=result.status_code,
            media_type=result.headers.get("content-type", "application/json"),
        )

    return app
