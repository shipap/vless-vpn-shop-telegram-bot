"""Ограниченный UUID-контракт Remnawave 2.8.1; новые команды только собственным тестовым объектам."""

import hashlib
import re
from datetime import datetime, timedelta
from urllib.parse import urlparse
from uuid import UUID, uuid4

import httpx


def panel_expiry(value):
    """Абсолютная цель панели с округлением вверх до миллисекунды; оплаченный срок не меняется."""
    try:
        expiry = datetime.fromisoformat(value)
        if expiry.tzinfo is None:
            raise ValueError("panel_expiry_invalid")
        remainder = expiry.microsecond % 1000
        if not remainder:
            return value
        return (expiry + timedelta(microseconds=1000 - remainder)).isoformat(timespec="milliseconds")
    except (TypeError, ValueError, OverflowError):
        raise ValueError("panel_expiry_invalid") from None


class PanelUnavailable(Exception):
    def __init__(self, message="panel_unavailable", *, create_not_started=False):
        super().__init__(message)
        self.create_not_started = create_not_started is True


class PanelReview(Exception):
    pass


class PanelMissing(PanelReview):
    """A GET returned 404; callers must separately verify panel authority."""


class PanelUsernameConflict(PanelReview):
    """The panel definitively rejected POST /users because its username already exists."""


def snapshot_device_limit(user):
    """0 disables HWID enforcement; null delegates to an unverified panel fallback."""
    if "hwidDeviceLimit" not in user:
        raise PanelReview("panel_response_unverified")
    value = user["hwidDeviceLimit"]
    if value is not None and (type(value) is not int or not 0 <= value <= 9007199254740991):
        raise PanelReview("panel_response_unverified")
    return value


def snapshot_squads(user):
    squads = user.get("activeInternalSquads")
    if not isinstance(squads, list):
        raise PanelReview("panel_response_unverified")
    result = []
    for item in squads:
        if not isinstance(item, dict) or not isinstance(item.get("uuid"), str):
            raise PanelReview("panel_response_unverified")
        try:
            value = str(UUID(item["uuid"]))
        except ValueError as failure:
            raise PanelReview("panel_response_unverified") from failure
        if value != item["uuid"].lower() or value in result:
            raise PanelReview("panel_response_unverified")
        result.append(value)
    return sorted(result)


