"""Собственный inventory и подтверждённые команды через общий durable worker."""

import hashlib
import re
from types import SimpleNamespace

from sqlalchemy import select
from sunrise_core.errors import DomainError, missing

from .db import Device, Job, PanelObservation, Setting, uid
from .moderation import DeviceModeration


class CustomerOperations:
    def __init__(self, commerce, panel_events=None):
        self.commerce, self.db, self.panel_events = commerce, commerce.db, panel_events

    def _scope(self):
        return (
            {"scope_id": self.commerce.runtime_policy.scope.scope_id}
            if getattr(self.commerce, "live", False) or getattr(self.commerce, "scoped_payments", False)
            else {}
        )

    def _before_queue(self, session, account, sub):
        self._guards(session, account, sub)
        if getattr(self.commerce, "live", False):
            self.commerce.runtime_policy.require_current(session, "panel_mutation")
        if getattr(self.commerce, "scoped_payments", False):
            self.commerce.runtime_policy.require_current(session, "panel_mutation")
            self.commerce.runtime_policy.require_subscription_in(session, sub)

    def inventory(self, account_id, sid, refresh=False):
        with self.db.transaction() as session:
            account = self.commerce._account(session, account_id)
            sub = self.commerce._sub(session, account.id, sid)
            from .retention import assert_mutation_allowed_in

            assert_mutation_allowed_in(session, sub, include_legacy=False)
            if (sub.panel_user_id is not None or sub.panel_uuid is not None) and getattr(
                self.commerce.panel_provider, "contract", None
            ) in {"3.4_id", "2.8_uuid"}:
                self._binding(session, account, sub)
        if refresh and self.panel_events:
            self.panel_events.reconcile_owned(sid)
        with self.db.transaction() as session:
            account = self.commerce._account(session, account_id)
            sub = self.commerce._sub(session, account.id, sid)
            assert_mutation_allowed_in(session, sub, include_legacy=False)
            observation = session.scalar(
                select(PanelObservation).where(
                    PanelObservation.subscription_id == sid,
                    PanelObservation.kind == "user",
                    PanelObservation.resource_id
                    == (
                        str(sub.panel_user_id)
                        if getattr(self.commerce.panel_provider, "contract", None) == "3.4_id"
                        else sub.panel_uuid
                    ),
                )
            )
            devices = list(
                session.scalars(select(Device).where(Device.subscription_id == sid).order_by(Device.id))
            )
            return {
                "subscription_id": sid,
                "source": "remnawave"
                if observation
                else "local"
                if self.commerce.local_test and not self.commerce.panel_provider
                else "unavailable",
                "status": observation.status if observation else None,
                "observed_at": observation.observed_at.isoformat() if observation else None,
                "error": observation.last_error
                if observation
                else "panel_snapshot_unavailable"
                if self.commerce.panel_provider
                else None,
                "traffic_bytes": str(observation.traffic_bytes)
                if observation and observation.traffic_bytes is not None
                else None,
                "traffic_limit_bytes": str(observation.traffic_limit_bytes)
                if observation and observation.traffic_limit_bytes is not None
                else None,
                "paid_expires_at": sub.expires_at.isoformat() if sub.expires_at else None,
                "devices": [
                    {
                        "id": device.id,
                        "name": device.name,
                        "platform": device.platform,
                        "generation": device.generation,
                        "revoked": device.revoked,
                        "observed_at": device.observed_at.isoformat() if device.observed_at else None,
                    }
                    for device in devices
                ],
            }

    @staticmethod
    def _validate(idempotency_key, confirmed):
        if confirmed is not True:
            raise DomainError("confirmation_required", "Подтвердите действие.", 400)
        if not isinstance(idempotency_key, str) or not re.fullmatch(
            r"[A-Za-z0-9_.:-]{1,128}", idempotency_key
        ):
            raise DomainError("idempotency_key_invalid", "Проверьте ключ операции.", 400)

    def _prior(self, session, account, key, request):
        digest = hashlib.sha256(key.encode()).hexdigest()[:40]
        marker = f"customer_op:{account.id}:{digest}"
        record = session.get(Setting, marker)
        saved = record.value.get("request") if record else None
        if (
            record
            and request.get("kind") == "device_revoke"
            and isinstance(saved, dict)
            and "unlink_mode" not in saved
            and saved == {name: value for name, value in request.items() if name != "unlink_mode"}
            and self._legacy_unlink_mode_matches(session, account, record.value.get("result"), request)
        ):
            saved = {**saved, "unlink_mode": request["unlink_mode"]}
        if record and saved != request:
            raise DomainError("idempotency_conflict", "Ключ использован для другого действия.", 409)
        return marker, record.value["result"] if record else None

    @staticmethod
    def _legacy_unlink_mode_matches(session, account, result, request):
        if not isinstance(result, dict) or result.get("device_ids") != request["device_ids"]:
            return False
        ids = result.get("job_ids")
        if not isinstance(ids, list):
            return False
        if not ids:
            return result.get("status") == "revoked"
        for jid in ids:
            job = session.get(Job, jid)
            payload = job.payload if job else {}
            did, generation = payload.get("device_id"), payload.get("generation")
            if (
                not job
                or job.kind != "device_revoke"
                or job.account_id != account.id
                or payload.get("subscription_id") != request["subscription_id"]
                or did not in request["device_ids"]
                or type(generation) is not int
                or generation <= 0
                or job.key != f"device-revoke:{did}:{generation}"
                or payload.get("unlink_mode") != request["unlink_mode"]
                or ("expected_generations" in request and request["expected_generations"][did] != generation)
            ):
                return False
        return True

    def _guards(self, session, account, sub):
        self.commerce._assert_new_operations(session)
        self.commerce.legal.require_current(session, account)
        self.commerce._assert_subscription_operations(session, sub)

    def _binding(self, session, account, sub):
        panel = self.commerce.panel_provider
        if (
            not panel
            or getattr(panel, "contract", None) not in {"3.4_id", "2.8_uuid"}
            or (sub.panel_user_id is None if panel.contract == "3.4_id" else sub.panel_uuid is None)
        ):
            raise DomainError(
                "panel_unavailable", "Операция недоступна без подтверждённой связи с панелью.", 503, True
            )
        try:
            binding = self.commerce.provisioning._auto_binding(
                session,
                SimpleNamespace(account_id=account.id, id=uid(), kind="customer_preflight"),
                sub,
                {"environment": sub.environment, **self._scope()},
            )
            panel.validate_binding(binding)
            return dict(binding)
        except ValueError:
            raise DomainError(
                "panel_identity_denied", "Связь с тестовой панелью требует проверки.", 503
            ) from None

    def revoke_devices(
        self,
        account_id,
        sid,
        device_ids,
        idempotency_key,
        confirmed=False,
        *,
        expected_generations=None,
        operation_mode=None,
    ):
        self._validate(idempotency_key, confirmed)
        if (
            not isinstance(device_ids, list)
            or not 1 <= len(device_ids) <= 100
            or any(not isinstance(value, str) or not 1 <= len(value) <= 64 for value in device_ids)
            or len(set(device_ids)) != len(device_ids)
        ):
            raise DomainError("devices_invalid", "Выберите от 1 до 100 разных устройств.", 400)
        requested = sorted(device_ids)
        if (
            operation_mode not in {None, "single", "bulk"}
            or operation_mode == "single"
            and len(requested) != 1
        ):
            raise DomainError("device_selection_invalid", "Проверьте тип выбранной отвязки.", 400)
        unlink_mode = operation_mode or ("bulk" if len(requested) > 1 else "single")
        request = {
            "kind": "device_revoke",
            "subscription_id": sid,
            "device_ids": requested,
            "unlink_mode": unlink_mode,
        }
        if expected_generations is not None:
            if (
                not isinstance(expected_generations, dict)
                or set(expected_generations) != set(requested)
                or any(type(value) is not int or value <= 0 for value in expected_generations.values())
            ):
                raise DomainError("device_selection_invalid", "Проверьте выбранные устройства.", 400)
            request["expected_generations"] = {did: expected_generations[did] for did in requested}
        with self.db.transaction() as session:
            account = self.commerce._account(session, account_id, True)
            sub = self.commerce._sub(session, account.id, sid, True)
            marker, prior = self._prior(session, account, idempotency_key, request)
            if prior:
                return prior
            DeviceModeration(self.commerce).assert_unlink_allowed(account)
            self._guards(session, account, sub)
            binding = self._binding(session, account, sub)
            devices = list(
                session.scalars(
                    select(Device)
                    .where(Device.subscription_id == sid, Device.id.in_(requested))
                    .order_by(Device.id)
                    .with_for_update()
                )
            )
            if len(devices) != len(requested):
                raise missing()
            if expected_generations is not None and any(
                device.generation != expected_generations[device.id] for device in devices
            ):
                raise DomainError(
                    "device_selection_changed",
                    "Список устройств изменился. Обновите его и подтвердите действие заново.",
                    409,
                )
            if any(not device.revoked and not device.panel_hwid for device in devices):
                raise DomainError("panel_device_denied", "Устройство не подтверждено панелью.", 503)
            self._before_queue(session, account, sub)
            jobs = []
            for device in devices:
                if device.revoked:
                    continue
                key = f"device-revoke:{device.id}:{device.generation}"
                job = session.scalar(select(Job).where(Job.key == key))
                if job is None:
                    job = Job(
                        key=key,
                        account_id=account.id,
                        kind="device_revoke",
                        payload={
                            "subscription_id": sid,
                            "device_id": device.id,
                            "device_name": device.name,
                            "generation": device.generation,
                            "hwid": device.panel_hwid,
                            "environment": sub.environment,
                            **self._scope(),
                            "panel_binding": binding,
                            "unlink_mode": unlink_mode,
                            "unlink_operation": marker,
                        },
                    )
                    session.add(job)
                    session.flush()
                elif job.account_id != account.id or job.payload.get("subscription_id") != sid:
                    raise DomainError("panel_identity_denied", "Операция требует проверки.", 409)
                jobs.append(job)
            status = (
                "review_required"
                if any(job.state in {"review", "blocked"} for job in jobs)
                else "revoke_pending"
                if jobs
                else "revoked"
            )
            result = {"status": status, "device_ids": requested, "job_ids": [job.id for job in jobs]}
            session.add(Setting(key=marker, value={"request": request, "result": result}))
            return result

    def rotate_credentials(
        self, account_id, sid, idempotency_key, confirmed=False, revoke_only_passwords=False
    ):
        self._validate(idempotency_key, confirmed)
        if type(revoke_only_passwords) is not bool:
            raise DomainError("rotation_invalid", "Проверьте действие обновления ссылки.", 400)
        request = {
            "kind": "credential_rotate",
            "subscription_id": sid,
            "revoke_only_passwords": revoke_only_passwords,
        }
        with self.db.transaction() as session:
            account = self.commerce._account(session, account_id, True)
            sub = self.commerce._sub(session, account.id, sid, True)
            marker, prior = self._prior(session, account, idempotency_key, request)
            if prior:
                return prior
            self._guards(session, account, sub)
            binding = self._binding(session, account, sub)
            active = session.scalar(
                select(Job.id).where(
                    Job.kind == "credential_rotate",
                    Job.payload["subscription_id"].as_string() == sid,
                    Job.state.in_(["pending", "in_flight", "review"]),
                )
            )
            if active:
                raise DomainError(
                    "credential_rotation_pending",
                    "Обновление ссылки уже ожидает завершения или проверки.",
                    409,
                )
            self._before_queue(session, account, sub)
            job = Job(
                key="credential-rotate:" + sid + ":" + marker.rsplit(":", 1)[1],
                account_id=account.id,
                kind="credential_rotate",
                payload={
                    "subscription_id": sid,
                    "environment": sub.environment,
                    **self._scope(),
                    "version": sub.version,
                    "revoke_only_passwords": revoke_only_passwords,
                    "panel_binding": binding,
                },
            )
            session.add(job)
            session.flush()
            result = {"status": "rotate_pending", "job_id": job.id}
            session.add(Setting(key=marker, value={"request": request, "result": result}))
            return result
