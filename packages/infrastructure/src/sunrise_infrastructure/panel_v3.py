"""Remnawave 3.4: проверяемые цели с раздельными тестовыми и production допусками."""

import hashlib
import re
from datetime import UTC, datetime
from urllib.parse import urlparse
from uuid import UUID

import httpx
from sqlalchemy import select

from .db import Alias, LegacyIssue
from .panel import (
    PanelReview,
    PanelUnavailable,
    PanelUsernameConflict,
    RemnawaveTest,
    panel_expiry,
    snapshot_device_limit,
    snapshot_squads,
)


def numeric_id(value):
    """JSON number проверяется до превращения в внутреннюю десятичную строку."""
    if type(value) is not int or not 1 <= value <= 9007199254740991:
        raise PanelReview("panel_identity_mismatch")
    return str(value)


def connection_metadata(user):
    """3.4.5 connection evidence: missing is unknown, explicit null remains null."""
    if not isinstance(user, dict) or not isinstance(user.get("userTraffic"), dict):
        return {}
    result, traffic = {}, user["userTraffic"]
    try:
        if "createdAt" in user:
            result["created_at"] = instant(user["createdAt"]).isoformat()
        for name, field in (("firstConnectedAt", "first_connected_at"), ("onlineAt", "online_at")):
            if name in traffic:
                result[field] = instant(traffic[name]).isoformat() if traffic[name] is not None else None
        if "lifetimeUsedTrafficBytes" in traffic:
            value = traffic["lifetimeUsedTrafficBytes"]
            if type(value) is not int or not 0 <= value <= 9007199254740991:
                return {}
            result["lifetime_traffic_bytes"] = str(value)
    except (PanelReview, TypeError, ValueError):
        return {}
    return result


def instant(value):
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None:
            raise ValueError
        return result
    except (AttributeError, ValueError, TypeError):
        raise PanelReview("panel_response_unverified") from None


def safe_text(value, limit):
    if value is None:
        return None
    if not isinstance(value, str):
        raise PanelReview("panel_response_unverified")
    return "".join(c for c in value if ord(c) >= 32 and ord(c) != 127)[:limit]


def registered_owner(session, value, sub, account, panel):
    """Первоначальный допуск неизменяем; подтверждённый merge разрешает нового canonical owner."""
    original = value.get("account_id")
    scoped = getattr(panel, "scoped_runtime_policy", None)
    if scoped:
        scoped.require_current(session)
        scoped.require_account_in(session, account)
    if (
        not account
        or not isinstance(original, str)
        or not re.fullmatch(r"[1-9][0-9]{0,18}", original)
        or int(original) >= 2**63
        or not scoped
        and value.get("telegram_id") not in panel.auto_test_telegram_ids
        or scoped
        and not scoped.owns_original_in(
            session, account, original, telegram_id=value.get("telegram_id"), check_telegram=True
        )
        or value.get("origin") != "new_test_identity"
        or value.get("contract") != "3.4_id"
        or value.get("subscription_id") != sub.id
        or value.get("environment") != sub.environment
        or value.get("panel_host") != panel.api_host
        or session.get(Alias, account.id)
        or session.scalar(
            select(LegacyIssue.id)
            .where(LegacyIssue.source_table == "subscriptions", LegacyIssue.source_id == sub.id)
            .limit(1)
        )
    ):
        return False
    if original == str(account.id):
        return True
    alias = session.get(Alias, int(original))
    return bool(alias and alias.canonical_id == account.id)