def registered_uuid_owner(session, value, sub, account, panel):
    """Неизменяемое происхождение новой UUID-привязки и подтверждённый canonical merge."""
    from sqlalchemy import select

    from .db import AccountIdentityOperation, Alias, LegacyIssue

    original = value.get("account_id")
    if (
        not account
        or not isinstance(original, str)
        or not re.fullmatch(r"[1-9][0-9]{0,18}", original)
        or int(original) >= 2**63
        or value.get("telegram_id") not in panel.auto_test_telegram_ids
        or value.get("origin") != "new_test_identity"
        or value.get("contract") != "2.8_uuid"
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
    alias = session.get(Alias, int(original)) if original != str(account.id) else None
    if original != str(account.id) and not (alias and alias.canonical_id == account.id):
        return False
    if str(account.telegram_id) == value["telegram_id"]:
        return True
    # Самовольное изменение Telegram не подтверждает передачу. Разрешённая отвязка
    # имеет завершённую server-owned операцию и сохраняет проверенную почту владельца.
    return bool(
        account.email
        and account.email_verified
        and session.scalar(
            select(AccountIdentityOperation.id)
            .where(
                AccountIdentityOperation.account_id == account.id,
                AccountIdentityOperation.kind.in_(["telegram_unlink", "owner_unlink_request"]),
                AccountIdentityOperation.state == "completed",
                AccountIdentityOperation.completed_at.is_not(None),
                AccountIdentityOperation.old_telegram_id == int(value["telegram_id"]),
            )
            .limit(1)
        )
    )


class RemnawaveTest:
    contract = "2.8_uuid"

    def __init__(
        self,
        *,
        base_url,
        token,
        contract=None,
        authorized=False,
        allowed_hosts=(),
        allowed_uuids=(),
        allowed_short_uuids=(),
        allowed_usernames=(),
        allowed_squads=(),
        allowed_external_squads=(),
        allowed_hwids=(),
        allowed_nodes=(),
        bindings=None,
        transport=None,
        allowed_subscription_hosts=(),
        auto_test_telegram_ids=(),
    ):
        url = urlparse(base_url)
        if (
            not authorized
            or contract != "2.8_uuid"
            or not token
            or "\n" in token
            or "\r" in token
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
        self.uuids = set(allowed_uuids)
        self.shorts = set(allowed_short_uuids)
        self.usernames = set(allowed_usernames)
        self.squads = set(allowed_squads)
        self.external_squads = set(allowed_external_squads)
        self.hwids = set(allowed_hwids)
        self.nodes = set(allowed_nodes)
        self.device_bindings = set()
        self.bindings = dict(bindings or {})
        self.api_host = url.hostname
        self.subscription_hosts = set(allowed_subscription_hosts)
        self.auto_test_telegram_ids = {str(value) for value in auto_test_telegram_ids}
        if any(not re.fullmatch(r"[1-9][0-9]{0,18}", value) for value in self.auto_test_telegram_ids):
            raise ValueError("panel_test_owner_denied")
        self.auto_bindings = {}
        self.auto_connection_urls = {}
        self.auto_scope = hashlib.sha256(
            (url.hostname + "|" + ",".join(sorted(self.auto_test_telegram_ids))).encode()
        ).hexdigest()[:32]
        if (
            (not self.bindings and not self.auto_test_telegram_ids)
            or not self.squads
            or (not self.subscription_hosts and not self.auto_test_telegram_ids)
            or "*" in self.subscription_hosts
        ):
            raise ValueError("panel_allowlist_required")
        for binding in self.bindings.values():
            self.validate_binding(binding)
        for value in self.uuids | self.squads | self.external_squads | self.nodes:
            if str(UUID(value)) != value:
                raise ValueError("panel_uuid_invalid")
        self.client = httpx.Client(
            base_url=base_url.rstrip("/") + "/api/",
            transport=transport,
            headers={"Authorization": "Bearer " + token},
            timeout=10,
            follow_redirects=False,
            trust_env=False,
        )

    def validate_binding(self, value):
        automatic = self.auto_bindings.get(value.get("uuid")) == value
        if (
            (not automatic and value.get("uuid") not in self.uuids)
            or (not automatic and value.get("short_uuid") not in self.shorts)
            or (not automatic and value.get("username") not in self.usernames)
            or not value.get("username", "").startswith("sunrise_test_")
            or not re.fullmatch(r"[A-Za-z0-9_-]{3,36}", value.get("username", ""))
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", value.get("short_uuid", ""))
        ):
            raise ValueError("panel_identity_denied")

    def register_test_binding(self, binding, *, telegram_id, connection_url=None):
        """Регистрация только после сохранения привязки и проверки владельца в Provisioning."""
        if str(telegram_id) not in self.auto_test_telegram_ids:
            raise ValueError("panel_test_owner_denied")
        try:
            identity = UUID(binding["uuid"])
            valid = (
                str(identity) == binding["uuid"]
                and identity.version == 4
                and binding["uuid"] not in self.uuids | self.squads | self.external_squads
                and binding["username"] == "sunrise_test_" + identity.hex[:20]
                and re.fullmatch(r"[a-f0-9]{24}", binding["short_uuid"])
            )
        except (KeyError, ValueError, TypeError, AttributeError):
            valid = False
        if not valid:
            raise ValueError("panel_identity_denied")
        if connection_url is not None and not self._safe_connection_url(connection_url, automatic=True):
            raise ValueError("panel_connection_denied")
        self.auto_bindings[binding["uuid"]] = dict(binding)
        self.auto_connection_urls[binding["uuid"]] = connection_url

    def _safe_connection_url(self, link, *, automatic=False):
        if not isinstance(link, str) or not link:
            return False
        try:
            url = urlparse(link)
            return (
                url.scheme == "https"
                and bool(url.hostname)
                and (automatic or url.hostname in self.subscription_hosts)
                and "*" not in url.hostname
                and not url.username
                and not url.password
                and url.port in {None, 443}
                and not url.fragment
                and not url.query
                and len(link) <= 2048
                and not any(ord(x) <= 32 for x in link)
            )
        except ValueError:
            return False

    def validate_target(self, value):
        expiry = datetime.fromisoformat(panel_expiry(value["expires_at"]))
        if (
            expiry.tzinfo is None
            or not isinstance(value["devices_limit"], int)
            or isinstance(value["devices_limit"], bool)
            or not 1 <= value["devices_limit"] <= 100
            or value.get("internal_squad_uuid") not in self.squads
            or (
                value.get("external_squad_uuid") is not None
                and value.get("external_squad_uuid") not in self.external_squads
            )
            or value.get("panel_status", "ACTIVE") not in {"ACTIVE", "DISABLED"}
        ):
            raise ValueError("panel_target_denied")

    def _request(self, method, path, body=None, missing=False):
        try:
            with self.client.stream(method, path, json=body) as response:
                if response.status_code == 404 and missing:
                    return None
                if response.status_code == 404 and method == "GET":
                    raise PanelMissing("panel_user_missing")
                if response.status_code >= 500 or response.status_code == 429:
                    raise PanelUnavailable("panel_unavailable")
                if response.status_code == 400 and method == "POST" and path == "users":
                    import json

                    chunks = bytearray()
                    for chunk in response.iter_bytes(chunk_size=4096):
                        if len(chunks) + len(chunk) > 4096:
                            raise PanelReview("panel_response_too_large")
                        chunks.extend(chunk)
                    error = json.loads(chunks)
                    if (
                        isinstance(error, dict)
                        and error.get("path") == "/api/users"
                        and error.get("errorCode") == "A019"
                        and error.get("message") == "User username already exists"
                        and isinstance(error.get("timestamp"), str)
                    ):
                        raise PanelUsernameConflict("panel_username_conflict")
                    raise PanelReview("panel_response_unverified")
                if response.status_code not in {200, 201}:
                    raise PanelReview("panel_response_unverified")
                chunks = bytearray()
                for chunk in response.iter_bytes(chunk_size=16384):
                    if len(chunks) + len(chunk) > 65536:
                        raise PanelReview("panel_response_too_large")
                    chunks.extend(chunk)
                import json

                envelope = json.loads(chunks)
                if not isinstance(envelope, dict):
                    raise PanelReview("panel_response_unverified")
                result = envelope.get("response")
                if not isinstance(result, dict) and not (
                    method == "GET" and path == "hosts" and type(result) is list
                ):
                    raise PanelReview("panel_response_unverified")
                return result
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            if isinstance(exc, httpx.HTTPError):
                raise PanelUnavailable("panel_unavailable") from None
            raise PanelReview("panel_response_unverified") from None

    def _identity(self, user, binding):
        if (
            not user
            or user.get("uuid") != binding["uuid"]
            or user.get("shortUuid") != binding["short_uuid"]
            or user.get("username") != binding["username"]
        ):
            raise PanelReview("panel_identity_mismatch")

    def _cleanup_binding(self, binding):
        """Удаление допустимо только для отдельно зарегистрированной новой тестовой личности."""
        try:
            self.validate_binding(binding)
            registered = (
                self.auto_bindings.get(binding.get("uuid")) == binding
                if self.contract == "2.8_uuid"
                else self.registered.get(binding.get("origin_nonce")) == binding
                and isinstance(binding.get("user_id"), str)
            )
            url = self.client.base_url
            if (
                not registered
                or url.host != self.api_host
                or url.scheme != "https"
                or url.port not in {None, 443}
                or url.path != "/api/"
                or url.username
                or url.password
                or url.query
                or url.fragment
            ):
                raise ValueError
        except (ValueError, KeyError, TypeError):
            raise PanelReview("panel_cleanup_scope_denied") from None

    def delete_user(self, binding, *, before_delete):
        self._cleanup_binding(binding)
        frozen = dict(binding)
        scope = (self.api_host, self.client.base_url)
        before_delete()
        self._cleanup_binding(binding)
        if binding != frozen or scope != (self.api_host, self.client.base_url):
            raise PanelReview("panel_cleanup_scope_denied")
        result = self._request("DELETE", "users/" + frozen["uuid"])
        if result.get("isDeleted") is not True:
            raise PanelReview("panel_delete_unverified")

    def cleanup_snapshot(self, binding):
        from .panel_v3 import instant

        self._cleanup_binding(binding)
        identity = binding["uuid"] if self.contract == "2.8_uuid" else binding["user_id"]
        user = self._request("GET", "users/" + identity, missing=True)
        if user is None:
            return None
        self._identity(user, binding)
        squads = snapshot_squads(user)
        if (
            user.get("status") not in {"ACTIVE", "EXPIRED", "DISABLED", "LIMITED"}
            or not set(squads) <= self.squads
        ):
            raise PanelReview("panel_cleanup_scope_denied")
        return {"expires_at": instant(user.get("expireAt")).isoformat(), "status": user["status"]}

    def apply(self, binding, target, *, create=False, uncertain=False):
        self.validate_binding(binding)
        self.validate_target(target)
        automatic = self.auto_bindings.get(binding["uuid"]) == binding
        if target.get("project_prefix") and not binding["username"].startswith(target["project_prefix"]):
            raise ValueError("panel_project_prefix_denied")
        path = "users/by-username/" + binding["username"] if create else "users/" + binding["uuid"]
        try:
            current = self._request("GET", path, missing=True)
        except PanelUnavailable:
            # Только свежая попытка доказуемо остановилась до любого POST/PATCH.
            # Неопределённость предыдущего запуска этим чтением не снимается.
            if create is True and uncertain is False:
                raise PanelUnavailable("panel_unavailable", create_not_started=True) from None
            raise
        body = {
            "uuid": binding["uuid"],
            "expireAt": panel_expiry(target["expires_at"]),
            "hwidDeviceLimit": target["devices_limit"],
            "activeInternalSquads": [target["internal_squad_uuid"]],
            "status": target.get("panel_status", "ACTIVE"),
        }
        if target.get("external_squad_uuid") is not None:
            body["externalSquadUuid"] = target["external_squad_uuid"]
        if current is None:
            if not create or uncertain:
                raise PanelReview("panel_creation_unresolved" if uncertain else "panel_identity_missing")
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
            self._identity(created, binding)
        else:
            self._identity(current, binding)
            if automatic and create and not uncertain:
                raise PanelReview("panel_identity_collision")
            self._request("PATCH", "users", body)
        result = self._request("GET", "users/" + binding["uuid"], missing=True)
        self._identity(result, binding)
        observed_squads = result.get("activeInternalSquads")
        if (
            not isinstance(observed_squads, list)
            or any(
                not isinstance(item, dict) or not isinstance(item.get("uuid"), str)
                for item in observed_squads
            )
            or not isinstance(result.get("expireAt"), str)
            or type(result.get("hwidDeviceLimit")) is not int
        ):
            raise PanelReview("panel_response_unverified")
        squads = {item["uuid"] for item in observed_squads}
        try:
            expiry_matches = datetime.fromisoformat(
                result["expireAt"].replace("Z", "+00:00")
            ) == datetime.fromisoformat(body["expireAt"])
        except (KeyError, TypeError, ValueError):
            expiry_matches = False
        if (
            not expiry_matches
            or result.get("hwidDeviceLimit") != target["devices_limit"]
            or squads != {target["internal_squad_uuid"]}
            or result.get("status") != body["status"]
            or ("externalSquadUuid" in body and result.get("externalSquadUuid") != body["externalSquadUuid"])
        ):
            raise PanelUnavailable("panel_target_not_observed")
        link = result.get("subscriptionUrl")
        if not isinstance(link, str) or not link:
            raise PanelUnavailable("panel_connection_unavailable")
        if not self._safe_connection_url(link, automatic=automatic):
            raise PanelReview("panel_connection_denied")
        pinned_link = self.auto_connection_urls.get(binding["uuid"])
        if automatic and pinned_link is not None and link != pinned_link:
            raise PanelReview("panel_connection_changed")
        return {"uuid": binding["uuid"], "short_uuid": binding["short_uuid"], "connection_url": link}

    def revoke(self, binding, hwid, *, uncertain=False, before_delete=None):
        self.validate_binding(binding)
        if self.auto_bindings.get(binding["uuid"]) == binding:
            if (binding["uuid"], hwid) not in self.device_bindings:
                raise ValueError("panel_device_denied")
            user = self._request("GET", "users/" + binding["uuid"])
            self._identity(user, binding)
            if not any(item["hwid"] == hwid for item in self._inventory(binding, user)):
                return bool(uncertain)
            if uncertain:
                raise PanelReview("panel_device_unresolved")
            if before_delete is not None:
                before_delete()
            self._request("POST", "hwid/devices/delete", {"userUuid": binding["uuid"], "hwid": hwid})
            user = self._request("GET", "users/" + binding["uuid"])
            self._identity(user, binding)
            if any(item["hwid"] == hwid for item in self._inventory(binding, user)):
                raise PanelUnavailable("panel_device_not_observed")
            return True
        if hwid not in self.hwids:
            raise ValueError("panel_device_denied")
        self._identity(self._request("GET", "users/" + binding["uuid"]), binding)
        result = self._request("GET", "hwid/devices/" + binding["uuid"])
        devices = result.get("devices")
        if not isinstance(devices, list) or any(
            not isinstance(item, dict) or not isinstance(item.get("hwid"), str) for item in devices
        ):
            raise PanelReview("panel_response_unverified")
        if not any(item["hwid"] == hwid for item in devices):
            return bool(uncertain)
        if uncertain:
            raise PanelReview("panel_device_unresolved")
        if before_delete is not None:
            before_delete()
        self._request("POST", "hwid/devices/delete", {"userUuid": binding["uuid"], "hwid": hwid})
        result = self._request("GET", "hwid/devices/" + binding["uuid"])
        devices = result.get("devices")
        if not isinstance(devices, list) or any(
            not isinstance(item, dict) or not isinstance(item.get("hwid"), str) for item in devices
        ):
            raise PanelReview("panel_response_unverified")
        if any(item["hwid"] == hwid for item in devices):
            raise PanelUnavailable("panel_device_not_observed")
        return True

    def _new_binding(self, binding):
        self.validate_binding(binding)
        if self.auto_bindings.get(binding["uuid"]) != binding:
            raise ValueError("panel_test_origin_denied")

    def _inventory(self, binding, user):
        from .panel_v3 import instant, numeric_id, safe_text

        owner_id = numeric_id(user.get("id"))
        result = self._request("GET", "hwid/devices/" + binding["uuid"])
        devices = result.get("devices")
        if (
            not isinstance(devices, list)
            or len(devices) > 1000
            or type(result.get("total")) is not int
            or result["total"] != len(devices)
        ):
            raise PanelReview("panel_response_unverified")
        safe, seen = [], set()
        for item in devices:
            if not isinstance(item, dict):
                raise PanelReview("panel_device_denied")
            hwid = item.get("hwid")
            if (
                numeric_id(item.get("userId")) != owner_id
                or not isinstance(hwid, str)
                or not 1 <= len(hwid) <= 512
                or any(ord(c) <= 32 for c in hwid)
                or hwid in seen
            ):
                raise PanelReview("panel_device_denied")
            seen.add(hwid)
            safe.append(
                {
                    "hwid": hwid,
                    "name": safe_text(item.get("deviceModel"), 200)
                    or safe_text(item.get("platform"), 100)
                    or "Устройство",
                    "platform": safe_text(item.get("platform"), 100),
                    "created_at": instant(item.get("createdAt")).isoformat(),
                    "updated_at": instant(item.get("updatedAt")).isoformat(),
                }
            )
        return safe

    def _connection(self, user, binding, expected_short=None):
        self._identity(user, binding | {"short_uuid": expected_short or binding["short_uuid"]})
        link = user.get("subscriptionUrl")
        if not self._safe_connection_url(link, automatic=True):
            raise PanelReview("panel_connection_denied")
        pinned = self.auto_connection_urls.get(binding["uuid"])
        if expected_short and expected_short != binding["short_uuid"]:
            if not pinned or not pinned.endswith("/" + binding["short_uuid"]):
                raise PanelReview("panel_connection_changed")
            pinned = pinned.rsplit("/", 1)[0] + "/" + expected_short
        if pinned is not None and link != pinned:
            raise PanelReview("panel_connection_changed")
        return {
            "uuid": binding["uuid"],
            "user_id": binding["uuid"],
            "short_uuid": user["shortUuid"],
            "connection_url": link,
        }

    def snapshot(self, binding):
        from .panel_v3 import instant

        self._new_binding(binding)
        user = self._request("GET", "users/" + binding["uuid"])
        result = self._connection(user, binding)
        traffic = user.get("userTraffic")
        used = traffic.get("usedTrafficBytes") if isinstance(traffic, dict) else None
        limit = user.get("trafficLimitBytes")
        if any(
            x is not None and (type(x) is not int or not 0 <= x <= 9007199254740991) for x in (used, limit)
        ) or user.get("status") not in {"ACTIVE", "DISABLED", "LIMITED", "EXPIRED"}:
            raise PanelReview("panel_response_unverified")
        return result | {
            "status": user["status"],
            "traffic_bytes": used,
            "traffic_limit_bytes": limit,
            "devices_limit": snapshot_device_limit(user),
            "internal_squad_uuids": snapshot_squads(user),
            "devices": self._inventory(binding, user),
            "expires_at": instant(user.get("expireAt")).isoformat(),
            "sub_revoked_at": instant(user["subRevokedAt"]).isoformat() if user.get("subRevokedAt") else None,
        }

    def register_device(self, binding, hwid):
        self._new_binding(binding)
        if not isinstance(hwid, str) or not 1 <= len(hwid) <= 512 or any(ord(c) <= 32 for c in hwid):
            raise ValueError("panel_device_denied")
        self.device_bindings.add((binding["uuid"], hwid))

    def rotate(
        self,
        binding,
        *,
        revoke_only_passwords=False,
        uncertain=False,
        before_short_uuid=None,
        before_sub_revoked_at=None,
        before_expires_at=None,
        expected_short_uuid=None,
        before_rotate=None,
    ):
        from .panel_v3 import instant

        self._new_binding(binding)
        if type(revoke_only_passwords) is not bool:
            raise ValueError("panel_target_denied")
        if uncertain and (
            before_short_uuid != binding["short_uuid"]
            or before_expires_at is None
            or not revoke_only_passwords
            and not isinstance(expected_short_uuid, str)
        ):
            raise PanelReview("panel_rotation_unresolved")
        next_short = (
            binding["short_uuid"]
            if revoke_only_passwords
            else expected_short_uuid
            if uncertain
            else uuid4().hex[:24]
        )
        if not revoke_only_passwords and not re.fullmatch(r"[a-f0-9]{24}", next_short):
            raise PanelReview("panel_rotation_unresolved")
        user = self._request("GET", "users/" + binding["uuid"])
        if uncertain:
            result = self._connection(user, binding, next_short)
            if instant(user.get("expireAt")) != instant(before_expires_at):
                raise PanelReview("panel_rotation_rights_changed")
        else:
            self._connection(user, binding)
            verified_expiry = instant(user.get("expireAt"))
            if not callable(before_rotate):
                raise ValueError("panel_rotation_proof_required")
            before_rotate(
                {
                    "before_short_uuid": binding["short_uuid"],
                    "before_sub_revoked_at": user.get("subRevokedAt"),
                    "before_expires_at": user.get("expireAt"),
                    "next_short_uuid": next_short,
                }
            )
            self._request(
                "POST",
                "users/" + binding["uuid"] + "/actions/revoke",
                {"revokeOnlyPasswords": revoke_only_passwords}
                | ({} if revoke_only_passwords else {"shortUuid": next_short}),
            )
            result_user = self._request("GET", "users/" + binding["uuid"])
            result = self._connection(result_user, binding, next_short)
            if instant(result_user.get("expireAt")) != verified_expiry:
                raise PanelReview("panel_rotation_rights_changed")
            before_sub_revoked_at, user = user.get("subRevokedAt"), result_user
        if revoke_only_passwords and (
            not user.get("subRevokedAt")
            or instant(user["subRevokedAt"])
            == (instant(before_sub_revoked_at) if before_sub_revoked_at else None)
        ):
            raise PanelReview("panel_rotation_unresolved")
        return result

    def node_snapshot(self, node_uuid):
        from .panel_v3 import safe_text

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
            "country_code": safe_text(node.get("countryCode"), 8),
            "status": "DISABLED"
            if node["isDisabled"]
            else "CONNECTED"
            if node["isConnected"]
            else "DISCONNECTED",
        }


def configured_panel(config, transport=None):
    from .settings import runtime_policy

    policy = runtime_policy(config)
    if policy and policy.production:
        from .panel_v3 import RemnawaveProduction

        return RemnawaveProduction(runtime_policy=policy, token=config.panel_token, transport=transport)
    if not config.panel_test_authorized:
        return None
    if transport is None and getattr(config, "panel_tunnel_port", None):
        from .panel_tunnel import PanelTunnelTransport

        hostname = urlparse(config.panel_base_url).hostname
        if hostname not in config.panel_allowed_hosts:
            raise ValueError("panel_configuration_denied")
        transport = PanelTunnelTransport(hostname, config.panel_tunnel_port)
    if config.panel_contract == "3.4_id":
        from .panel_v3 import RemnawaveV3

        panel = RemnawaveV3(
            base_url=config.panel_base_url,
            token=config.panel_token,
            authorized=True,
            allowed_hosts=config.panel_allowed_hosts,
            allowed_squads=config.panel_test_squads,
            allowed_external_squads=getattr(config, "panel_test_external_squads", []),
            allowed_subscription_hosts=config.panel_subscription_allowed_hosts,
            allowed_user_ids=getattr(config, "panel_test_user_ids", []),
            allowed_nodes=getattr(config, "panel_test_node_uuids", []),
            bindings=config.panel_test_bindings,
            transport=transport,
            auto_test_telegram_ids={
                value.strip()
                for value in getattr(config, "test_telegram_recipients", "").split(",")
                if value.strip()
            },
        )
        if policy and policy.scoped_payments:
            panel.scoped_runtime_policy = policy
        return panel
    return RemnawaveTest(
        base_url=config.panel_base_url,
        token=config.panel_token,
        contract=config.panel_contract,
        authorized=True,
        allowed_hosts=config.panel_allowed_hosts,
        allowed_uuids=config.panel_test_uuids,
        allowed_short_uuids=config.panel_test_short_uuids,
        allowed_usernames=config.panel_test_usernames,
        allowed_squads=config.panel_test_squads,
        allowed_external_squads=getattr(config, "panel_test_external_squads", []),
        allowed_hwids=config.panel_test_hwids,
        allowed_nodes=getattr(config, "panel_test_node_uuids", []),
        bindings=config.panel_test_bindings,
        transport=transport,
        allowed_subscription_hosts=config.panel_subscription_allowed_hosts,
        auto_test_telegram_ids={
            value.strip()
            for value in getattr(config, "test_telegram_recipients", "").split(",")
            if value.strip()
        },
    )
