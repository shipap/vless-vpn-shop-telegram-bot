"""Подписанный durable inbox и наблюдения только собственных тестовых объектов."""

import hashlib
import hmac
import json
import re
from copy import deepcopy
from datetime import timedelta

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sunrise_core.errors import DomainError

from . import panel_entitlements
from .db import Account, Device, Job, PanelEvent, PanelObservation, Product, Setting, Subscription, now, uid
from .panel import PanelMissing, PanelReview, PanelUnavailable, registered_uuid_owner
from .panel_news import CRM_EVENT
from .panel_v3 import instant, numeric_id, registered_owner

USER_EVENTS = {
    "user." + name
    for name in (
        "created",
        "modified",
        "deleted",
        "revoked",
        "enabled",
        "disabled",
        "limited",
        "expired",
        "traffic_reset",
        "first_connected",
        "bandwidth_usage_threshold_reached",
        "expiration",
        "not_connected",
    )
} | {"user_hwid_devices.added", "user_hwid_devices.deleted"}
NODE_EVENTS = {
    "node." + name
    for name in (
        "created",
        "modified",
        "deleted",
        "enabled",
        "disabled",
        "connection_lost",
        "connection_restored",
        "traffic_notify",
    )
}


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def new_device_notice_content(product, subscription_name, device_name):
    """Snapshot the current catalog tariff title; personal subscription names stay stored."""
    from sunrise_core.content import validate_content
    from sunrise_core.telegram_content import render_classic, render_plain, telegram_html_to_content

    from .catalog import validate_presentation

    name = getattr(product, "name", None) or subscription_name or "Подписка"
    fallback = telegram_html_to_content(name[:200]) if product else [{"type": "text", "text": name[:200]}]
    content = getattr(product, "content", None)
    candidate = content.get("name") if isinstance(content, dict) else None
    try:
        nodes = deepcopy(validate_content(candidate)) if candidate else fallback
        if not render_plain(nodes):
            nodes = fallback
    except (ValueError, TypeError, AttributeError):
        nodes = fallback
    presentation = getattr(product, "presentation", None)
    icon = presentation.get("icon") if isinstance(presentation, dict) else None

    def has_emoji(items):
        return any(node["type"] == "emoji" or has_emoji(node.get("children", [])) for node in items)

    if icon and not has_emoji(nodes):
        try:
            validate_presentation({"icon": icon})
            emoji = {"type": "emoji", "fallback": icon["fallback"], "label": "Тариф"}
            if icon.get("custom_emoji_id"):
                emoji["id"] = icon["custom_emoji_id"]
            nodes = [emoji, {"type": "text", "text": " "}, *nodes]
        except DomainError:
            pass
    prefix = {"type": "text", "text": "К подписке «"}
    suffix = {
        "type": "text",
        "text": f"» подключено устройство «{device_name}». Если это не вы, проверьте устройства и обновите ссылку.",
    }
    result = [prefix, {"type": "bold", "children": nodes}, suffix]
    try:
        validate_content(result)
        if len(render_classic(result, custom_emoji=True)) > 3500:
            raise ValueError("notice_title_too_large")
    except ValueError:
        result = [prefix, {"type": "bold", "children": fallback}, suffix]
    return render_plain(result), result