class RemnawaveV3:
    contract = "3.4_id"
    _request = RemnawaveTest._request
    validate_target = RemnawaveTest.validate_target
    _safe_connection_url = RemnawaveTest._safe_connection_url
    _cleanup_binding = RemnawaveTest._cleanup_binding
    cleanup_snapshot = RemnawaveTest.cleanup_snapshot

    def __init__(
        self,
        *,
        base_url,
        token,
        authorized=False,
        allowed_hosts=(),
        allowed_squads=(),
        allowed_external_squads=(),
        allowed_subscription_hosts=(),
        auto_test_telegram_ids=(),
        allowed_user_ids=(),
        allowed_nodes=(),
        bindings=None,
        transport=None,
    ):
        url = urlparse(base_url or "")
        if (
            not authorized
            or not isinstance(token, str)
            or not token
            or any(ord(c) <= 32 for c in token)
            or url.scheme != "https"
            or url.hostname not in set(allowed_hosts)
            or url.username
            or url.password
            or url.port not in {None, 443}
            or url.path not in {"", "/"}
            or url.query
            or url.fragment
            or "*" in allowed_hosts
        ):
            raise ValueError("panel_configuration_denied")
        self.api_host = url.hostname
        self.squads = set(allowed_squads)
        self.external_squads = set(allowed_external_squads)
        self.subscription_hosts = set(allowed_subscription_hosts)
        self.auto_test_telegram_ids = {str(x) for x in auto_test_telegram_ids}
        self.allowed_user_ids = {str(x) for x in allowed_user_ids}
        self.nodes = set(allowed_nodes)
        self.bindings = dict(bindings or {})
        self.registered = {}
        self.connection_pins = {}
        self.device_bindings = set()
        self.hwids = set()
        if (
            not self.squads
            or not self.auto_test_telegram_ids
            or any(not re.fullmatch(r"[1-9][0-9]{0,18}", x) for x in self.auto_test_telegram_ids)
            or "*" in self.subscription_hosts
        ):
            raise ValueError("panel_allowlist_required")
        for value in self.squads | self.external_squads | self.nodes:
            if str(UUID(value)) != value:
                raise ValueError("panel_uuid_invalid")
        for value in self.allowed_user_ids:
            if not re.fullmatch(r"[1-9][0-9]{0,15}", value) or int(value) > 9007199254740991:
                raise ValueError("panel_identity_denied")
        self.auto_scope = hashlib.sha256(
            (self.api_host + "|3.4_id|" + ",".join(sorted(self.auto_test_telegram_ids))).encode()
        ).hexdigest()[:32]
        self.client = httpx.Client(
            base_url=base_url.rstrip("/") + "/api/",
            headers={"Authorization": "Bearer " + token},
            timeout=10,
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        )

    def register_test_binding(self, binding, *, telegram_id, connection_url=None, account_id=None):
        nonce = binding.get("origin_nonce")
        user_id = binding.get("user_id")
        scoped = getattr(self, "scoped_runtime_policy", None)
        if scoped and account_id is not None:
            from .db import Account, Alias

            with scoped._database.transaction() as session:
                scoped.require_current(session)
                account = session.get(Account, int(account_id))
                if account is None:
                    alias = session.get(Alias, int(account_id))
                    account = session.get(Account, alias.canonical_id) if alias else None
                scoped.require_account_in(session, account)
                if not scoped.owns_original_in(
                    session, account, str(account_id), telegram_id=telegram_id, check_telegram=True
                ):
                    raise ValueError("panel_identity_denied")
        if (
            not (scoped and account_id is not None)
            and str(telegram_id) not in self.auto_test_telegram_ids
            or not isinstance(nonce, str)
            or not re.fullmatch(r"[a-f0-9]{32}", nonce)
            or binding.get("username") != "sunrise_test_" + nonce[:20]
            and not (
                scoped
                and isinstance(binding.get("username"), str)
                and re.fullmatch(r"[A-Za-z0-9_-]{3,36}", binding["username"])
                and (
                    binding["username"].endswith("_" + nonce[:20])
                    or re.fullmatch(r"[A-Za-z0-9_-]+_[1-9][0-9]{0,3}", binding["username"])
                )
            )
            or not isinstance(binding.get("short_uuid"), str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{16,64}", binding["short_uuid"])
            or (
                user_id is not None
                and (
                    not isinstance(user_id, str)
                    or not re.fullmatch(r"[1-9][0-9]{0,15}", user_id)
                    or int(user_id) > 9007199254740991
                )
            )
            or (
                connection_url is not None
                and not self._safe_connection_url(connection_url, automatic=not self.subscription_hosts)
            )
        ):
            raise ValueError("panel_identity_denied")
        previous = self.registered.get(nonce)
        if previous and previous.get("user_id") not in {None, user_id}:
            raise ValueError("panel_identity_denied")
        self.registered[nonce] = dict(binding)
        self.connection_pins[nonce] = connection_url

    def delete_user(self, binding, *, before_delete):
        self._cleanup_binding(binding)
        frozen = dict(binding)
        scope = (self.api_host, self.client.base_url)
        before_delete()
        self._cleanup_binding(binding)
        if binding != frozen or scope != (self.api_host, self.client.base_url):
            raise PanelReview("panel_cleanup_scope_denied")
        try:
            with self.client.stream("DELETE", "users/" + frozen["user_id"]) as response:
                if response.status_code >= 500 or response.status_code == 429:
                    raise PanelUnavailable("panel_unavailable")
                if response.status_code != 204 or any(response.iter_bytes(chunk_size=1)):
                    raise PanelReview("panel_delete_unverified")
        except httpx.HTTPError:
            raise PanelUnavailable("panel_unavailable") from None

    def sync_contacts(self, binding, contacts, *, before_patch, before_start=None):
        from types import SimpleNamespace

        from .panel_contacts import profile_contacts

        scoped = getattr(self, "scoped_runtime_policy", None)
        if (
            not scoped
            or not callable(before_patch)
            or binding.get("user_id") is None
            or not isinstance(contacts, dict)
            or set(contacts) - {"email", "telegramId"}
            or "telegramId" not in contacts
            or profile_contacts(
                SimpleNamespace(
                    telegram_id=contacts["telegramId"], email=contacts.get("email"), email_verified=True
                )
            )
            != contacts
        ):
            raise PanelReview("panel_contact_invalid")
        scoped.require("panel_mutation")
        self.validate_binding(binding)
        current = self._request("GET", "users/" + binding["user_id"], missing=True)
        self._identity(current, binding)
        self._connection(current, binding)
        before_patch()
        if all(name in current and current[name] == value for name, value in contacts.items()):
            return current
        if before_start is not None:
            before_start(current)
        scoped.require("panel_mutation")
        before_patch()
        self._request("PATCH", "users", {"id": int(binding["user_id"])} | contacts)
        result = self._request("GET", "users/" + binding["user_id"])
        self._identity(result, binding)
        self._connection(result, binding)
        if any(name not in result or result[name] != value for name, value in contacts.items()):
            raise PanelUnavailable("panel_contacts_not_observed")
        return result

    def validate_binding(self, binding):
        if not isinstance(binding, dict) or self.registered.get(binding.get("origin_nonce")) != binding:
            raise ValueError("panel_identity_denied")

    def _identity(self, user, binding, *, rotated=False):
        if not isinstance(user, dict):
            raise PanelReview("panel_identity_mismatch")
        user_id = numeric_id(user.get("id"))
        if (
            user.get("username") != binding["username"]
            or (binding.get("user_id") is not None and user_id != binding["user_id"])
            or (not rotated and user.get("shortUuid") != binding["short_uuid"])
        ):
            raise PanelReview("panel_identity_mismatch")
        return user_id

    def _connection(self, user, binding, *, rotated=False):
        short = user.get("shortUuid")
        link = user.get("subscriptionUrl")
        if not isinstance(short, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,64}", short):
            raise PanelReview("panel_response_unverified")
        if not self._safe_connection_url(link, automatic=not self.subscription_hosts):
            raise PanelReview("panel_connection_denied")
        pinned = self.connection_pins.get(binding["origin_nonce"])
        if not rotated and pinned is not None and pinned != link and not self.scoped_host_alias(pinned, link):
            raise PanelReview("panel_connection_changed")
        return {
            "user_id": numeric_id(user.get("id")),
            "uuid": None,
            "short_uuid": short,
            "connection_url": link,
            "connection_metadata": connection_metadata(user),
        }

    def scoped_host_alias(self, pinned, link):
        scoped = getattr(self, "scoped_runtime_policy", None)
        if not scoped or not self._safe_connection_url(pinned) or not self._safe_connection_url(link):
            return False
        old, new = urlparse(pinned), urlparse(link)
        return (
            old.hostname != new.hostname
            and old.hostname in scoped.scope.panel.subscription_hosts
            and new.hostname in scoped.scope.panel.subscription_hosts
            and old.path == new.path
            and old.port == new.port
        )

    def _capacity_body(self, binding, target, current, *, create):
        return {"hwidDeviceLimit": target["devices_limit"]}

    def _capacity_observed(self, result, target):
        return (
            type(result.get("hwidDeviceLimit")) is int
            and result["hwidDeviceLimit"] == target["devices_limit"]
        )

    def apply(
        self, binding, target, *, create=False, uncertain=False, before_create=None, before_mutation=None,
        select_candidate=None,
    ):
        self.validate_binding(binding)
        self.validate_target(target)
        scoped = getattr(self, "scoped_runtime_policy", None)
        path = (
            "users/" + binding["user_id"]
            if binding.get("user_id")
            else "users/by-short-uuid/" + binding["short_uuid"]
            if scoped and uncertain
            else "users/by-username/" + binding["username"]
        )
        if scoped and create and not uncertain and binding.get("user_id") is None:
            current = None
        else:
            try:
                current = self._request("GET", path, missing=True)
            except PanelUnavailable:
                if create is True and uncertain is False:
                    raise PanelUnavailable(create_not_started=True) from None
                raise
        body = {
            "expireAt": panel_expiry(target["expires_at"]),
            **self._capacity_body(binding, target, current, create=create),
            "activeInternalSquads": [target["internal_squad_uuid"]],
            "status": target.get("panel_status", "ACTIVE"),
        }
        if target.get("external_squad_uuid") is not None:
            body["externalSquadUuid"] = target["external_squad_uuid"]
        if current is None:
            if not create or uncertain or binding.get("user_id") is not None:
                raise PanelReview("panel_creation_unresolved")
            for attempt in range(16):
                frozen, origin = dict(binding), self.client.base_url
                if before_create is not None:
                    before_create()
                    self.validate_binding(binding)
                    if binding != frozen or self.client.base_url != origin:
                        raise PanelReview("panel_creation_scope_changed") from None
                if before_mutation is not None:
                    before_mutation()
                try:
                    created = self._request(
                        "POST",
                        "users",
                        body
                        | {
                            "username": binding["username"],
                            "shortUuid": binding["short_uuid"],
                            "trafficLimitBytes": 0,
                            "trafficLimitStrategy": "NO_RESET",
                        },
                    )
                except PanelUsernameConflict:
                    if not scoped or select_candidate is None:
                        raise
                    if attempt == 15:
                        raise PanelReview("panel_name_exhausted") from None
                    candidate = select_candidate(dict(binding))
                    if (
                        candidate.get("origin_nonce") != binding["origin_nonce"]
                        or candidate.get("short_uuid") != binding["short_uuid"]
                        or candidate.get("user_id") is not None
                    ):
                        raise PanelReview("panel_creation_scope_changed") from None
                    binding = candidate
                    self.validate_binding(binding)
                    continue
                break
            else:
                raise PanelReview("panel_name_exhausted")
            user_id = self._identity(created, binding)
        else:
            user_id = self._identity(current, binding)
            if create and not uncertain:
                raise PanelReview("panel_identity_collision")
            patch = body | {"id": int(user_id)}
            if instant(body["expireAt"]) <= datetime.now(UTC):
                if body["status"] != "DISABLED" or instant(current.get("expireAt")) != instant(
                    body["expireAt"]
                ):
                    raise PanelReview("panel_expiry_reconciliation_required")
                patch.pop("expireAt")
            if before_mutation is not None:
                before_mutation()
            self._request("PATCH", "users", patch)
        result = self._request("GET", "users/" + user_id)
        resolved = binding | {"user_id": user_id}
        self._identity(result, resolved)
        squads = result.get("activeInternalSquads")
        if (
            not isinstance(squads, list)
            or any(not isinstance(x, dict) or not isinstance(x.get("uuid"), str) for x in squads)
            or {x["uuid"] for x in squads} != set(body["activeInternalSquads"])
            or not self._capacity_observed(result, target)
            or instant(result.get("expireAt")) != instant(body["expireAt"])
            or result.get("status") != body["status"]
            or ("externalSquadUuid" in body and result.get("externalSquadUuid") != body["externalSquadUuid"])
        ):
            raise PanelUnavailable("panel_target_not_observed")
        return self._connection(result, binding) | {
            "applied_target": {
                "status": result["status"],
                "expires_at": instant(result["expireAt"]).isoformat(),
                "devices_limit": result["hwidDeviceLimit"],
                "internal_squad_uuids": sorted(x["uuid"] for x in squads),
            }
        }

    def _inventory(self, binding):
        result = self._request("GET", "hwid/devices/" + binding["user_id"])
        devices = result.get("devices")
        if not isinstance(devices, list) or len(devices) > 100:
            raise PanelReview("panel_response_unverified")
        safe, seen = [], set()
        for item in devices:
            if not isinstance(item, dict):
                raise PanelReview("panel_response_unverified")
            hwid = item.get("hwid")
            if (
                numeric_id(item.get("userId")) != binding["user_id"]
                or not isinstance(hwid, str)
                or not 1 <= len(hwid) <= 512
                or any(ord(c) <= 32 for c in hwid)
                or hwid in seen
            ):
                raise PanelReview("panel_device_denied")
            seen.add(hwid)
            created, updated = instant(item.get("createdAt")), instant(item.get("updatedAt"))
            safe.append(
                {
                    "hwid": hwid,
                    "name": safe_text(item.get("deviceModel"), 200)
                    or safe_text(item.get("platform"), 100)
                    or "Устройство",
                    "platform": safe_text(item.get("platform"), 100),
                    "created_at": created.isoformat(),
                    "updated_at": updated.isoformat(),
                }
            )
        if type(result.get("total")) is not int or result["total"] != len(devices):
            raise PanelReview("panel_response_unverified")
        return safe

    def snapshot(self, binding):
        self.validate_binding(binding)
        if binding.get("user_id") is None:
            raise ValueError("panel_identity_denied")
        user = self._request("GET", "users/" + binding["user_id"])
        self._identity(user, binding)
        result = self._connection(user, binding)
        traffic = user.get("userTraffic")
        used = traffic.get("usedTrafficBytes") if isinstance(traffic, dict) else None
        limit = user.get("trafficLimitBytes")
        if any(
            x is not None and (type(x) is not int or not 0 <= x <= 9007199254740991) for x in [used, limit]
        ):
            raise PanelReview("panel_response_unverified")
        if user.get("status") not in {"ACTIVE", "DISABLED", "LIMITED", "EXPIRED"}:
            raise PanelReview("panel_response_unverified")
        return result | {
            "status": user["status"],
            "traffic_threshold": user.get("lastTriggeredThreshold")
            if type(user.get("lastTriggeredThreshold")) is int and 1 <= user["lastTriggeredThreshold"] <= 100
            else None,
            "traffic_reset_at": instant(user["lastTrafficResetAt"]).isoformat()
            if user.get("lastTrafficResetAt")
            else None,
            "traffic_cycle_known": "lastTrafficResetAt" in user
            and (user["lastTrafficResetAt"] is None or isinstance(user["lastTrafficResetAt"], str)),
            "traffic_bytes": used,
            "traffic_limit_bytes": limit,
            "devices_limit": snapshot_device_limit(user),
            "internal_squad_uuids": snapshot_squads(user),
            "devices": self._inventory(binding),
            "expires_at": instant(user.get("expireAt")).isoformat(),
            "sub_revoked_at": instant(user["subRevokedAt"]).isoformat() if user.get("subRevokedAt") else None,
        }

    def engagement_snapshot(self, binding):
        self._cleanup_binding(binding)
        user = self._request("GET", "users/" + binding["user_id"])
        self._identity(user, binding)
        result = self._connection(user, binding)
        if user.get("status") not in {"ACTIVE", "DISABLED", "LIMITED", "EXPIRED"}:
            raise PanelReview("panel_response_unverified")
        if not set(snapshot_squads(user)) <= self.squads:
            raise PanelReview("panel_engagement_scope_denied")
        return result | {
            "status": user["status"],
            "expires_at": instant(user.get("expireAt")).isoformat(),
        }

    def register_device(self, binding, hwid):
        self.validate_binding(binding)
        if not binding.get("user_id") or not isinstance(hwid, str) or not 1 <= len(hwid) <= 512:
            raise ValueError("panel_device_denied")
        self.device_bindings.add((binding["user_id"], hwid))
        self.hwids.add(hwid)

    def revoke(self, binding, hwid, *, uncertain=False, before_delete=None):
        self.validate_binding(binding)
        if (binding.get("user_id"), hwid) not in self.device_bindings:
            raise ValueError("panel_device_denied")
        self._identity(self._request("GET", "users/" + binding["user_id"]), binding)
        if not any(x["hwid"] == hwid for x in self._inventory(binding)):
            return bool(uncertain)
        if uncertain:
            raise PanelReview("panel_device_delete_unresolved")
        if before_delete:
            before_delete()
        self._request("POST", "hwid/devices/delete", {"userId": int(binding["user_id"]), "hwid": hwid})
        if any(x["hwid"] == hwid for x in self._inventory(binding)):
            raise PanelUnavailable("panel_device_not_observed")
        return True

    def rotate(
        self,
        binding,
        *,
        revoke_only_passwords=False,
        uncertain=False,
        before_short_uuid=None,
        before_sub_revoked_at=None,
        before_expires_at=None,
        before_rotate=None,
    ):
        self.validate_binding(binding)
        if not binding.get("user_id") or type(revoke_only_passwords) is not bool:
            raise ValueError("panel_identity_denied")
        if uncertain and (before_short_uuid != binding["short_uuid"] or before_expires_at is None):
            raise PanelReview("panel_rotation_unresolved")
        user = self._request("GET", "users/" + binding["user_id"])
        self._identity(user, binding, rotated=uncertain)
        before_short = before_short_uuid or binding["short_uuid"]
        if uncertain:
            if before_expires_at is not None and instant(user.get("expireAt")) != instant(before_expires_at):
                raise PanelReview("panel_rotation_rights_changed")
            if revoke_only_passwords:
                marker = user.get("subRevokedAt")
                changed = marker is not None and instant(marker) != (
                    instant(before_sub_revoked_at) if before_sub_revoked_at else None
                )
                if user.get("shortUuid") != before_short or not changed:
                    raise PanelReview("panel_rotation_unresolved")
            elif user.get("shortUuid") == before_short:
                raise PanelReview("panel_rotation_unresolved")
            return self._connection(user, binding, rotated=True)
        verified_expiry = instant(user.get("expireAt"))
        self._connection(user, binding)
        previous = user.get("subRevokedAt")
        if not callable(before_rotate):
            raise ValueError("panel_rotation_proof_required")
        before_rotate(
            {
                "before_short_uuid": user["shortUuid"],
                "before_sub_revoked_at": previous,
                "before_expires_at": user["expireAt"],
            }
        )
        self._request(
            "POST",
            "users/" + binding["user_id"] + "/actions/revoke",
            {"revokeOnlyPasswords": revoke_only_passwords},
        )
        result = self._request("GET", "users/" + binding["user_id"])
        self._identity(result, binding, rotated=True)
        if instant(result.get("expireAt")) != verified_expiry:
            raise PanelReview("panel_rotation_rights_changed")
        if revoke_only_passwords:
            marker = result.get("subRevokedAt")
            if (
                result.get("shortUuid") != user["shortUuid"]
                or marker is None
                or (previous is not None and instant(marker) == instant(previous))
            ):
                raise PanelReview("panel_rotation_unresolved")
        elif result.get("shortUuid") == user["shortUuid"]:
            raise PanelReview("panel_rotation_unresolved")
        return self._connection(result, binding, rotated=True)

    def node_snapshot(self, node_uuid):
        if node_uuid not in self.nodes:
            raise ValueError("panel_node_denied")
        node = self._request("GET", "nodes/" + node_uuid)
        if (
            node.get("uuid") != node_uuid
            or type(node.get("isConnected")) is not bool
            or type(node.get("isDisabled")) is not bool
        ):
            raise PanelReview("panel_response_unverified")
        return {
            "uuid": node_uuid,
            "name": safe_text(node.get("name"), 100),
            "status": "DISABLED"
            if node["isDisabled"]
            else "CONNECTED"
            if node["isConnected"]
            else "DISCONNECTED",
            "country_code": safe_text(node.get("countryCode"), 8),
        }


def production_binding_key(sid):
    key = "panel_production:" + sid
    return key if len(key) <= 80 else "panel_production:" + hashlib.sha256(sid.encode()).hexdigest()


def registered_project_owner(session, value, sub, account, panel):
    from .panel_entitlements import owned_account

    binding = value.get("binding") if isinstance(value, dict) else None
    policy = getattr(panel, "runtime_policy", None)
    return bool(
        policy
        and policy.production
        and account
        and sub.environment == "live"
        and value.get("scope_id") == policy.scope.scope_id
        and value.get("origin") in {"new_project_identity", "imported_project_identity"}
        and value.get("contract") == "3.4_id"
        and value.get("panel_host") == panel.api_host
        and value.get("subscription_id") == sub.id
        and value.get("environment") == "live"
        and owned_account(session, sub, value.get("account_id"))
        and isinstance(binding, dict)
        and binding.get("scope_id") == policy.scope.scope_id
        and type(value.get("generation")) is int
        and value["generation"] > 0
        and binding.get("generation") == value["generation"]
        and binding.get("user_id") == (str(sub.panel_user_id) if sub.panel_user_id is not None else None)
        and binding.get("uuid") == sub.panel_uuid
        and sub.short_uuid in {None, binding.get("short_uuid")}
        and value.get("connection_url") == sub.connection_url
    )


def explicit_null_evidence(value):
    if (
        not isinstance(value, dict)
        or set(value) != {"kind", "present", "source", "until"}
        or value.get("kind") != "explicit_null_preserved"
        or value.get("present") is not True
        or value.get("source") != "authenticated_panel_snapshot"
        or not isinstance(value.get("until"), str)
    ):
        return False
    try:
        instant(value["until"])
    except (TypeError, ValueError, PanelReview):
        return False
    return True


class RemnawaveProduction(RemnawaveV3):
    """Approved project bindings; test identities and nullable Telegram are unrelated."""

    def __init__(self, *, runtime_policy, token, transport=None):
        from contextvars import ContextVar

        if (
            not runtime_policy
            or not runtime_policy.production
            or not isinstance(token, str)
            or not token
            or any(ord(c) <= 32 for c in token)
        ):
            raise ValueError("production_panel_configuration_denied")
        runtime_policy.require()
        self.runtime_policy = runtime_policy
        self._mutation_authority = ContextVar("production_panel_mutation", default=None)
        self._creation_contacts = ContextVar("production_panel_contacts", default=None)
        scope = runtime_policy.scope.panel
        self.api_host = urlparse(scope.origin).hostname
        self.squads, self.external_squads = set(scope.internal_squads), set(scope.external_squads)
        self.subscription_hosts, self.nodes = set(scope.subscription_hosts), set(scope.node_uuids)
        self.bindings, self.registered, self.connection_pins = {}, {}, {}
        self.capacity_evidence = {}
        self.device_bindings, self.hwids, self.allowed_user_ids, self.auto_test_telegram_ids = (
            set(),
            set(),
            set(),
            set(),
        )
        self.client = httpx.Client(
            base_url=scope.origin + "/api/",
            headers={"Authorization": "Bearer " + token},
            transport=transport,
            timeout=10,
            follow_redirects=False,
            trust_env=False,
        )

    def register_project_binding(self, binding, *, connection_url=None, capacity_evidence=None):
        if (
            not isinstance(binding, dict)
            or binding.get("scope_id") != self.runtime_policy.scope.scope_id
            or type(binding.get("generation")) is not int
            or binding["generation"] <= 0
            or not re.fullmatch(r"[a-f0-9]{32}", binding.get("origin_nonce", ""))
            or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", binding.get("username", ""))
            or not re.fullmatch(r"[A-Za-z0-9_-]{16,64}", binding.get("short_uuid", ""))
            or binding.get("user_id") is not None
            and numeric_id(int(binding["user_id"])) != binding["user_id"]
            or connection_url is not None
            and not self._safe_connection_url(connection_url)
        ):
            raise ValueError("panel_identity_denied")
        if binding.get("uuid") is not None and str(UUID(binding["uuid"])) != binding["uuid"]:
            raise ValueError("panel_identity_denied")
        self.registered[binding["origin_nonce"]] = dict(binding)
        self.connection_pins[binding["origin_nonce"]] = connection_url
        if capacity_evidence is not None and not explicit_null_evidence(capacity_evidence):
            raise ValueError("panel_capacity_evidence_denied")
        self.capacity_evidence[binding["origin_nonce"]] = (
            dict(capacity_evidence) if capacity_evidence else None
        )

    def validate_target(self, value):
        if value.get("devices_limit") is None:
            if value.get("preserve_null_capacity") is not True or not explicit_null_evidence(
                value.get("capacity_evidence")
            ):
                raise ValueError("panel_capacity_evidence_denied")
            return super().validate_target(value | {"devices_limit": 1})
        return super().validate_target(value)

    def _capacity_body(self, binding, target, current, *, create):
        if target["devices_limit"] is not None:
            return super()._capacity_body(binding, target, current, create=create)
        evidence = self.capacity_evidence.get(binding["origin_nonce"])
        if (
            create
            or binding.get("user_id") is None
            or current is None
            or evidence != target.get("capacity_evidence")
            or not explicit_null_evidence(evidence)
            or instant(evidence["until"]) <= datetime.now(UTC)
            or "hwidDeviceLimit" not in current
            or current["hwidDeviceLimit"] is not None
            or snapshot_squads(current) != [target["internal_squad_uuid"]]
            or (
                target.get("external_squad_uuid") is not None
                and current.get("externalSquadUuid") != target["external_squad_uuid"]
            )
        ):
            raise PanelReview("panel_capacity_preservation_denied")
        self._identity(current, binding)
        return {}

    def _capacity_observed(self, result, target):
        if target["devices_limit"] is None:
            return "hwidDeviceLimit" in result and result["hwidDeviceLimit"] is None
        return super()._capacity_observed(result, target)

    def _request(self, method, path, body=None, missing=False):
        self.runtime_policy.require("panel_mutation" if method != "GET" else None)
        if (
            method == "POST"
            and path == "users"
            and (
                not isinstance(body, dict)
                or not isinstance(body.get("username"), str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{3,36}", body["username"])
            )
        ):
            raise PanelReview("panel_name_invalid")
        if method in {"POST", "PATCH"}:
            authorize = self._mutation_authority.get()
            if authorize is None:
                raise PanelReview("panel_mutation_authorization_missing")
            authorize()
        if method == "POST" and path == "users" and self._creation_contacts.get() is not None:
            body = body | {
                key: value for key, value in self._creation_contacts.get().items() if value is not None
            }
        result = RemnawaveTest._request(self, method, path, body, missing)
        self.runtime_policy.require()
        return result

    def apply(
        self, binding, target, *, before_mutation=None, create_contacts=None, select_candidate=None, **kwargs
    ):
        if not callable(before_mutation):
            raise PanelReview("panel_mutation_authorization_missing")
        if create_contacts is not None:
            from types import SimpleNamespace

            from .panel_contacts import profile_contacts

            if (
                not isinstance(create_contacts, dict)
                or profile_contacts(
                    SimpleNamespace(
                        telegram_id=create_contacts.get("telegramId"),
                        email=create_contacts.get("email"),
                        email_verified=True,
                    )
                )
                != create_contacts
            ):
                raise PanelReview("panel_contact_invalid")
        if kwargs.get("create") and kwargs.get("uncertain"):
            return self.recover_creation(binding, target)
        if kwargs.get("create") and select_candidate is not None:
            for _ in range(16):
                self.validate_binding(binding)
                try:
                    occupied = self._request("GET", "users/by-username/" + binding["username"], missing=True)
                except PanelUnavailable:
                    raise PanelUnavailable(create_not_started=True) from None
                if occupied is None:
                    break
                if (
                    not isinstance(occupied, dict)
                    or occupied.get("username") != binding["username"]
                    or numeric_id(occupied.get("id")) == binding.get("user_id")
                    or not isinstance(occupied.get("shortUuid"), str)
                    or occupied["shortUuid"] == binding["short_uuid"]
                ):
                    raise PanelReview("panel_identity_collision")
                selected = select_candidate(dict(binding), occupied)
                if (
                    selected.get("short_uuid") != binding["short_uuid"]
                    or selected.get("origin_nonce") != binding["origin_nonce"]
                ):
                    raise PanelReview("panel_creation_scope_changed")
                binding = selected
            else:
                raise PanelUnavailable(create_not_started=True)

        def authorize():
            before_mutation()
            self.validate_binding(binding)
            if target.get("devices_limit") is None:
                evidence = self.capacity_evidence.get(binding["origin_nonce"])
                if (
                    not explicit_null_evidence(evidence)
                    or evidence != target.get("capacity_evidence")
                    or instant(evidence["until"]) <= datetime.now(UTC)
                ):
                    raise PanelReview("panel_capacity_preservation_denied")

        token = self._mutation_authority.set(authorize)
        contacts_token = self._creation_contacts.set(create_contacts)
        try:
            return super().apply(binding, target, **kwargs)
        finally:
            self._mutation_authority.reset(token)
            self._creation_contacts.reset(contacts_token)

    def recover_creation(self, binding, target=None):
        self.validate_binding(binding)
        current = self._request("GET", "users/by-short-uuid/" + binding["short_uuid"], missing=True)
        if current is None:
            raise PanelReview("panel_creation_unresolved")
        user_id = self._identity(current, binding)
        current = self._request("GET", "users/" + user_id)
        self._identity(current, binding)
        result = self._connection(current, binding)
        if target is not None:
            self.validate_target(target)
            if (
                instant(current.get("expireAt")) != instant(panel_expiry(target["expires_at"]))
                or not self._capacity_observed(current, target)
                or snapshot_squads(current) != [target["internal_squad_uuid"]]
                or current.get("status") != target.get("panel_status", "ACTIVE")
                or target.get("external_squad_uuid") is not None
                and current.get("externalSquadUuid") != target["external_squad_uuid"]
            ):
                raise PanelReview("panel_creation_target_unverified")
            result["applied_target"] = {
                "status": current["status"],
                "expires_at": instant(current["expireAt"]).isoformat(),
                "devices_limit": current["hwidDeviceLimit"],
                "internal_squad_uuids": snapshot_squads(current),
            }
        return result

    def sync_contacts(self, binding, contacts, *, before_patch, before_start=None):
        from types import SimpleNamespace

        from .panel_contacts import profile_contacts

        self.validate_binding(binding)
        if (
            not callable(before_patch)
            or binding.get("user_id") is None
            or not isinstance(contacts, dict)
            or set(contacts) - {"email", "telegramId"}
            or "telegramId" not in contacts
            or profile_contacts(
                SimpleNamespace(
                    telegram_id=contacts["telegramId"], email=contacts.get("email"), email_verified=True
                )
            )
            != contacts
        ):
            raise PanelReview("panel_contact_invalid")
        current = self._request("GET", "users/" + binding["user_id"], missing=True)
        self._identity(current, binding)
        self._connection(current, binding)
        if all(name in current and current[name] == value for name, value in contacts.items()):
            before_patch()
            return current

        def authorize():
            before_patch()
            if before_start is not None:
                before_start(current)

        token = self._mutation_authority.set(authorize)
        try:
            self._request("PATCH", "users", {"id": int(binding["user_id"])} | contacts)
        finally:
            self._mutation_authority.reset(token)
        result = self._request("GET", "users/" + binding["user_id"])
        self._identity(result, binding)
        self._connection(result, binding)
        if any(name not in result or result[name] != value for name, value in contacts.items()):
            raise PanelUnavailable("panel_contacts_not_observed")
        return result

    def delete_user(self, binding, *, before_delete):
        def authorize():
            self.runtime_policy.require("panel_mutation")
            self.runtime_policy.require("retention")
            before_delete()

        return super().delete_user(binding, before_delete=authorize)

    def revoke(self, binding, hwid, *, before_delete=None, **kwargs):
        if not callable(before_delete):
            raise PanelReview("panel_mutation_authorization_missing")
        token = self._mutation_authority.set(before_delete)
        try:
            return super().revoke(binding, hwid, before_delete=before_delete, **kwargs)
        finally:
            self._mutation_authority.reset(token)

    def rotate(self, binding, *, before_rotate=None, **kwargs):
        if not callable(before_rotate):
            raise PanelReview("panel_mutation_authorization_missing")
        proof = None

        def capture(current):
            nonlocal proof
            before_rotate(current)
            proof = dict(current)

        def authorize():
            if proof is None:
                raise PanelReview("panel_rotation_proof_required")
            before_rotate(proof)

        token = self._mutation_authority.set(authorize)
        try:
            return super().rotate(binding, before_rotate=capture, **kwargs)
        finally:
            self._mutation_authority.reset(token)

    def _identity(self, user, binding, *, rotated=False):
        result = super()._identity(user, binding, rotated=rotated)
        if binding.get("uuid") is not None and user.get("uuid") not in {None, binding["uuid"]}:
            raise PanelReview("panel_identity_mismatch")
        return result

    def _connection(self, user, binding, *, rotated=False):
        return super()._connection(user, binding, rotated=rotated) | {"uuid": binding.get("uuid")}
