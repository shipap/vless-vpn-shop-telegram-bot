import hmac
import json
import logging
import re
import time
from datetime import datetime, timedelta
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from fastapi import Depends, FastAPI, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import ValidationError
from sqlalchemy import select, text
from starlette.middleware.trustedhost import TrustedHostMiddleware
from sunrise_core.errors import DomainError, missing
from sunrise_infrastructure.account_features import AccountsFeatures
from sunrise_infrastructure.accounts import Accounts
from sunrise_infrastructure.admin import Admin
from sunrise_infrastructure.broadcast_media_registry import (
    BroadcastMediaRegistry,
    MediaReference,
    configured_media_store,
)
from sunrise_infrastructure.broadcasts import BroadcastService
from sunrise_infrastructure.commerce import Commerce, account_dto
from sunrise_infrastructure.db import Database, Inbox, now
from sunrise_infrastructure.emoji import configured_emoji
from sunrise_infrastructure.mail import configured_mail
from sunrise_infrastructure.notifications import NotificationService
from sunrise_infrastructure.panel import configured_panel
from sunrise_infrastructure.recurring import Recurring
from sunrise_infrastructure.security import RateGuard, digest, verify_callback
from sunrise_infrastructure.settings import Settings
from sunrise_infrastructure.staff_permissions import StaffPermissions, route_capability
from sunrise_infrastructure.telegram_auth import FLOW_COOKIE, TelegramAuth

from .schemas import (
    AuthDto,
    BonusAdjustment,
    BroadcastEstimate,
    BroadcastRequest,
    BroadcastSelfRequest,
    CatalogDto,
    CodeRequest,
    Compensation,
    ConfigDto,
    ConnectionInstructionsDto,
    CreditAdjustment,
    DashboardDto,
    DeletionNoticeDismissRequest,
    DeviceModerationRequest,
    DiscountPreview,
    DiscountsUpdate,
    EmailRequest,
    ItemsDto,
    LegalAccept,
    LegalDto,
    LegalStatusDto,
    LegalUpdate,
    LinkEmailChallengeDto,
    LinkEmailCodeRequest,
    LinkEmailRequest,
    LinkEmailVerificationDto,
    LinkRequest,
    MfaEnrollmentDto,
    MfaRequest,
    MfaStatusDto,
    MiniAppRequest,
    NodeNewsDto,
    OrderDto,
    OrderRequest,
    OrdinaryContentUpdate,
    PanelEventRoutes,
    PanelEventRoutesResponse,
    PanelEventRoutesUpdate,
    PanelMappingDto,
    PanelMappingUpdate,
    PendingPromoDto,
    PeriodsUpdate,
    ProductCreate,
    ProductUpdate,
    ProductValues,
    PromoActivationRequest,
    PromoClaimDto,
    PromoClaimRequest,
    ProviderOperationReviewRequest,
    PublicContentUpdate,
    QuoteDto,
    QuoteRequest,
    Reason,
    RecurringRequest,
    ReferralDto,
    RefundPolicyUpdate,
    RefundResolve,
    RegistryUpdate,
    RemnaMembershipProbe,
    RetentionUpdate,
    TelegramConsent,
    TelegramStartDto,
    ThemeUpdate,
    ToggleRequest,
)