class PanelEvents:
    def __init__(self, db, panel, settings, notifications=None):
        self.db, self.panel, self.settings = db, panel, settings
        if notifications is None:
            from .notifications import NotificationService

            notifications = NotificationService(db, settings)
        self.notifications = notifications

    def _owned(self, session, sub):
        policy = getattr(self.panel, "runtime_policy", None)
        if policy and policy.production:
            from .panel_v3 import production_binding_key, registered_project_owner

            policy.require_current(session)
            record = session.get(Setting, production_binding_key(sub.id)) if sub else None
            value = record.value if record and isinstance(record.value, dict) else {}
            account = session.get(Account, sub.account_id) if sub else None
            if (
                not sub
                or not registered_project_owner(session, value, sub, account, self.panel)
                or sub.panel_user_id is None
            ):
                raise ValueError("panel_identity_denied")
            binding = value["binding"]
            self.panel.register_project_binding(
                binding,
                connection_url=value.get("connection_url"),
                capacity_evidence=value.get("capacity_evidence")
                if value.get("origin") == "imported_project_identity"
                else None,
            )
            return dict(binding), dict(value)
        if self.panel and getattr(self.panel, "contract", None) == "2.8_uuid" and sub:
            record = session.get(Setting, "panel_test:" + sub.id)
            account = session.get(Account, sub.account_id)
            value = record.value if record else {}
            binding = value.get("binding")
            if (
                sub.environment not in {"local_test", "sandbox"}
                or not registered_uuid_owner(session, value, sub, account, self.panel)
                or not isinstance(binding, dict)
                or sub.panel_uuid is None
                or sub.panel_user_id is not None
                or binding.get("uuid") != sub.panel_uuid
                or binding.get("short_uuid") != sub.short_uuid
                or value.get("connection_url") != sub.connection_url
            ):
                raise ValueError("panel_identity_denied")
            self.panel.register_test_binding(
                binding, telegram_id=value["telegram_id"], connection_url=sub.connection_url
            )
            return dict(binding), dict(value)
        if not self.panel or getattr(self.panel, "contract", None) != "3.4_id" or not sub:
            raise ValueError("panel_identity_denied")
        record = session.get(Setting, "panel_test:" + sub.id)
        account = session.get(Account, sub.account_id)
        value = record.value if record else {}
        binding = value.get("binding")
        allowed = getattr(self.settings, "panel_test_user_ids", ())
        scoped = getattr(self.panel, "scoped_runtime_policy", None)
        if scoped:
            scoped.require_current(session)
            scoped.require_subscription_in(session, sub)
        if (
            sub.environment not in ({"scoped_live"} if scoped else {"local_test", "sandbox"})
            or not account
            or not registered_owner(session, value, sub, account, self.panel)
            or value.get("origin") != "new_test_identity"
            or value.get("contract") != "3.4_id"
            or value.get("subscription_id") != sub.id
            or value.get("environment") != sub.environment
            or value.get("panel_host") != self.panel.api_host
            or not isinstance(binding, dict)
            or sub.panel_user_id is None
            or binding.get("user_id") != str(sub.panel_user_id)
            or sub.short_uuid != binding.get("short_uuid")
            or sub.connection_url != value.get("connection_url")
            or (allowed and str(sub.panel_user_id) not in allowed)
        ):
            raise ValueError("panel_identity_denied")
        self.panel.register_test_binding(
            binding,
            telegram_id=value["telegram_id"],
            connection_url=sub.connection_url,
            **({"account_id": value["account_id"]} if scoped else {}),
        )
        return dict(binding), dict(value)

    def accept(self, raw, headers):
        def reject(code="panel_event_invalid", status=400):
            raise DomainError(code, "Событие панели не принято.", status)

        policy = getattr(self.panel, "runtime_policy", None)
        scoped = getattr(self.panel, "scoped_runtime_policy", None)
        if policy and policy.production:
            policy.require()
        elif not getattr(self.settings, "panel_webhook_test_authorized", False):
            reject("panel_webhook_disabled", 404)
        secret = getattr(self.settings, "panel_webhook_secret", "")
        if (
            not secret
            or not isinstance(raw, bytes)
            or not 1 <= len(raw) <= self.settings.panel_webhook_max_bytes
        ):
            reject()
        lowered = {key.lower(): value for key, value in headers.items()}
        signature = lowered.get("x-remnawave-signature", "")
        if not isinstance(signature, str) or not re.fullmatch(r"[a-f0-9]{64}", signature):
            reject("panel_signature_invalid", 401)
        expected = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            reject("panel_signature_invalid", 401)
        try:
            payload = json.loads(raw, object_pairs_hook=unique_object)
            if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
                reject()
            stamp = payload.get("timestamp")
            if not isinstance(stamp, str) or stamp != lowered.get("x-remnawave-timestamp"):
                reject()
            when = instant(stamp)
            if abs((now() - when).total_seconds()) > self.settings.panel_webhook_max_age_seconds:
                reject("panel_event_stale")
            kind, scope, data = payload.get("event"), payload.get("scope"), payload["data"]
            if not isinstance(kind, str) or scope != kind.split(".")[0]:
                reject()
            if kind not in USER_EVENTS | NODE_EVENTS | {CRM_EVENT}:
                return {"status": "ignored"}
            if scoped and kind == CRM_EVENT:
                return {"status": "ignored"}
            clean = {"timestamp": stamp}
            if kind == CRM_EVENT:
                if (
                    set(data) != {"providerName", "nodeName", "nextBillingAt", "loginUrl"}
                    or not all(isinstance(data[field], str) for field in data)
                    or not 1 <= len(data["providerName"]) <= 200
                    or not 1 <= len(data["nodeName"]) <= 200
                    or len(data["loginUrl"]) > 4096
                ):
                    reject()
                clean.update(
                    provider_name=data["providerName"],
                    node_name=data["nodeName"],
                    next_billing_at=instant(data["nextBillingAt"]).isoformat(),
                )
            elif kind in USER_EVENTS:
                user = data.get("user") if kind.startswith("user_hwid_devices.") else data
                if not isinstance(user, dict):
                    reject()
                user_id = numeric_id(user.get("id"))
                clean["user_id"] = user_id
                if kind.startswith("user_hwid_devices."):
                    device = data.get("hwidUserDevice")
                    if not isinstance(device, dict) or numeric_id(device.get("userId")) != user_id:
                        reject()
                    hwid = device.get("hwid")
                    if (
                        not isinstance(hwid, str)
                        or not 1 <= len(hwid) <= 512
                        or any(ord(c) <= 32 for c in hwid)
                    ):
                        reject()
                    clean["hwid"] = hwid
                    if kind == "user_hwid_devices.added":
                        clean["device_created_at"] = instant(device.get("createdAt")).isoformat()
            else:
                node_id = data.get("uuid")
                if (
                    not self.panel
                    or node_id not in self.panel.nodes
                    or not (policy and policy.production)
                    and node_id not in set(getattr(self.settings, "panel_test_node_uuids", ()))
                ):
                    return {"status": "ignored"}
                clean["node_uuid"] = node_id
        except (ValueError, TypeError, UnicodeError, RecursionError, PanelReview):
            reject()
        key = "remnawave:" + hashlib.sha256(raw).hexdigest()
        with self.db.transaction() as session:
            if scoped:
                scoped.require_current(session)
            sid = None
            if kind in NODE_EVENTS and policy and policy.production:
                policy.require_current(session)
                if node_id not in set(policy.scope.panel.node_uuids) & self.panel.nodes:
                    return {"status": "ignored"}
            if kind in NODE_EVENTS and scoped and node_id not in set(scoped.scope.panel.node_uuids):
                return {"status": "ignored"}
            if kind == CRM_EVENT:
                from .panel_event_routes import policy_in

                policy = policy_in(session, self.panel, self.settings)
                if policy is None or not any(
                    entry.provider_name == clean["provider_name"] and entry.node_name == clean["node_name"]
                    for entry in policy.infra
                ):
                    return {"status": "ignored"}
            if kind in USER_EVENTS:
                sub = session.scalar(select(Subscription).where(Subscription.panel_user_id == int(user_id)))
                try:
                    self._owned(session, sub)
                except ValueError:
                    return {"status": "ignored"}
                sid = sub.id
            added = session.scalar(
                insert(PanelEvent)
                .values(
                    id=uid(),
                    event_key=key,
                    kind=kind,
                    panel_user_id=int(user_id) if kind in USER_EVENTS else None,
                    subscription_id=sid,
                    payload=clean,
                    state="pending",
                    received_at=now(),
                    available_at=now(),
                    attempts=0,
                )
                .on_conflict_do_nothing(index_elements=[PanelEvent.event_key])
                .returning(PanelEvent.id)
            )
        return {"status": "accepted" if added else "duplicate"}

    def _with_lock(self, sid, operation):
        with self.db.engine.connect() as connection:
            params = {"key": "sunrise:panel:" + sid}
            acquired = connection.scalar(
                text("SELECT pg_try_advisory_lock(hashtextextended(:key, 0))"), params
            )
            connection.commit()
            if not acquired:
                return {"status": "busy"}
            try:
                return operation()
            finally:
                connection.execute(text("SELECT pg_advisory_unlock(hashtextextended(:key, 0))"), params)
                connection.commit()

    def reconcile_owned(
        self, sid, initial=False, *, first_event=False, threshold_event=False, added_device_events=()
    ):
        return self._with_lock(
            sid,
            lambda: self._snapshot(
                sid,
                initial,
                first_event=first_event,
                threshold_event=threshold_event,
                added_device_events=added_device_events,
            ),
        )

    def _snapshot(self, sid, initial, *, first_event=False, threshold_event=False, added_device_events=()):
        with self.db.transaction() as session:
            sub = session.get(Subscription, sid)
            from .retention import fenced_in

            if sub is not None and fenced_in(session, sub):
                return {"status": "pending", "error": "subscription_cleanup_pending"}
            try:
                binding, proof = self._owned(session, sub)
            except ValueError:
                return {"status": "denied"}
            user_id = str(sub.panel_user_id) if self.panel.contract == "3.4_id" else sub.panel_uuid
            original_owner, original_version = sub.account_id, sub.version
            if panel_entitlements.pending_target(session, sub) or panel_entitlements.at_unapplied_boundary(
                session, sub
            ):
                return {"status": "pending", "error": "panel_target_pending"}
        try:
            snapshot = self.panel.snapshot(binding)
        except (PanelReview, PanelUnavailable, ValueError) as failure:
            if isinstance(failure, PanelMissing) and getattr(self.panel, "scoped_runtime_policy", None):
                return self._confirmed_scoped_absence(sid, binding, proof, original_owner, original_version)
            error = "panel_unavailable" if isinstance(failure, PanelUnavailable) else "panel_review_required"
            with self.db.transaction() as session:
                observation = session.scalar(
                    select(PanelObservation)
                    .where(PanelObservation.kind == "user", PanelObservation.resource_id == user_id)
                    .with_for_update()
                )
                if observation:
                    observation.last_error = error
                else:
                    session.add(
                        PanelObservation(
                            kind="user",
                            resource_id=user_id,
                            subscription_id=sid,
                            last_error=error,
                            payload={},
                        )
                    )
            return {"status": "error", "error": error}
        with self.db.transaction() as session:
            # Same account-before-subscription order as financial fulfillment; HTTP has already completed.
            session.scalar(select(Account).where(Account.id == original_owner).with_for_update())
            sub = session.scalar(select(Subscription).where(Subscription.id == sid).with_for_update())
            if sub is not None and fenced_in(session, sub):
                return {"status": "pending", "error": "subscription_cleanup_pending"}
            try:
                current, current_proof = self._owned(session, sub)
                if (
                    current != binding
                    or current_proof != proof
                    or snapshot["user_id"] != user_id
                    or sub.account_id != original_owner
                ):
                    raise ValueError
            except ValueError:
                return {"status": "denied"}
            if sub.version != original_version or panel_entitlements.pending_target(session, sub):
                return {"status": "pending", "error": "panel_target_pending"}
            alias_link = snapshot["connection_url"]
            if alias_link != sub.connection_url:
                if not getattr(self.panel, "scoped_runtime_policy", None) or not self.panel.scoped_host_alias(
                    sub.connection_url, alias_link
                ):
                    return {"status": "denied"}
                record = session.get(Setting, "panel_test:" + sid, with_for_update=True)
                if record is None or record.value != current_proof:
                    return {"status": "denied"}
                record.value = current_proof | {"connection_url": alias_link}
                sub.connection_url = alias_link
            synced_version, product_fact = panel_entitlements.adopt(session, sub, snapshot, binding)
            observation = session.scalar(
                select(PanelObservation)
                .where(PanelObservation.kind == "user", PanelObservation.resource_id == user_id)
                .with_for_update()
            )
            device_count = (
                observation.payload.get("device_count")
                if observation is not None and isinstance(observation.payload, dict)
                else None
            )
            baseline = (
                initial
                or not isinstance(device_count, int)
                or isinstance(device_count, bool)
                or not 0 <= device_count <= 1000
            )
            if observation is None:
                observation = PanelObservation(kind="user", resource_id=user_id, subscription_id=sid)
                session.add(observation)
            observation.status, observation.traffic_bytes, observation.traffic_limit_bytes = (
                snapshot["status"],
                snapshot["traffic_bytes"],
                snapshot["traffic_limit_bytes"],
            )
            observation.observed_at, observation.last_error = now(), None
            observation.payload = {
                "binding_rotations": observation.payload.get("binding_rotations", [])
                if isinstance(observation.payload, dict)
                else [],
                "expires_at": snapshot["expires_at"],
                "device_count": len(snapshot["devices"]),
                "devices_limit": snapshot["devices_limit"],
                "subscription_version": synced_version,
                "internal_squad_uuids": snapshot["internal_squad_uuids"],
                **product_fact,
                "source": "remnawave",
                "binding": binding,
                "environment": sub.environment,
                "account_id": str(sub.account_id),
                "connection_metadata": snapshot.get("connection_metadata", {}),
                "traffic_threshold": snapshot.get("traffic_threshold"),
            }
            if self.panel.contract == "3.4_id":
                from .commerce import Commerce
                from .subscription_engagement import SubscriptionEngagement

                SubscriptionEngagement(
                    Commerce(
                        self.db,
                        panel_provider=self.panel,
                        public_settings=self.settings,
                        connection_provider=object(),
                    ),
                    self.notifications,
                ).observe_in(session, sub, snapshot, first_event=first_event, threshold_event=threshold_event)
            devices = {
                device.panel_hwid: device
                for device in session.scalars(
                    select(Device)
                    .where(Device.subscription_id == sid, Device.panel_hwid.is_not(None))
                    .with_for_update()
                )
            }
            seen = set()
            for item in snapshot["devices"]:
                hwid = item["hwid"]
                seen.add(hwid)
                device = devices.get(hwid)
                added = device is None or device.revoked
                if device is None:
                    device = Device(subscription_id=sid, panel_hwid=hwid, generation=1)
                    session.add(device)
                elif device.revoked:
                    device.generation += 1
                device.revoked, device.name, device.platform = False, item["name"], item["platform"]
                device.observed_at, device.last_seen_at = now(), instant(item["updated_at"])
                session.flush()
                from_signed_added_event = (hwid, item["created_at"]) in added_device_events
                if (added and not baseline) or from_signed_added_event:
                    from .subscription_engagement import SubscriptionEngagement

                    body, nodes = new_device_notice_content(
                        session.get(Product, sub.product_id) if sub.product_id else None,
                        sub.name,
                        device.name,
                    )
                    self.notifications.emit_in(
                        session,
                        sub.account_id,
                        f"panel_device:{sid}:{device.id}:{device.generation}",
                        "new_device",
                        "Новое устройство",
                        body,
                        nodes=nodes,
                        target="/?tab=subscriptions",
                        channel="telegram",
                        allow_push=False,
                        actions=[SubscriptionEngagement.action(sid, "devices", "Устройства")],
                    )
            pending = set(
                session.scalars(
                    select(Job.payload["device_id"].as_string()).where(
                        Job.kind == "device_revoke",
                        Job.payload["subscription_id"].as_string() == sid,
                        Job.state.in_(["pending", "in_flight"]),
                    )
                )
            )
            for hwid, device in devices.items():
                if hwid not in seen and device.id not in pending:
                    if self.panel.contract == "3.4_id" and (
                        getattr(self.panel, "scoped_runtime_policy", None)
                        or getattr(self.panel, "runtime_policy", None)
                    ):
                        from .commerce import Commerce
                        from .moderation import DeviceModeration

                        DeviceModeration(
                            Commerce(
                                self.db,
                                panel_provider=self.panel,
                                public_settings=self.settings,
                                connection_provider=object(),
                            )
                        ).complete(session, sub.account_id, sid, device.id)
                    else:
                        device.revoked = True
                    device.observed_at = now()
        return {"status": "ok"}

    def _confirmed_scoped_absence(self, sid, binding, proof, owner, version):
        """Two exact user GET404s plus an admitted node GET200 authenticate absence."""
        from .commerce import Commerce
        from .retention import fenced_in
        from .subscription_archive import archive_absence_in

        policy = self.panel.scoped_runtime_policy
        node_ids = sorted(set(policy.scope.panel.node_uuids) & self.panel.nodes)
        if not node_ids or self.panel.contract != "3.4_id":
            return {"status": "error", "error": "panel_review_required"}
        try:
            policy.require()
            if self.panel.cleanup_snapshot(binding) is not None:
                return {"status": "error", "error": "panel_review_required"}
            self.panel.node_snapshot(node_ids[0])
            if self.panel.cleanup_snapshot(binding) is not None:
                return {"status": "error", "error": "panel_review_required"}
            policy.require()
        except (PanelReview, PanelUnavailable, ValueError, DomainError) as failure:
            return {
                "status": "error",
                "error": "panel_unavailable"
                if isinstance(failure, PanelUnavailable)
                else "panel_review_required",
            }
        read_proof = {
            "version": 1,
            "subscription_id": sid,
            "owner_id": str(owner),
            "environment": "scoped_live",
            "scope_id": policy.scope.scope_id,
            "panel_host": self.panel.api_host,
            "contract": self.panel.contract,
            "binding": binding,
            "subscription_version": version,
            "authority_node_uuid": node_ids[0],
            "observed_at": now().isoformat(),
        }
        proof_id = (
            "panel_read:"
            + hashlib.sha256(
                json.dumps(read_proof, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
        )
        try:
            with self.db.transaction() as session:
                session.scalar(select(Account).where(Account.id == owner).with_for_update())
                sub = session.scalar(select(Subscription).where(Subscription.id == sid).with_for_update())
                policy.require_current(session)
                if (
                    sub is None
                    or sub.account_id != owner
                    or sub.version != version
                    or fenced_in(session, sub)
                ):
                    return {"status": "denied"}
                current, current_proof = self._owned(session, sub)
                if (
                    current != binding
                    or current_proof != proof
                    or panel_entitlements.pending_target(session, sub)
                ):
                    return {"status": "denied"}
                commerce = Commerce(
                    self.db,
                    panel_provider=self.panel,
                    public_settings=self.settings,
                    connection_provider=object(),
                )
                session.add(Setting(key=proof_id, value=read_proof))
                session.flush()
                archive_absence_in(
                    session, commerce, sub, binding, reason="verified_panel_absence", proof_id=proof_id
                )
        except (DomainError, PanelReview, ValueError):
            return {"status": "denied"}
        except PanelUnavailable:
            return {"status": "error", "error": "panel_unavailable"}
        return {"status": "ok"}

    def reconcile_pending(self, limit=50):
        limit = max(1, min(100, int(limit)))
        with self.db.transaction() as session:
            candidates = list(
                session.execute(
                    select(PanelEvent.subscription_id, PanelEvent.payload, PanelEvent.kind)
                    .where(PanelEvent.state == "pending", PanelEvent.available_at <= now())
                    .order_by(PanelEvent.received_at, PanelEvent.id)
                    .limit(limit)
                )
            )
        completed, handled = 0, set()
        for sid, payload, kind in candidates:
            node = payload.get("node_uuid")
            infra = (payload.get("provider_name"), payload.get("node_name")) if kind == CRM_EVENT else None
            target = (
                "crm:" + hashlib.sha256(json.dumps(infra).encode()).hexdigest()
                if infra
                else "user:" + sid
                if sid
                else "node:" + str(node)
            )
            if target in handled:
                continue
            handled.add(target)
            result = self._with_lock(
                "events:" + target,
                lambda sid=sid, node=node, infra=infra: self._reconcile_event_target(sid, node, infra=infra),
            )
            completed += result.get("completed", 0)
        return completed

    def _reconcile_event_target(self, sid, node, *, infra=None):
        condition = (
            PanelEvent.subscription_id == sid
            if sid
            else (
                PanelEvent.subscription_id.is_(None) & (PanelEvent.payload["node_uuid"].as_string() == node)
            )
        )
        if infra:
            condition = (
                (PanelEvent.kind == CRM_EVENT)
                & (PanelEvent.payload["provider_name"].as_string() == infra[0])
                & (PanelEvent.payload["node_name"].as_string() == infra[1])
            )
        with self.db.transaction() as session:
            pending = list(
                session.execute(
                    select(PanelEvent.id, PanelEvent.available_at, PanelEvent.kind, PanelEvent.payload).where(
                        condition, PanelEvent.state == "pending"
                    )
                )
            )
            # A new event cannot turn an earlier failure into every-tick HTTP retries.
            if not pending or any(available > now() for _, available, _, _ in pending):
                return {"status": "deferred", "completed": 0}
            ids = [eid for eid, _, _, _ in pending]
            first_event = any(
                kind == "user.first_connected"
                and timedelta(0)
                <= now() - instant(payload["timestamp"])
                <= timedelta(seconds=self.settings.panel_webhook_max_age_seconds)
                for _, _, kind, payload in pending
            )
        deleted = next((eid for eid, _, kind, _ in pending if kind == "user.deleted"), None)
        if infra:
            from .panel_news import crm_intent_in

            try:
                with self.db.transaction() as session:
                    result = crm_intent_in(session, self.panel, self.settings, pending, self.notifications)
            except PanelUnavailable:
                result = {"status": "error", "error": "panel_unavailable"}
        elif sid and deleted:
            result = self._with_lock(sid, lambda: self._deleted_snapshot(sid, deleted))
        elif sid:
            result = self.reconcile_owned(
                sid,
                first_event=first_event,
                threshold_event=any(
                    kind == "user.bandwidth_usage_threshold_reached" for _, _, kind, _ in pending
                ),
                added_device_events=frozenset(
                    (payload["hwid"], payload["device_created_at"])
                    for _, _, kind, payload in pending
                    if kind == "user_hwid_devices.added" and "device_created_at" in payload
                ),
            )
        else:
            result = self._reconcile_node(node, events=pending)
        completed = 0
        with self.db.transaction() as session:
            for event in session.scalars(
                select(PanelEvent).where(PanelEvent.id.in_(ids)).order_by(PanelEvent.id).with_for_update()
            ):
                if event.state != "pending" or result["status"] == "busy":
                    continue
                event.attempts += 1
                if result["status"] == "ok":
                    event.state, event.last_error = "done", None
                    completed += 1
                elif result["status"] == "denied":
                    event.state, event.last_error = "ignored", "panel_identity_denied"
                else:
                    event.last_error = result["error"]
                    event.state = "review" if event.last_error == "panel_review_required" else "pending"
                    event.available_at = now() + timedelta(
                        seconds=min(3600, 30 * 2 ** min(event.attempts, 6))
                    )
        return {"status": result["status"], "completed": completed}

    def _deleted_snapshot(self, sid, event_id):
        from .commerce import Commerce
        from .subscription_archive import archive_absence_in
        from .subscription_archive import state_in as archive_state_in

        with self.db.transaction() as session:
            sub = session.get(Subscription, sid)
            try:
                binding, proof = self._owned(session, sub)
                if archive_state_in(session, sub) is not None:
                    return {"status": "ok"}
            except (DomainError, ValueError):
                return {"status": "denied"}
            owner, version = sub.account_id, sub.version
        try:
            if self.panel.cleanup_snapshot(binding) is not None:
                return {"status": "error", "error": "panel_deletion_unconfirmed"}
        except (PanelReview, PanelUnavailable, ValueError) as failure:
            return {
                "status": "error",
                "error": "panel_unavailable"
                if isinstance(failure, PanelUnavailable)
                else "panel_review_required",
            }
        try:
            with self.db.transaction() as session:
                session.scalar(select(Account).where(Account.id == owner).with_for_update())
                sub = session.scalar(select(Subscription).where(Subscription.id == sid).with_for_update())
                try:
                    current, current_proof = self._owned(session, sub)
                    if (
                        sub.account_id != owner
                        or sub.version != version
                        or current != binding
                        or current_proof != proof
                    ):
                        raise ValueError
                    commerce = Commerce(
                        self.db,
                        panel_provider=self.panel,
                        public_settings=self.settings,
                        connection_provider=object(),
                    )
                    archive_absence_in(
                        session, commerce, sub, binding, reason="manual_panel", proof_id=event_id
                    )
                except (DomainError, PanelReview, ValueError):
                    return {"status": "denied"}
        except PanelUnavailable:
            # Leave the outer transaction before mapping to durable backoff:
            # user/owner notifications and archive state must roll back together.
            return {"status": "error", "error": "panel_unavailable"}
        return {"status": "ok"}

    def _reconcile_node(self, node_uuid, *, events=()):
        if not self.panel:
            return {"status": "denied"}
        policy = getattr(self.panel, "runtime_policy", None)
        if policy and policy.production:
            try:
                with self.db.transaction() as session:
                    policy.require_current(session)
                    if node_uuid not in set(policy.scope.panel.node_uuids) & self.panel.nodes:
                        return {"status": "denied"}
            except DomainError:
                return {"status": "denied"}
        elif node_uuid not in set(getattr(self.settings, "panel_test_node_uuids", ())):
            return {"status": "denied"}
        scoped = getattr(self.panel, "scoped_runtime_policy", None)
        if scoped:
            try:
                with self.db.transaction() as session:
                    scoped.require_current(session)
                    if node_uuid not in set(scoped.scope.panel.node_uuids) & self.panel.nodes:
                        return {"status": "denied"}
            except DomainError:
                return {"status": "denied"}
        from .panel_event_routes import policy_in

        with self.db.transaction() as session:
            routes = policy_in(session, self.panel, self.settings)
            if routes is None or not any(route.node_uuid == node_uuid for route in routes.nodes):
                return {"status": "denied"}
        if events and max(events, key=lambda event: instant(event[3]["timestamp"]))[2] == "node.deleted":
            return {"status": "ok"}
        return self._with_lock("node:" + node_uuid, lambda: self._node_snapshot(node_uuid, events=events))

    def _node_snapshot(self, node_uuid, *, events=()):
        try:
            snapshot = self.panel.node_snapshot(node_uuid)
        except (PanelReview, PanelUnavailable, ValueError) as failure:
            error = "panel_unavailable" if isinstance(failure, PanelUnavailable) else "panel_review_required"
            with self.db.transaction() as session:
                observation = session.scalar(
                    select(PanelObservation)
                    .where(PanelObservation.kind == "node", PanelObservation.resource_id == node_uuid)
                    .with_for_update()
                )
                if observation:
                    observation.last_error = error
            return {
                "status": "error",
                "error": error,
            }
        with self.db.transaction() as session:
            observation = session.scalar(
                select(PanelObservation)
                .where(PanelObservation.kind == "node", PanelObservation.resource_id == node_uuid)
                .with_for_update()
            )
            if observation is None:
                observation = PanelObservation(kind="node", resource_id=node_uuid)
                session.add(observation)
            observation.status, observation.observed_at, observation.last_error = (
                snapshot["status"],
                now(),
                None,
            )
            observation.payload = {"name": snapshot["name"], "country_code": snapshot["country_code"]}
            from .panel_news import record_node_news_in

            record_node_news_in(session, self.panel, self.settings, node_uuid, snapshot, events)
        return {"status": "ok"}

    def nodes_snapshot(self):
        allowed = set(getattr(self.settings, "panel_test_node_uuids", ()))
        if (
            not allowed
            or not self.panel
            or getattr(self.panel, "contract", None) not in {"3.4_id", "2.8_uuid"}
        ):
            return []
        for node_uuid in sorted(allowed)[:20]:
            with self.db.transaction() as session:
                observation = session.scalar(
                    select(PanelObservation).where(
                        PanelObservation.kind == "node", PanelObservation.resource_id == node_uuid
                    )
                )
                stale = observation is None or observation.observed_at < now() - timedelta(
                    seconds=max(30, self.settings.panel_reconcile_interval_seconds)
                )
            if stale:
                self._reconcile_node(node_uuid)
        with self.db.transaction() as session:
            return [
                {
                    "id": item.resource_id,
                    "status": item.status,
                    "name": item.payload.get("name"),
                    "country_code": item.payload.get("country_code"),
                    "observed_at": item.observed_at.isoformat(),
                    "error": item.last_error,
                }
                for item in session.scalars(
                    select(PanelObservation)
                    .where(
                        PanelObservation.kind == "node",
                        PanelObservation.resource_id.in_(sorted(allowed)[:20]),
                    )
                    .order_by(PanelObservation.resource_id)
                )
            ]
