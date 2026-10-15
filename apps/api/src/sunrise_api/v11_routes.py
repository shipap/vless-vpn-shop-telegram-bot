"""Маршруты личного журнала и подтверждённых изменений аккаунта."""

import asyncio
import json
from datetime import timedelta
from typing import Annotated, Literal

from fastapi import Depends, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sunrise_core.errors import DomainError
from sunrise_infrastructure.db import AccountIdentityOperation, PanelObservation, Setting, now

from .schemas import AUTOMATIC_AUDIT_REASON, PromoInput


class EmailChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field(max_length=320)


class EmailChangeCode(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation_id: str = Field(max_length=64)
    challenge_id: str = Field(max_length=64)
    code: str = Field(pattern=r"^\d{6}$")


class IdentityReason(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(default=AUTOMATIC_AUDIT_REASON, min_length=5, max_length=500)


class IdentityResolution(IdentityReason):
    confirmed: bool
    idempotency_key: str | None = Field(default=None, max_length=128)


class PushKeys(BaseModel):
    p256dh: str = Field(max_length=200)
    auth: str = Field(max_length=100)


class BrowserPush(BaseModel):
    model_config = ConfigDict(extra="forbid")
    endpoint: str = Field(max_length=2048)
    keys: PushKeys


class BrowserPreference(BaseModel):
    model_config = ConfigDict(extra="forbid")
    browser_enabled: bool


class ConfirmedOperation(IdentityReason):
    confirmed: bool
    idempotency_key: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,128}$")


class DeviceBatch(ConfirmedOperation):
    reason: str = "Подтверждена отвязка устройств"
    device_ids: list[str] = Field(min_length=1, max_length=100)
    operation_mode: Literal["single", "bulk"] | None = None
    expected_generations: dict[str, Annotated[int, Field(strict=True, ge=1)]] | None = Field(
        default=None, min_length=1, max_length=100
    )


class CredentialRotation(ConfirmedOperation):
    reason: str = "Подтверждено обновление ссылки"
    revoke_only_passwords: bool = False


class OwnerGrant(ConfirmedOperation):
    product_id: str = Field(max_length=64)
    subscription_id: str | None = Field(default=None, max_length=64)
    days: int = Field(ge=1, le=3660)
    devices: int | None = Field(default=None, ge=1, le=100)


class DirectMessage(ConfirmedOperation):
    nodes: list[dict] = Field(min_length=1, max_length=100)


class CampaignCreate(IdentityReason):
    confirmed: bool
    name: str = Field(min_length=1, max_length=200)
    source: str = Field(min_length=1, max_length=200)
    medium: str = Field(min_length=1, max_length=200)
    campaign: str = Field(min_length=1, max_length=200)


class CampaignVisitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    campaign_id: str = Field(pattern=r"^[A-Za-z0-9_-]{24}$")
    visitor_id: str = Field(pattern=r"^[A-Za-z0-9_.:-]{16,128}$")


class CampaignAttach(BaseModel):
    model_config = ConfigDict(extra="forbid")
    visit_tokens: list[str] = Field(min_length=1, max_length=2)


class NodeVisibility(IdentityReason):
    confirmed: bool
    name: str = Field(min_length=1, max_length=100)
    visible: bool


class PromoSave(PromoInput):
    reason: str = Field(default=AUTOMATIC_AUDIT_REASON, min_length=5, max_length=500)
    confirmed: bool


def install_v11_routes(
    app, authenticated, fresh_admin, notifications, features, commerce, config, admin_verified=None
):
    from sunrise_infrastructure.admin_operations import AdminOperations
    from sunrise_infrastructure.campaigns import Campaigns
    from sunrise_infrastructure.customer_operations import CustomerOperations
    from sunrise_infrastructure.panel_events import PanelEvents

    operations = AdminOperations(commerce, config)
    campaigns = Campaigns(commerce, config)
    customer = CustomerOperations(
        commerce, PanelEvents(commerce.db, commerce.panel_provider, config, notifications=notifications)
    )

    def current_node_labels(session):
        from sunrise_infrastructure.panel_event_routes import policy_in

        policy = policy_in(session, commerce.panel_provider, config)
        return {node.node_uuid: node.safe_label for node in policy.nodes} if policy else {}

    @app.get("/api/v1/notifications")
    def inbox(
        limit: Annotated[int, Query(ge=1, le=50)] = 5,
        offset: Annotated[int, Query(ge=0, le=100000)] = 0,
        pair=Depends(authenticated),
    ):
        return notifications.list(pair[0].id, limit=limit, offset=offset)

    @app.post("/api/v1/notifications/read-all")
    def read_all(pair=Depends(authenticated)):
        return notifications.mark_all_read(pair[0].id)

    @app.post("/api/v1/notifications/{identifier}/read")
    def read(identifier: str, pair=Depends(authenticated)):
        return notifications.mark_read(pair[0].id, identifier)

    @app.get("/api/v1/notifications/preferences")
    def preferences(pair=Depends(authenticated)):
        return notifications.preferences(pair[0].id)

    @app.patch("/api/v1/notifications/preferences")
    def update_preferences(body: BrowserPreference, pair=Depends(authenticated)):
        return notifications.set_browser_preference(pair[0].id, body.browser_enabled)

    @app.post("/api/v1/notifications/push")
    def push(body: BrowserPush, pair=Depends(authenticated)):
        return notifications.register_push(pair[0].id, body.model_dump())

    @app.delete("/api/v1/notifications/push/{identifier}")
    def delete_push(identifier: str, pair=Depends(authenticated)):
        return notifications.remove_push(pair[0].id, identifier)

    @app.get("/api/v1/notifications/stream")
    async def stream(request: Request, pair=Depends(authenticated)):
        account_id, session_id = pair[0].id, pair[1].id

        async def events():
            seen = set()
            for _ in range(30):
                if await request.is_disconnected():
                    return
                # Recheck revocation; never keep an authenticated stream alive after logout.
                try:
                    current = await asyncio.to_thread(
                        features.accounts.session, request.cookies.get("sunrise_session")
                    )
                except DomainError:
                    return
                if current[0].id != account_id or current[1].id != session_id:
                    return
                result = await asyncio.to_thread(notifications.list, account_id)
                identifiers = {row["id"] for row in result["items"]}
                for identifier in identifiers - seen:
                    yield "event: notification\ndata: " + json.dumps({"id": identifier}) + "\n\n"
                seen |= identifiers
                yield ": heartbeat\n\n"
                await asyncio.sleep(2)

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/v1/account/sessions")
    def sessions(pair=Depends(authenticated)):
        return features.list_sessions(pair[0].id, pair[1].id)

    @app.post("/api/v1/account/sessions/{identifier}/revoke")
    def revoke(identifier: str, pair=Depends(authenticated)):
        return features.revoke_session(pair[0].id, pair[1].id, identifier)

    @app.post("/api/v1/account/sessions/revoke-others")
    def revoke_others(pair=Depends(authenticated)):
        return features.revoke_other_sessions(pair[0].id, pair[1].id)

    @app.get("/api/v1/account/identity/status")
    def identity(pair=Depends(authenticated)):
        return features.identity_status(pair[0].id, pair[1].id)

    @app.post("/api/v1/account/telegram/unlink")
    def unlink(request: Request, pair=Depends(authenticated)):
        return features.request_unlink(pair[0].id, pair[1].id, request.client.host)

    @app.post("/api/v1/account/telegram/unlink-request")
    def unlink_request(body: IdentityReason, request: Request, pair=Depends(authenticated)):
        return features.request_owner_unlink(pair[0].id, pair[1].id, request.client.host, body.reason)

    @app.post("/api/v1/account/email/request")
    def email_request(body: EmailChange, request: Request, pair=Depends(authenticated)):
        return features.request_email_change(pair[0].id, pair[1].id, body.email, request.client.host)

    @app.post("/api/v1/account/email/verify")
    def email_verify(body: EmailChangeCode, request: Request, pair=Depends(authenticated)):
        return features.verify_email_change(
            pair[0].id, pair[1].id, body.operation_id, body.challenge_id, body.code, request.client.host
        )

    @app.get("/api/v1/subscriptions/{sid}/inventory")
    def inventory(sid: str, refresh: bool = False, pair=Depends(authenticated)):
        return customer.inventory(pair[0].id, sid, refresh=refresh)

    @app.post("/api/v1/subscriptions/{sid}/inventory/refresh")
    def refresh_inventory(sid: str, pair=Depends(authenticated)):
        return customer.inventory(pair[0].id, sid, refresh=True)

    @app.post("/api/v1/subscriptions/{sid}/devices/revoke-bulk")
    def bulk(sid: str, body: DeviceBatch, pair=Depends(authenticated)):
        return customer.revoke_devices(
            pair[0].id,
            sid,
            body.device_ids,
            body.idempotency_key,
            confirmed=body.confirmed,
            expected_generations=body.expected_generations,
            operation_mode=body.operation_mode,
        )

    @app.post("/api/v1/subscriptions/{sid}/credentials/rotate")
    def rotate(sid: str, body: CredentialRotation, pair=Depends(authenticated)):
        return customer.rotate_credentials(
            pair[0].id,
            sid,
            body.idempotency_key,
            confirmed=body.confirmed,
            revoke_only_passwords=body.revoke_only_passwords,
        )

    @app.post("/api/v1/admin/users/{account_id}/message")
    def message(account_id: str, body: DirectMessage, pair=Depends(admin_verified or fresh_admin)):
        return operations.message(
            pair[0].id, account_id, body.nodes, body.idempotency_key, body.reason, confirmed=body.confirmed
        )

    @app.post("/api/v1/admin/users/{account_id}/grant")
    def grant(account_id: str, body: OwnerGrant, pair=Depends(fresh_admin)):
        return operations.grant(
            pair[0].id,
            account_id,
            body.product_id,
            body.days,
            body.idempotency_key,
            body.reason,
            confirmed=body.confirmed,
            subscription_id=body.subscription_id,
            devices=body.devices,
        )

    @app.get("/api/v1/admin/backups")
    def backups(pair=Depends(admin_verified or fresh_admin)):
        return operations.backups(pair[0].id)

    @app.post("/api/v1/admin/backups")
    def backup(body: ConfirmedOperation, pair=Depends(fresh_admin)):
        return operations.request_backup(
            pair[0].id, body.idempotency_key, body.reason, confirmed=body.confirmed
        )

    @app.get("/api/v1/admin/campaigns")
    def campaign_list(pair=Depends(admin_verified or fresh_admin)):
        return campaigns.list(pair[0].id)

    @app.get("/api/v1/admin/referrals")
    def referral_top(metric: str = "rewards", pair=Depends(admin_verified or fresh_admin)):
        from sunrise_infrastructure.admin import Admin
        from sunrise_infrastructure.bot_admin import BotAdmin

        return BotAdmin(Admin(commerce, features.accounts.guard, features.accounts), config).bot_referrals(
            pair[0].id, metric=metric, page=1, limit=20
        )

    @app.get("/api/v1/admin/promos")
    def promos(pair=Depends(admin_verified or fresh_admin)):
        from sunrise_infrastructure.admin import Admin

        return {"items": Admin(commerce, features.accounts.guard, features.accounts).bot_promos(pair[0].id)}

    @app.post("/api/v1/admin/promos")
    def save_promo(body: PromoSave, pair=Depends(admin_verified or fresh_admin)):
        if not body.confirmed:
            raise DomainError("confirmation_required", "Подтвердите промокод.", 400)
        from sunrise_infrastructure.admin import Admin

        return Admin(commerce, features.accounts.guard, features.accounts).save_bot_promo(
            pair[0].id, body.model_dump(exclude={"reason", "confirmed"}), body.reason
        )

    @app.delete("/api/v1/admin/promos/{code}")
    def delete_promo(code: str, body: IdentityResolution, pair=Depends(fresh_admin)):
        if not body.confirmed:
            raise DomainError("confirmation_required", "Подтвердите удаление промокода.", 400)
        from sunrise_infrastructure.admin import Admin

        return Admin(commerce, features.accounts.guard, features.accounts).delete_bot_promo(
            pair[0].id, code, body.reason
        )

    @app.post("/api/v1/admin/campaigns")
    def campaign_create(body: CampaignCreate, pair=Depends(admin_verified or fresh_admin)):
        if not body.confirmed:
            from sunrise_core.errors import DomainError

            raise DomainError("confirmation_required", "Подтвердите создание ссылки.", 400)
        return campaigns.create(pair[0].id, body.model_dump(), body.reason)

    @app.get("/api/v1/admin/campaigns/{identifier}/stats")
    def campaign_stats(identifier: str, pair=Depends(admin_verified or fresh_admin)):
        return campaigns.stats(pair[0].id, identifier)

    @app.post("/api/v1/campaigns/visit")
    def campaign_visit(body: CampaignVisitRequest, request: Request):
        from sunrise_infrastructure.security import digest

        key = "campaign-visit:" + digest(request.client.host)
        guard = features.accounts.guard
        guard.check(key)
        if guard.fail(key, threshold=120, window=60):
            from sunrise_core.errors import DomainError

            raise DomainError("campaign_rate_limited", "Слишком много переходов.", 429)
        return campaigns.visit(body.campaign_id, body.visitor_id)

    @app.post("/api/v1/campaigns/attach")
    def campaign_attach(body: CampaignAttach, pair=Depends(authenticated)):
        return campaigns.attach(pair[0].id, body.visit_tokens)

    @app.get("/api/v1/nodes")
    def node_status(pair=Depends(authenticated)):
        with commerce.db.transaction() as session:
            labels = current_node_labels(session)
            visibility = session.get(Setting, "node_visibility")
            values = visibility.value if visibility else {}
            rows = session.scalars(
                select(PanelObservation).where(
                    PanelObservation.kind == "node",
                    PanelObservation.resource_id.in_(config.panel_test_node_uuids),
                )
            )
            rows = [
                row
                for row in rows
                if row.resource_id in labels and values.get(row.resource_id, {}).get("visible")
            ]
            items = [
                {
                    "id": row.resource_id,
                    "name": values[row.resource_id].get("name") or labels[row.resource_id],
                    "status": row.status,
                    "observed_at": row.observed_at.isoformat(),
                }
                for row in rows
            ]
            cutoff = now() - timedelta(minutes=10)
            fresh = all(not row.last_error and cutoff <= row.observed_at for row in rows)
            return {
                "items": items,
                "source": "panel",
                "state": "fresh" if items and fresh else "stale" if items else "unknown",
            }

    @app.get("/api/v1/admin/nodes")
    def admin_nodes(pair=Depends(admin_verified or fresh_admin)):
        with commerce.db.transaction() as session:
            labels = current_node_labels(session)
            visibility = session.get(Setting, "node_visibility")
            values = visibility.value if visibility else {}
            observations = {
                row.resource_id: row
                for row in session.scalars(
                    select(PanelObservation).where(
                        PanelObservation.kind == "node",
                        PanelObservation.resource_id.in_(labels),
                    )
                )
            }
            return {
                "items": [
                    {
                        "id": identifier,
                        "name": values.get(identifier, {}).get("name")
                        or labels.get(identifier, "Название не определено"),
                        "visible": values.get(identifier, {}).get("visible", False),
                        "status": observations[identifier].status if identifier in observations else None,
                        "observed_at": observations[identifier].observed_at.isoformat()
                        if identifier in observations
                        else None,
                        "error": observations[identifier].last_error if identifier in observations else None,
                    }
                    for identifier in config.panel_test_node_uuids
                ]
            }

    @app.patch("/api/v1/admin/nodes/{identifier}")
    def configure_node(identifier: str, body: NodeVisibility, pair=Depends(fresh_admin)):
        from sunrise_core.errors import DomainError

        if not body.confirmed or identifier not in config.panel_test_node_uuids:
            raise DomainError("node_denied", "Узел не разрешён для тестового стенда.", 403)
        with commerce.db.transaction() as session:
            from sunrise_infrastructure.db import Audit
            from sunrise_infrastructure.staff_permissions import StaffPermissions

            StaffPermissions(features.accounts).require_in(session, pair[0].id, "operations_manage")
            row = session.scalar(select(Setting).where(Setting.key == "node_visibility").with_for_update())
            value = {**(row.value if row else {}), identifier: {"name": body.name, "visible": body.visible}}
            if row:
                row.value = value
            else:
                session.add(Setting(key="node_visibility", value=value))
            session.add(
                Audit(
                    actor_id=pair[0].id,
                    action="node_visibility",
                    target=identifier,
                    reason=body.reason,
                    details={"name": body.name, "visible": body.visible},
                )
            )
            return value[identifier]

    @app.get("/api/v1/admin/identity-requests")
    def requests(pair=Depends(admin_verified or fresh_admin)):
        with commerce.db.transaction() as session:
            rows = session.scalars(
                select(AccountIdentityOperation)
                .where(
                    AccountIdentityOperation.kind == "owner_unlink_request",
                    AccountIdentityOperation.state == "owner_review",
                    AccountIdentityOperation.expires_at > now(),
                )
                .order_by(AccountIdentityOperation.created_at)
                .limit(100)
            )
            return {
                "items": [
                    {
                        "id": row.id,
                        "account_id": str(row.account_id),
                        "reason": row.payload.get("reason"),
                        "created_at": row.created_at.isoformat(),
                        "expires_at": row.expires_at.isoformat(),
                    }
                    for row in rows
                ]
            }

    @app.post("/api/v1/admin/identity-requests/{identifier}/resolve")
    def resolve(identifier: str, body: IdentityResolution, pair=Depends(fresh_admin)):
        if not body.confirmed:
            from sunrise_core.errors import DomainError

            raise DomainError("confirmation_required", "Подтвердите удаление привязки.", 400)
        return features.approve_owner_unlink(pair[0].id, identifier, body.reason)

    @app.post("/api/v1/webhooks/remnawave")
    @app.post("/remnawave")
    async def panel_webhook(request: Request):
        from sunrise_infrastructure.panel_events import PanelEvents

        raw = bytearray()
        async for chunk in request.stream():
            if len(raw) + len(chunk) > config.panel_webhook_max_bytes:
                from sunrise_core.errors import DomainError

                raise DomainError("webhook_too_large", "Событие слишком большое.", 413)
            raw.extend(chunk)
        return PanelEvents(commerce.db, commerce.panel_provider, config, notifications=notifications).accept(
            bytes(raw), request.headers
        )