def create_app(
    settings=None,
    redis_client=None,
    payment_provider=None,
    panel_provider=None,
    mail_transport=None,
    emoji_assets=None,
    telegram_transport=None,
):
    config = settings or Settings()
    db = Database(config.database_url)
    from sunrise_infrastructure.providers import configured_payments
    from sunrise_infrastructure.security import configured_redis
    from sunrise_infrastructure.settings import configured_runtime

    policy = configured_runtime(config, db)
    redis_client = configured_redis(config, policy, client=redis_client)
    guard = RateGuard(redis_client)
    mail = mail_transport or configured_mail(
        config,
        media_store=configured_media_store(config)
        if config.smtp_test_authorized or policy.production
        else None,
    )
    commerce = Commerce(
        db,
        config.local_test,
        panel_provider=panel_provider or configured_panel(config),
        mail_transport=mail,
        notification_allowlist=config.email_allowlist,
        device_bindings=config.panel_test_device_bindings,
        referral_bot_url=config.bot_url,
        sandbox_payments=config.sandbox_payments_enabled,
        owner_id=config.owner_id,
        public_settings=config,
    )
    accounts = Accounts(db, guard, config, mail_transport=mail)
    telegram = TelegramAuth(accounts, transport=telegram_transport)
    assets = emoji_assets or configured_emoji(config)
    commerce.accounts = accounts
    admin = Admin(commerce, guard, accounts)
    staff = StaffPermissions(accounts)
    notifications = NotificationService(db, config, mail_transport=mail)
    account_features = AccountsFeatures(accounts)
    broadcasts = BroadcastService(commerce, config)
    provider = payment_provider or configured_payments(config, policy)
    recurring = Recurring(commerce, provider)
    app = FastAPI(
        title=config.instance.app_name, version="1.0.0", docs_url=None, redoc_url=None, openapi_url=None
    )
    from .instance_routes import register_instance_routes

    register_instance_routes(app, config)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=config.allowed_hosts)
    app.state.commerce = commerce
    app.state.accounts = accounts
    app.state.telegram_auth = telegram
    app.state.settings = config

    @app.post("/api/v1/internal/remnawave-membership", include_in_schema=False)
    async def remnawave_membership(request: Request):
        """Answer the local callback relay without accepting raw panel identities."""
        from .remna_membership import admitted_resource_in
        from .webhook_router import sign_remna_probe_response, verify_remna_probe_request

        peer = request.client.host if request.client else ""
        host = request.headers.get("host", "")
        if (
            peer not in {"127.0.0.1", "::1"}
            or host not in {"127.0.0.1", "127.0.0.1:8000", "localhost", "localhost:8000"}
            or config.runtime_mode != "scoped_test"
            or not policy.scoped_payments
            or not config.panel_webhook_test_authorized
            or not config.panel_webhook_secret
            or len(config.panel_webhook_secret) < 16
        ):
            return JSONResponse({"code": "probe_unavailable"}, status_code=404)
        raw = bytearray()
        async for chunk in request.stream():
            if len(raw) + len(chunk) > 1024:
                return JSONResponse({"code": "probe_invalid"}, status_code=413)
            raw.extend(chunk)
        raw = bytes(raw)
        stamp = request.headers.get("x-sunrise-remna-timestamp", "")
        nonce = request.headers.get("x-sunrise-remna-nonce", "")
        signature = request.headers.get("x-sunrise-remna-signature", "")
        try:
            verify_remna_probe_request(
                config.panel_webhook_secret, raw, stamp, nonce, signature, int(time.time())
            )

            def unique_fields(pairs):
                value = {}
                for key, item in pairs:
                    if key in value:
                        raise ValueError("duplicate_probe_field")
                    value[key] = item
                return value

            body = RemnaMembershipProbe.model_validate(
                json.loads(raw.decode("utf-8"), object_pairs_hook=unique_fields)
            )
            if (
                body.scope_id != policy.scope.scope_id
                or body.admission_sha256 != config.scoped_test_admission_sha256
            ):
                raise ValueError("probe_scope_changed")
        except (ValueError, UnicodeError, ValidationError):
            return JSONResponse({"code": "probe_invalid"}, status_code=403)
        try:
            with db.transaction() as session:
                session.execute(text("SET TRANSACTION READ ONLY"))
                admitted = admitted_resource_in(
                    session,
                    commerce,
                    config,
                    body.resource_kind,
                    body.resource_digest,
                    config.panel_webhook_secret,
                )
        except Exception:  # noqa: BLE001 -- no DB, identity, or admission details leave this route
            return JSONResponse({"code": "probe_unavailable"}, status_code=503)
        payload = body.model_dump() | {"admitted": admitted}
        encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        proof = sign_remna_probe_response(config.panel_webhook_secret, encoded, stamp, nonce)
        return Response(
            content=encoded,
            media_type="application/json",
            headers={"X-Sunrise-Remna-Response-Signature": proof, "Cache-Control": "no-store"},
        )

    @app.get("/api/v1/content/emoji/{emoji_id}")
    def emoji_asset(emoji_id: str):
        result = assets.get(emoji_id) if assets else None
        if result is None:
            raise missing()
        content, mime = result
        return Response(
            content=content,
            media_type=mime,
            headers={"Cache-Control": "private, max-age=300", "X-Content-Type-Options": "nosniff"},
        )

    def error_response(exc, cid):
        return JSONResponse(
            status_code=exc.status,
            content={
                "error": {
                    "code": exc.code,
                    "message": exc.message,
                    "retryable": exc.retryable,
                    "correlation_id": cid,
                    "next_action": exc.next_action,
                }
            },
        )

    @app.exception_handler(DomainError)
    async def domain_error(request, exc):
        return error_response(exc, request.state.correlation_id)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return error_response(
            DomainError("validation_error", "Проверьте введённые данные.", 422), request.state.correlation_id
        )

    @app.exception_handler(Exception)
    async def unexpected_error(request, exc):
        from sqlalchemy.exc import OperationalError

        status = 503 if isinstance(exc, OperationalError) else 500
        return error_response(
            DomainError("service_unavailable", "Сервис временно недоступен. Повторите позже.", status, True),
            getattr(request.state, "correlation_id", str(uuid4())),
        )

    @app.middleware("http")
    async def protection(request, call_next):
        request.state.correlation_id = str(uuid4())
        scan_subject = "scan:" + digest(request.client.host if request.client else "unknown")
        exempt = request.url.path in {
            "/favicon.ico",
            "/robots.txt",
            "/api/v1/health",
        } or request.url.path.startswith(("/assets/", "/api/v1/webhooks/"))
        if not exempt:
            try:
                guard.check(scan_subject)
            except DomainError as exc:
                return error_response(exc, request.state.correlation_id)
        if request.method in {"POST", "PATCH", "PUT", "DELETE"}:
            origin = request.headers.get("origin")
            if origin and origin not in config.allowed_origins:
                return error_response(
                    DomainError("origin_invalid", "Запрос не подтверждён.", 403), request.state.correlation_id
                )
            length = request.headers.get("content-length", "0")
            body_limit = 65536
            if request.method == "POST" and request.url.path == "/api/v1/admin/broadcasts/media":
                mime = request.headers.get("content-type")
                if mime in {"image/jpeg", "image/png"}:
                    body_limit = 2_000_000
                elif mime == "video/mp4":
                    body_limit = 8_000_000
            if not length.isdigit() or int(length) > body_limit:
                return error_response(
                    DomainError("body_too_large", "Запрос слишком большой.", 413),
                    request.state.correlation_id,
                )
            body = bytearray()
            async for chunk in request.stream():
                if len(body) + len(chunk) > body_limit:
                    return error_response(
                        DomainError("body_too_large", "Запрос слишком большой.", 413),
                        request.state.correlation_id,
                    )
                body.extend(chunk)
            request._body = bytes(body)
        try:
            response = await call_next(request)
        except Exception as exc:  # noqa: BLE001 - безопасная граница ASGI не выпускает частные исключения
            # Catch before ServerErrorMiddleware re-raises into the ASGI logger; never print private exception details.
            from sqlalchemy.exc import OperationalError

            status = 503 if isinstance(exc, OperationalError) else 500
            response = error_response(
                DomainError(
                    "service_unavailable", "Сервис временно недоступен. Повторите позже.", status, True
                ),
                request.state.correlation_id,
            )
        valid_emoji_path = bool(re.fullmatch(r"/api/v1/content/emoji/[0-9]{1,32}", request.url.path))
        if (
            response.status_code == 404
            and not exempt
            and not valid_emoji_path
            and not request.url.path.startswith("/api/v1/admin")
        ):
            try:
                blocked = guard.missing_route(scan_subject, request.url.path[:200])
                if blocked:
                    from sunrise_infrastructure.db import Block

                    with db.transaction() as s:
                        s.add(
                            Block(
                                context=scan_subject,
                                rule="route_scanning",
                                count=60,
                                threshold=60,
                                ends_at=now() + timedelta(hours=1),
                            )
                        )
            except DomainError:
                pass
        if not (response.status_code == 200 and request.url.path.startswith("/api/v1/content/emoji/")):
            response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Correlation-ID"] = request.state.correlation_id
        return response

    def authenticated(request: Request):
        a, session = accounts.session(request.cookies.get("sunrise_session"))
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            if not hmac.compare_digest(request.headers.get("x-csrf-token", ""), session.csrf):
                raise DomainError("csrf_invalid", "Обновите страницу и повторите действие.", 403)
        return a, session

    def admin_access(request: Request):
        try:
            a, session = accounts.session(request.cookies.get("sunrise_session"))
        except DomainError as exc:
            if exc.status == 401:
                raise missing() from None
            raise
        if not accounts.is_admin(a):
            raise missing()
        capability = route_capability(request.url.path, request.method)
        if capability:
            staff.require(a.id, capability)
        if request.method not in {"GET", "HEAD", "OPTIONS"} and not hmac.compare_digest(
            request.headers.get("x-csrf-token", ""), session.csrf
        ):
            raise DomainError("csrf_invalid", "Обновите страницу и повторите действие.", 403)
        return a, session

    def admin_verified(request: Request, pair=Depends(admin_access)):
        a, session = pair
        if not accounts.admin_mfa.trusted(a, request.client.host):
            raise DomainError(
                "mfa_required", "Подтвердите вход кодом дополнительной защиты.", 403, next_action="mfa"
            )
        return pair

    def fresh_admin(pair=Depends(admin_verified)):
        if not pair[1].mfa_at or pair[1].mfa_at < now() - timedelta(minutes=5):
            raise DomainError(
                "mfa_required",
                "Подтвердите опасное изменение свежим кодом аутентификатора.",
                403,
                next_action="mfa",
            )
        return pair

    def confirmation(body):
        if not body.confirmed:
            raise DomainError("confirmation_required", "Подтвердите изменение.", 400)

    def browser_label(request):
        agent = request.headers.get("user-agent", "")[:1000]
        browser = next(
            (
                label
                for marker, label in (
                    ("Edg/", "Edge"),
                    ("Firefox/", "Firefox"),
                    ("Chrome/", "Chrome"),
                    ("Safari/", "Safari"),
                )
                if marker in agent
            ),
            "Браузер",
        )
        platform = next(
            (
                label
                for marker, label in (
                    ("Android", "Android"),
                    ("iPhone", "iOS"),
                    ("iPad", "iPadOS"),
                    ("Windows", "Windows"),
                    ("Macintosh", "macOS"),
                    ("Linux", "Linux"),
                )
                if marker in agent
            ),
            "Неизвестная платформа",
        )
        return browser + " · " + platform

    @app.get("/api/v1/health")
    def health():
        return {
            "status": "ok",
            "environment": "live" if policy.production else "local_test" if config.local_test else "test",
        }

    @app.get("/api/v1/public-config", response_model=ConfigDto)
    def public_config():
        return {
            "instance": config.instance.public(),
            "web_push_public_key": notifications.public_key(),
            "smtp_email_enabled": bool(accounts.mail_transport),
            "turnstile_test_bypass": config.local_test and config.turnstile_test_bypass,
            "telegram_miniapp_enabled": telegram.miniapp_enabled,
            "telegram_login_enabled": telegram.login_enabled,
            **{
                key: getattr(config, key)
                for key in ("dashboard_url", "bot_url", "support_url", "turnstile_site_key", "local_test")
            },
        }

    @app.get("/api/v1/catalog", response_model=CatalogDto)
    def catalog():
        return commerce.catalog()

    @app.get("/api/v1/admin/payment-methods")
    def payment_methods_read(pair=Depends(admin_verified)):
        with db.transaction() as session:
            return commerce._setting(session, "payment_methods", None)

    from .schemas import PaymentMethodsUpdate

    @app.patch("/api/v1/admin/payment-methods")
    def payment_methods_write(body: PaymentMethodsUpdate, pair=Depends(fresh_admin)):
        confirmation(body)
        return admin._save_setting(pair[0].id, "payment_methods", body.policy, body.reason)

    @app.get("/api/v1/legal", response_model=LegalDto)
    def legal_documents():
        return commerce.legal.public()

    @app.get("/api/v1/legal/status", response_model=LegalStatusDto)
    def legal_status(pair=Depends(authenticated)):
        return commerce.legal_status(pair[0].id)

    @app.post("/api/v1/legal/accept", response_model=LegalStatusDto)
    def legal_accept(body: LegalAccept, pair=Depends(authenticated)):
        return commerce.accept_legal(
            pair[0].id, [d.model_dump() for d in body.documents], "website", body.context
        )

    @app.get("/api/v1/admin/legal", response_model=LegalDto)
    def admin_legal(pair=Depends(admin_verified)):
        return commerce.legal.public()

    @app.post("/api/v1/admin/catalog")
    def admin_catalog_create(body: ProductCreate, pair=Depends(fresh_admin)):
        confirmation(body)
        return admin.catalog_create(
            pair[0].id, body.model_dump(exclude={"reason", "confirmed"}, exclude_unset=True), body.reason
        )

    @app.patch("/api/v1/admin/legal", response_model=LegalDto)
    def admin_legal_update(body: LegalUpdate, pair=Depends(fresh_admin)):
        confirmation(body)
        return commerce.legal.configure(
            pair[0].id,
            [d.model_dump() for d in body.documents],
            body.reason,
            partial=body.partial,
            expected_revision=body.expected_revision,
        )

    @app.get("/api/v1/admin/legal/missing", response_model=ItemsDto)
    def admin_legal_missing(
        page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=200), pair=Depends(admin_verified)
    ):
        return commerce.legal.missing(page, page_size)

    @app.get("/api/v1/admin/public-content")
    def admin_public_content(pair=Depends(admin_verified)):
        return admin.public_content()

    @app.patch("/api/v1/admin/public-content")
    def admin_public_content_update(body: PublicContentUpdate, pair=Depends(fresh_admin)):
        confirmation(body)
        return admin.update_public_content(
            pair[0].id, body.model_dump(exclude={"reason", "confirmed"}, exclude_none=True), body.reason
        )

    @app.get("/api/v1/admin/telegram-theme")
    def admin_theme(pair=Depends(admin_verified)):
        return admin.telegram_theme()

    @app.get("/api/v1/admin/panel-mapping", response_model=PanelMappingDto)
    def admin_panel_mapping(pair=Depends(admin_verified)):
        return admin.panel_mapping()

    @app.patch("/api/v1/admin/panel-mapping", response_model=PanelMappingDto)
    def admin_panel_mapping_update(body: PanelMappingUpdate, pair=Depends(fresh_admin)):
        confirmation(body)
        return admin.update_panel_mapping(
            pair[0].id, body.external_squad_uuid, body.project_prefix, body.reason
        )

    @app.patch("/api/v1/admin/telegram-theme")
    def admin_theme_update(body: ThemeUpdate, pair=Depends(fresh_admin)):
        confirmation(body)
        return admin.update_telegram_theme(pair[0].id, body.theme, body.reason)

    @app.get("/api/v1/admin/refund-policy")
    def admin_refund_policy(pair=Depends(admin_verified)):
        return admin.refund_policy()

    @app.patch("/api/v1/admin/refund-policy")
    def admin_refund_policy_update(body: RefundPolicyUpdate, pair=Depends(fresh_admin)):
        confirmation(body)
        return admin.update_refund_policy(pair[0].id, body.max_age_hours, body.max_traffic_bytes, body.reason)

    @app.get("/api/v1/admin/refund-reviews", response_model=ItemsDto)
    def admin_refund_reviews(
        page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=200), pair=Depends(admin_verified)
    ):
        return admin.refund_reviews(page, page_size)

    @app.post("/api/v1/admin/refund-reviews/{review_id}/resolve")
    def admin_refund_resolve(review_id: str, body: RefundResolve, pair=Depends(fresh_admin)):
        confirmation(body)
        return admin.resolve_refund(
            pair[0].id, review_id, body.action, body.reason, body.ends_at, body.expected_subscription_version
        )

    @app.post("/api/v1/auth/email/request")
    def email_request(body: EmailRequest, request: Request):
        if not (config.local_test and config.turnstile_test_bypass and body.turnstile_token == "local-test"):  # noqa: S105 - несекретный маркер изолированного стенда
            if not config.turnstile_secret:
                raise DomainError("captcha_unavailable", "Проверка безопасности временно недоступна.", 503)
            try:
                result = httpx.post(
                    "https://challenges.cloudflare.com/turnstile/v0/siteverify",
                    data={
                        "secret": config.turnstile_secret,
                        "response": body.turnstile_token,
                        "remoteip": request.client.host,
                    },
                    timeout=10,
                ).json()
            except (httpx.HTTPError, ValueError):
                raise DomainError(
                    "captcha_unavailable", "Проверка безопасности временно недоступна.", 503, True
                ) from None
            expected_hostname = config.turnstile_hostname or urlsplit(config.dashboard_url or "").hostname
            if (
                not isinstance(result, dict)
                or result.get("success") is not True
                or not expected_hostname
                or result.get("hostname") != expected_hostname
                or result.get("action") != "login"
            ):
                raise DomainError("captcha_invalid", "Повторите проверку безопасности.", 400)
        return accounts.request_code(str(body.email), request.client.host)

    @app.post("/api/v1/auth/telegram/miniapp", response_model=AuthDto)
    def miniapp_login(body: MiniAppRequest, request: Request, response: Response):
        token, dto = telegram.miniapp(
            body.init_data,
            request.client.host,
            legal_accepted=body.legal_accepted,
            documents=[d.model_dump() for d in body.documents],
            referral_code=getattr(body, "referral_code", None),
            browser_label=browser_label(request),
        )
        response.set_cookie(
            "sunrise_session",
            token,
            max_age=604800,
            httponly=True,
            secure=config.cookie_secure,
            samesite="lax",
            path="/",
        )
        return dto

    @app.post("/api/v1/auth/telegram/start", response_model=TelegramStartDto)
    def telegram_start(body: TelegramConsent, request: Request, response: Response):
        dto, browser = telegram.start(
            [d.model_dump() for d in body.documents],
            body.legal_accepted,
            request.client.host,
            referral_code=getattr(body, "referral_code", None),
        )
        response.set_cookie(
            FLOW_COOKIE,
            browser,
            max_age=600,
            httponly=True,
            secure=config.cookie_secure,
            samesite="lax",
            path="/api/v1/auth/telegram",
        )
        return dto

    @app.get("/api/v1/auth/telegram/callback")
    def telegram_callback(request: Request):
        try:
            if any(len(request.query_params.getlist(field)) != 1 for field in ("code", "state")):
                raise DomainError("telegram_auth_invalid", "Вход через Telegram не подтверждён.", 400)
            token, _dto = telegram.callback(
                request.query_params.get("code"),
                request.query_params.get("state"),
                request.cookies.get(FLOW_COOKIE),
                request.client.host,
                browser_label=browser_label(request),
            )
            response = RedirectResponse("/", status_code=303)
            response.set_cookie(
                "sunrise_session",
                token,
                max_age=604800,
                httponly=True,
                secure=config.cookie_secure,
                samesite="lax",
                path="/",
            )
        except DomainError as exc:
            safe_code = (
                exc.code
                if exc.code
                in {
                    "telegram_auth_invalid",
                    "telegram_auth_unavailable",
                    "telegram_identity_not_allowed",
                    "telegram_auth_rate_limited",
                    "protection_unavailable",
                    "legal_consent_required",
                    "legal_versions_stale",
                    "legal_not_configured",
                    "scoped_test_owner_denied",
                    "auth_required",
                }
                else "other"
            )
            logging.getLogger("sunrise.telegram_login").warning(
                "telegram_login_callback_denied code=%s", safe_code
            )
            response = RedirectResponse("/?telegram_login_error=" + exc.code, status_code=303)
        response.delete_cookie(FLOW_COOKIE, path="/api/v1/auth/telegram")
        return response

    @app.post("/api/v1/auth/email/verify", response_model=AuthDto)
    def email_verify(body: CodeRequest, request: Request, response: Response):
        token, dto = accounts.verify_code(
            body.challenge_id,
            body.code,
            request.client.host,
            legal_accepted=body.legal_accepted,
            documents=[document.model_dump() for document in body.documents],
            referral_code=getattr(body, "referral_code", None),
            browser_label=browser_label(request),
        )
        response.set_cookie(
            "sunrise_session",
            token,
            max_age=604800,
            httponly=True,
            secure=config.cookie_secure,
            samesite="lax",
            path="/",
        )
        return dto

    @app.get("/api/v1/auth/me", response_model=AuthDto)
    def me(request: Request, pair=Depends(authenticated)):
        a, session = pair
        return {
            "account": account_dto(a),
            "csrf_token": session.csrf,
            "is_admin": accounts.is_admin(a),
            "mfa_verified": accounts.admin_mfa.trusted(a, request.client.host),
            **staff.describe(a.id),
        }

    from .v11_routes import install_v11_routes

    install_v11_routes(
        app, authenticated, fresh_admin, notifications, account_features, commerce, config, admin_verified
    )

    @app.post("/api/v1/auth/logout")
    def logout(response: Response, pair=Depends(authenticated)):
        accounts.logout(pair[1].id)
        response.delete_cookie("sunrise_session", path="/")
        return {"status": "ok"}

    @app.get("/api/v1/dashboard", response_model=DashboardDto)
    def dashboard(refresh_panel: bool = False, pair=Depends(authenticated)):
        return commerce.dashboard(pair[0].id, refresh_panel=refresh_panel)

    @app.get("/api/v1/node-news", response_model=NodeNewsDto)
    def node_news(
        before: str | None = None, limit: int = Query(20, ge=1, le=100), pair=Depends(authenticated)
    ):
        return commerce.node_news(pair[0].id, before=before, limit=limit)

    @app.get("/api/v1/admin/panel-event-routes", response_model=PanelEventRoutesResponse)
    def panel_event_routes(pair=Depends(admin_verified)):
        return admin.panel_event_routes(pair[0].id)

    @app.patch("/api/v1/admin/panel-event-routes", response_model=PanelEventRoutes)
    def save_panel_event_routes(body: PanelEventRoutesUpdate, pair=Depends(fresh_admin)):
        confirmation(body)
        return admin.save_panel_event_routes(pair[0].id, body.policy.model_dump(), body.reason)

    @app.get("/api/v1/promos/active", response_model=PendingPromoDto | None)
    def active_promo(pair=Depends(authenticated)):
        return commerce.active_promo(pair[0].id)

    @app.post("/api/v1/promos/activate", response_model=PendingPromoDto)
    def activate_promo(body: PromoActivationRequest, pair=Depends(authenticated)):
        return commerce.activate_promo(pair[0].id, body.code)

    @app.post("/api/v1/promos/claim", response_model=PromoClaimDto)
    def claim_promo(body: PromoClaimRequest, pair=Depends(authenticated)):
        return commerce.claim_promo(pair[0].id, body.code, body.subscription_id)

    @app.get("/api/v1/referral", response_model=ReferralDto)
    def referral_info(pair=Depends(authenticated)):
        return commerce.referral_info(pair[0].id)

    @app.post("/api/v1/quotes", response_model=QuoteDto)
    def quote(body: QuoteRequest, pair=Depends(authenticated)):
        return commerce.quote(pair[0].id, **body.model_dump())

    @app.get("/api/v1/subscriptions/{sid}/renewal-offer", response_model=QuoteDto)
    def renewal_offer(
        sid: str, quote_id: str | None = None, payment_method: str | None = None, pair=Depends(authenticated)
    ):
        return commerce.renewal_offer(pair[0].id, sid, quote_id=quote_id, payment_method=payment_method)

    @app.post("/api/v1/orders", response_model=OrderDto)
    def create_order(body: OrderRequest, pair=Depends(authenticated)):
        return commerce.create_order(pair[0].id, body.quote_id, body.idempotency_key)

    @app.get("/api/v1/orders/{order_id}", response_model=OrderDto)
    def order(order_id: str, pair=Depends(authenticated)):
        return commerce.order(pair[0].id, order_id)

    @app.post("/api/v1/orders/{order_id}/test-pay", response_model=OrderDto)
    def test_pay(order_id: str, pair=Depends(authenticated)):
        return commerce.test_pay(pair[0].id, order_id)

    @app.get("/api/v1/subscriptions/{sid}/connection-instructions", response_model=ConnectionInstructionsDto)
    def connection_instructions(sid: str, pair=Depends(authenticated)):
        return commerce.connection_instructions(pair[0].id, sid)

    @app.post("/api/v1/subscriptions/{sid}/deletion-notice/dismiss")
    def dismiss_deletion_notice(sid: str, body: DeletionNoticeDismissRequest, pair=Depends(authenticated)):
        return commerce.dismiss_deletion_notice(pair[0].id, sid)

    @app.post("/api/v1/subscriptions/{sid}/devices/{device_id}/revoke")
    def revoke_device(sid: str, device_id: str, pair=Depends(authenticated)):
        return commerce.revoke_device(pair[0].id, sid, device_id)

    @app.post("/api/v1/subscriptions/{sid}/recurring")
    def enroll(sid: str, body: RecurringRequest | None = None, pair=Depends(authenticated)):
        if body is None or provider is None:
            return commerce.enroll(pair[0].id, sid)
        return recurring.enroll(pair[0].id, sid, body.plan_id, body.confirmed, body.idempotency_key)

    @app.post("/api/v1/subscriptions/{sid}/recurring/cancel")
    def cancel(sid: str, pair=Depends(authenticated)):
        return recurring.cancel(pair[0].id, sid) if provider else commerce.cancel_agreement(pair[0].id, sid)

    @app.get("/api/v1/subscriptions/{sid}/recurring/status")
    def recurring_status(sid: str, pair=Depends(authenticated)):
        with db.transaction() as s:
            commerce._sub(s, pair[0].id, sid)
        return {"loyalty": recurring.loyalty(sid), "dynamic_amount_supported": False}

    @app.post("/api/v1/account/link/email/request", response_model=LinkEmailChallengeDto)
    def link_email_request(body: LinkEmailRequest, request: Request, pair=Depends(authenticated)):
        return accounts.request_link_email(pair[0].id, pair[1].id, request.client.host)

    @app.post("/api/v1/account/link/email/verify", response_model=LinkEmailVerificationDto)
    def link_email_verify(body: LinkEmailCodeRequest, request: Request, pair=Depends(authenticated)):
        return accounts.verify_link_email(
            pair[0].id, pair[1].id, body.challenge_id, body.code, request.client.host
        )

    @app.post("/api/v1/account/link/request")
    def link_request(body: LinkRequest, request: Request, pair=Depends(authenticated)):
        return accounts.request_link(
            pair[0].id,
            body.telegram_id,
            pair[1].confirmed_at,
            target_telegram_username=body.telegram_username,
            session_id=pair[1].id,
            ip=request.client.host,
        )

    @app.get("/api/v1/account/bonuses")
    def bonuses(pair=Depends(authenticated)):
        from sunrise_infrastructure.db import Ledger

        with db.transaction() as s:
            ledger = list(
                s.scalars(
                    select(Ledger).where(Ledger.account_id == pair[0].id).order_by(Ledger.created_at.desc())
                )
            )
            return {
                "referral_url": None,
                "ledger": [
                    {
                        "id": x.id,
                        "created_at": x.created_at.isoformat(),
                        "reason": x.operation.split(":")[0],
                        "amount": str(x.amount),
                    }
                    for x in ledger
                ],
            }

    @app.get("/api/v1/admin/access")
    def access(pair=Depends(admin_access)):
        return {"allowed": True}

    @app.get("/api/v1/admin/mfa/status", response_model=MfaStatusDto)
    def mfa_status(request: Request, pair=Depends(admin_access)):
        return accounts.admin_mfa.status(pair[0].id, request.client.host)

    @app.post("/api/v1/admin/mfa/enroll", response_model=MfaEnrollmentDto)
    def mfa_enroll(request: Request, pair=Depends(admin_access)):
        return accounts.admin_mfa.begin(pair[0].id, pair[1].id, ip=request.client.host)

    @app.post("/api/v1/admin/mfa/enroll/confirm", response_model=MfaStatusDto)
    def mfa_enroll_confirm(body: MfaRequest, request: Request, pair=Depends(admin_access)):
        return accounts.admin_mfa.verify(
            pair[0].id, pair[1].id, body.code, request.client.host, enrollment=True
        )

    @app.post("/api/v1/admin/mfa/verify", response_model=MfaStatusDto)
    def mfa(body: MfaRequest, request: Request, pair=Depends(admin_access)):
        return accounts.mfa(pair[0], pair[1].id, body.code, request.client.host)

    @app.get("/api/v1/admin/overview")
    def overview(
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        timezone: str = "Europe/Moscow",
        currency: str | None = None,
        source: str | None = None,
        product_id: str | None = None,
        interval: str = "day",
        mode: str = "cash",
        pair=Depends(admin_verified),
    ):
        try:
            return commerce.analytics(
                date_from, date_to, timezone, currency, source, product_id, interval, mode
            )
        except (ValueError, KeyError):
            raise DomainError("range_invalid", "Проверьте период и часовой пояс.", 400) from None

    @app.get("/api/v1/admin/{section}", response_model=ItemsDto)
    def admin_items(
        section: str,
        search: str | None = Query(default=None, max_length=320),
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=25, ge=1, le=200),
        pair=Depends(admin_verified),
    ):
        return admin.list_items(section, search, page, page_size)

    @app.get("/api/v1/admin/users/{account_id}")
    def user_detail(
        account_id: str,
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=25, ge=1, le=50),
        pair=Depends(admin_verified),
    ):
        return admin.user_detail(account_id, pair[0].id, config, page, page_size)

    @app.get("/api/v1/admin/users/{account_id}/invitees", response_model=ItemsDto)
    def user_invitees(
        account_id: str,
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=10, ge=1, le=50),
        pair=Depends(admin_verified),
    ):
        return admin.user_invitees(account_id, page, page_size)

    @app.post("/api/v1/admin/users/{account_id}/device-moderation")
    def device_moderation(account_id: str, body: DeviceModerationRequest, pair=Depends(fresh_admin)):
        confirmation(body)
        from sunrise_infrastructure.moderation import DeviceModeration

        return DeviceModeration(commerce).moderate(
            pair[0].id, account_id, body.action, body.reason, body.idempotency_key, config, body.confirmed
        )

    @app.get("/api/v1/admin/payments/{payment_id}")
    def payment_detail(payment_id: str, pair=Depends(admin_verified)):
        return admin.payment_detail(payment_id)

    @app.get("/api/v1/admin/content/{key}/versions")
    def content_versions(key: str, pair=Depends(admin_verified)):
        return {"items": admin.content_versions(key)}

    @app.post("/api/v1/admin/discounts/preview")
    def discount_preview(body: DiscountPreview, pair=Depends(admin_verified)):
        return admin.discount_preview(body.model_dump())

    @app.post("/api/v1/admin/catalog/{pid}/impact")
    def catalog_impact(pid: str, body: ProductValues, pair=Depends(admin_verified)):
        return admin.catalog_impact(pid, body.model_dump(exclude_unset=True))

    @app.post("/api/v1/admin/broadcasts/estimate")
    def broadcast_estimate(body: BroadcastEstimate, pair=Depends(admin_verified)):
        return broadcasts.preview(
            pair[0].id, body.nodes, media=body.media.model_dump() if body.media else None
        )

    @app.post("/api/v1/admin/broadcasts/media", response_model=MediaReference)
    async def broadcast_media_upload(request: Request, pair=Depends(admin_verified)):
        mime = request.headers.get("content-type")
        if mime not in {"image/jpeg", "image/png", "video/mp4"}:
            raise DomainError("media_invalid", "Загрузите фото JPEG/PNG или видео MP4.", 400)
        cap = 8_000_000 if mime == "video/mp4" else 2_000_000
        length = request.headers.get("content-length")
        if length is not None and (not length.isdecimal() or int(length) > cap):
            raise DomainError("media_too_large", "Файл превышает допустимый размер.", 413)
        chunks, received = [], 0
        async for chunk in request.stream():
            received += len(chunk)
            if received > cap:
                raise DomainError("media_too_large", "Файл превышает допустимый размер.", 413)
            chunks.append(chunk)
        return BroadcastMediaRegistry(commerce, config).register(pair[0].id, chunks, mime)

    @app.get("/api/v1/admin/broadcasts/media/{asset_id}", response_model=MediaReference)
    def broadcast_media_metadata(asset_id: str, pair=Depends(admin_verified)):
        return BroadcastMediaRegistry(commerce, config).metadata(pair[0].id, asset_id)

    @app.post("/api/v1/admin/broadcasts/test-self")
    def broadcast_self(body: BroadcastSelfRequest, pair=Depends(fresh_admin)):
        confirmation(body)
        if body.media:
            return broadcasts.test_self(pair[0].id, body.nodes, body.reason, media=body.media.model_dump())
        return admin.broadcast(pair[0].id, body.nodes, [pair[0].email], config.email_allowlist, body.reason)

    @app.get("/api/v1/admin/broadcasts/{jid}")
    def broadcast_detail(jid: str, pair=Depends(admin_verified)):
        return broadcasts.detail(pair[0].id, jid)

    @app.get("/api/v1/admin/operations/health")
    def operations_health(pair=Depends(admin_verified)):
        return admin.health()

    @app.post("/api/v1/admin/operations/provider/{operation_id}/acknowledge")
    def provider_operation_review(
        operation_id: str, body: ProviderOperationReviewRequest, pair=Depends(fresh_admin)
    ):
        confirmation(body)
        return admin.acknowledge_provider_operation(pair[0].id, operation_id, body.ack_token, body.reason)

    @app.patch("/api/v1/admin/operations/maintenance")
    def maintenance(body: ToggleRequest, pair=Depends(fresh_admin)):
        confirmation(body)
        return admin.maintenance(pair[0].id, body.enabled, body.reason)

    @app.get("/api/v1/admin/analytics/export.csv")
    def analytics_csv(
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        timezone: str = "Europe/Moscow",
        currency: str | None = None,
        source: str | None = None,
        product_id: str | None = None,
        interval: str = "day",
        mode: str = "cash",
        pair=Depends(admin_verified),
    ):
        import csv
        import io

        data = commerce.analytics(date_from, date_to, timezone, currency, source, product_id, interval, mode)
        stream = io.StringIO()
        writer = csv.DictWriter(
            stream,
            fieldnames=[
                "id",
                "payment_id",
                "order_id",
                "date",
                "kind",
                "amount",
                "currency",
                "fee",
                "fee_currency",
                "net_usdt",
                "source",
                "product_id",
                "canonical_account_id",
                "original_account_id",
                "provenance",
                "time_source",
            ],
        )
        writer.writeheader()
        writer.writerows(data["rows"])
        return Response(
            stream.getvalue(),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="payments.csv"'},
        )

    @app.patch("/api/v1/admin/recurring")
    def toggle(body: ToggleRequest, pair=Depends(fresh_admin)):
        confirmation(body)
        return admin.toggle(pair[0].id, body.enabled, body.reason)

    @app.patch("/api/v1/admin/catalog/{product_id}")
    def update_catalog(product_id: str, body: ProductUpdate, pair=Depends(fresh_admin)):
        confirmation(body)
        return admin.catalog_update(
            pair[0].id,
            product_id,
            body.model_dump(exclude={"reason", "confirmed"}, exclude_unset=True),
            body.reason,
        )

    @app.post("/api/v1/admin/security/{block_id}/revoke")
    def unblock(block_id: str, body: Reason, pair=Depends(fresh_admin)):
        confirmation(body)
        return admin.unblock(pair[0].id, block_id, body.reason)

    @app.post("/api/v1/admin/payments/{payment_id}/resolve-dispute")
    def resolve_dispute(payment_id: str, body: Reason, pair=Depends(fresh_admin)):
        confirmation(body)
        return admin.resolve_dispute(pair[0].id, payment_id, body.reason)

    @app.post("/api/v1/admin/orders/{order_id}/retry-provisioning")
    def retry(order_id: str, body: Reason, pair=Depends(fresh_admin)):
        confirmation(body)
        return admin.retry_provisioning(pair[0].id, order_id, body.reason)

    @app.post("/api/v1/admin/users/{account_id}/bonus")
    def bonus_adjust(account_id: str, body: BonusAdjustment, pair=Depends(fresh_admin)):
        confirmation(body)
        return admin.adjust_bonus(pair[0].id, account_id, body.amount, body.operation_id, body.reason)

    @app.post("/api/v1/admin/users/{account_id}/credit")
    def credit_adjust(account_id: str, body: CreditAdjustment, pair=Depends(fresh_admin)):
        confirmation(body)
        return admin.adjust_credit(
            pair[0].id,
            account_id,
            body.amount,
            body.currency,
            body.provenance,
            body.operation_id,
            body.reason,
            body.confirm_existing_currency,
        )

    @app.post("/api/v1/admin/users/{account_id}/revoke-sessions")
    def revoke_sessions(account_id: str, body: Reason, pair=Depends(fresh_admin)):
        confirmation(body)
        return admin.revoke_sessions(pair[0].id, account_id, body.reason)

    @app.post("/api/v1/admin/subscriptions/{sid}/compensate")
    def compensate(sid: str, body: Compensation, pair=Depends(fresh_admin)):
        confirmation(body)
        return admin.compensate(pair[0].id, sid, body.days, body.reason)

    @app.get("/api/v1/admin/security/logins")
    def login_diagnostics(
        search: str | None = None, page: int = 1, page_size: int = 30, pair=Depends(admin_verified)
    ):
        return admin.login_events(search, page, page_size)

    @app.post("/api/v1/admin/subscriptions/{sid}/suspend")
    def suspend(sid: str, body: Reason, pair=Depends(fresh_admin)):
        confirmation(body)
        return admin.subscription_operation(pair[0].id, sid, "suspend", body.reason)

    @app.post("/api/v1/admin/subscriptions/{sid}/resume")
    def resume(sid: str, body: Reason, pair=Depends(fresh_admin)):
        confirmation(body)
        return admin.subscription_operation(pair[0].id, sid, "resume", body.reason)

    @app.post("/api/v1/admin/subscriptions/{sid}/synchronize")
    def synchronize(sid: str, body: Reason, pair=Depends(fresh_admin)):
        confirmation(body)
        return admin.subscription_operation(pair[0].id, sid, "synchronize", body.reason)

    @app.patch("/api/v1/admin/content/{key}")
    def content_update(key: str, body: OrdinaryContentUpdate, pair=Depends(fresh_admin)):
        confirmation(body)
        return admin.save_content(pair[0].id, key, body.nodes, body.reason)

    @app.patch("/api/v1/admin/subscription-retention")
    def retention_update(body: RetentionUpdate, pair=Depends(fresh_admin)):
        confirmation(body)
        return admin.save_retention(pair[0].id, body.enabled, body.days_after_expiry, body.reason)

    @app.patch("/api/v1/admin/discounts")
    def discounts_update(body: DiscountsUpdate, pair=Depends(fresh_admin)):
        confirmation(body)
        return admin.save_discounts(pair[0].id, body.model_dump(exclude={"reason", "confirmed"}), body.reason)

    @app.patch("/api/v1/admin/periods")
    def periods_update(body: PeriodsUpdate, pair=Depends(fresh_admin)):
        confirmation(body)
        return admin.save_periods(pair[0].id, [p.model_dump() for p in body.periods], body.reason)

    @app.post("/api/v1/admin/broadcasts")
    def broadcast(body: BroadcastRequest, pair=Depends(admin_verified)):
        confirmation(body)
        return broadcasts.create(
            pair[0].id,
            body.nodes,
            body.reason,
            draft=False,
            media=body.media.model_dump() if body.media else None,
        )

    @app.post("/api/v1/admin/broadcasts/{jid}/stop")
    def stop_broadcast(jid: str, body: Reason, pair=Depends(admin_verified)):
        confirmation(body)
        return broadcasts.stop(pair[0].id, jid, body.reason)

    @app.patch("/api/v1/admin/emoji-registry")
    def emoji_registry(body: RegistryUpdate, pair=Depends(fresh_admin)):
        confirmation(body)
        return admin.save_registry(pair[0].id, body.registry, body.reason)

    @app.post("/api/v1/admin/recurring/{agreement_id}/cancel")
    def admin_cancel(agreement_id: str, body: Reason, pair=Depends(fresh_admin)):
        confirmation(body)
        return admin.cancel_recurring(pair[0].id, agreement_id, body.reason)

    @app.post("/api/v1/admin/operations/jobs/{jid}/retry")
    def job_retry(jid: str, body: Reason, pair=Depends(fresh_admin)):
        confirmation(body)
        return admin.retry_job(pair[0].id, jid, body.reason)

    @app.post("/api/v1/webhooks/rollypay")
    async def rollypay(request: Request):
        raw = await request.body()
        if len(raw) > 65536:
            raise DomainError("body_too_large", "Запрос слишком большой.", 413)
        if not config.rollypay_signing_secret:
            raise DomainError("provider_unavailable", "Интеграция не настроена.", 503)
        verify_callback(
            raw,
            request.headers.get("x-timestamp", ""),
            request.headers.get("x-signature", ""),
            config.rollypay_signing_secret,
            int(time.time()),
        )
        try:

            def unique_fields(pairs):
                result = {}
                for name, value in pairs:
                    if name in result:
                        raise ValueError("duplicate_callback_field")
                    result[name] = value
                return result

            raw_text = raw.decode("utf-8")
            event = json.loads(raw_text, object_pairs_hook=unique_fields)
        except (ValueError, UnicodeError):
            raise DomainError("event_invalid", "Некорректное уведомление.", 400) from None
        if not isinstance(event, dict):
            raise DomainError("event_invalid", "Некорректное уведомление.", 400)
        if not (policy.production or policy.scoped_payments) and event.get("test") is not True:
            raise DomainError("environment_mismatch", "Разрешены только тестовые уведомления.", 403)
        if (policy.production or policy.scoped_payments) and (
            ("test" in event and event["test"] is not False)
            or "environment" in event
            and event["environment"] != "prod"
        ):
            raise DomainError("environment_mismatch", "Окружение платежа не подтверждено.", 403)
        # A signed event is durably accepted, then independently reconciled through provider API.
        key = digest(raw_text)
        with db.transaction() as s:
            policy.require_current(s)
            if getattr(policy, "scoped_payments", False):
                from sunrise_infrastructure.db import Order, ProviderOperation

                operation = s.scalar(
                    select(ProviderOperation)
                    .where(ProviderOperation.provider_id == event.get("payment_id"))
                    .with_for_update()
                )
                order = (
                    s.get(Order, event.get("order_id")) if isinstance(event.get("order_id"), str) else None
                )
                if operation is None and order is not None:
                    operation = s.scalar(
                        select(ProviderOperation)
                        .where(
                            ProviderOperation.order_id == order.id,
                            ProviderOperation.provider_id.is_(None),
                            ProviderOperation.state.in_(["in_flight", "outcome_unknown"]),
                        )
                        .with_for_update()
                    )
                policy.record_callback_candidate_in(s, order, operation, event.get("payment_id"), key)
            from sqlalchemy.dialects.postgresql import insert

            safe = {
                k: event.get(k)
                for k in (
                    "payment_id",
                    "order_id",
                    "status",
                    "amount",
                    "currency",
                    "test",
                    "environment",
                    "payment_method",
                    "terminal_id",
                )
                if k in event
            }
            if policy.production or policy.scoped_payments:
                safe["receipt"] = {
                    "provider": "rollypay",
                    "terminal": policy.scope.rollypay.terminal,
                    "scope_id": policy.scope.scope_id,
                    "signed_body": raw_text,
                    "body_sha256": key,
                }
            s.execute(
                insert(Inbox)
                .values(id=str(uuid4()), event_key=key, payload=safe)
                .on_conflict_do_nothing(index_elements=["event_key"])
            )
        return {"status": "accepted"}

    return app
