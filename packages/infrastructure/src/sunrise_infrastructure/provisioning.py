"""Восстанавливаемая выдача абсолютной сохранённой цели разрешённым тестовым объектам."""

from datetime import timedelta
from uuid import uuid4

from sqlalchemy import literal, or_, select, text
from sunrise_core.errors import DomainError

from .db import Account, Alias, Device, Job, LegacyIssue, Setting, Subscription, now, uid
from .panel import PanelReview, PanelUnavailable, registered_uuid_owner


class Provisioning:
    def __init__(self, db, panel, commerce=None):
        self.db, self.panel = db, panel
        self.commerce = commerce
        self._historical_auto_jobs = set()
        if panel and getattr(panel, "auto_test_telegram_ids", ()):
            self._activate_auto_mode()

    @property
    def numeric_panel(self):
        return getattr(self.panel, "contract", None) == "3.4_id"

    def _production_rows(self, session, jid, sid, original_owner, *, customer=False):
        """Use the same sorted canonical account domain as an identity merge."""
        hint_sub, hint_job = session.get(Subscription, sid), session.get(Job, jid)
        if not self.commerce or hint_sub is None or hint_job is None:
            raise PanelReview("panel_owner_changed")
        identifiers = {original_owner, hint_sub.account_id, hint_job.account_id}
        owners = self.commerce._lock_accounts_in(session, identifiers)
        canonical = owners[original_owner]
        sub = session.scalar(
            select(Subscription)
            .where(Subscription.id == sid)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        job = session.scalar(
            select(Job).where(Job.id == jid).with_for_update().execution_options(populate_existing=True)
        )
        account = session.get(Account, canonical, populate_existing=True)
        if (
            not account
            or not sub
            or not job
            or sub.account_id != canonical
            or job.account_id != canonical
            or any(owner != canonical for owner in owners.values())
        ):
            raise PanelReview("panel_owner_changed")
        self.panel.runtime_policy.require_current(session)
        if customer:
            from .accounts import require_account_access_in

            require_account_access_in(session, account, self.panel.runtime_policy)
        return account, sub, job

    def _activate_auto_mode(self):
        # Job не имеет времени создания: available_at меняется при повторах и не доказывает происхождение.
        # Снимок существующих ID создаётся один раз, до покупок текущего тестового запуска.
        key = "panel_test_activation:" + self.panel.auto_scope
        with self.db.transaction() as session:
            session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": key})
            activation = session.get(Setting, key)
            if activation is None:
                ids = list(
                    session.scalars(
                        select(Job.id).where(
                            Job.kind.in_(["provision", "device_revoke", "credential_rotate"])
                        )
                    )
                )
                activation = Setting(
                    key=key, value={"activated_at": now().isoformat(), "historical_job_ids": ids}
                )
                session.add(activation)
            self._historical_auto_jobs = set(activation.value["historical_job_ids"])

    def _auto_binding(self, session, job, sub, payload):
        policy = getattr(self.panel, "runtime_policy", None)
        if policy and policy.production:
            return self._project_binding(session, job, sub, payload)
        scoped = getattr(self.panel, "scoped_runtime_policy", None)
        if scoped:
            scoped.require_current(session, "panel_mutation")
            scoped.require_account_in(session, session.get(Account, sub.account_id) if sub else None)
            if not sub or sub.environment != "scoped_live":
                raise ValueError("panel_test_origin_denied")
            if job.kind == "provision":
                from .db import Order

                scoped.require_paid_service_in(session, session.get(Order, payload.get("order_id")))
        if (
            not sub
            or sub.environment not in ({"scoped_live"} if scoped else {"local_test", "sandbox"})
            or payload.get("environment") != sub.environment
            or job.account_id != sub.account_id
            or any(key.startswith("legacy_") for key in payload)
        ):
            raise ValueError("panel_test_origin_denied")
        account = session.get(Account, sub.account_id)
        key = "panel_test:" + sub.id
        record = session.scalar(select(Setting).where(Setting.key == key).with_for_update())
        if (
            not account
            or (
                record is None
                and (
                    not scoped
                    and str(account.telegram_id) not in self.panel.auto_test_telegram_ids
                    or account.legacy_consent
                    or session.get(Alias, account.id)
                )
            )
            or session.scalar(
                select(LegacyIssue.id)
                .where(
                    LegacyIssue.source_table == "subscriptions",
                    LegacyIssue.source_id == sub.id,
                )
                .limit(1)
            )
        ):
            raise ValueError("panel_test_owner_denied")
        if record is None:
            if (
                job.id in self._historical_auto_jobs
                or sub.panel_uuid is not None
                or (self.numeric_panel and sub.panel_user_id is not None)
                or sub.short_uuid is not None
                or sub.connection_url is not None
                or payload.get("panel_binding") is not None
            ):
                raise ValueError("panel_test_origin_denied")
            identity = uuid4()
            prefix = getattr(self.commerce.public_settings, "panel_username_prefix", "") if scoped else ""
            if scoped and prefix:
                from .panel_contacts import panel_username

                sequence = 1
                for existing in session.scalars(
                    select(Setting).where(
                        Setting.key.like("panel_test:%"),
                        Setting.value["account_id"].as_string() == str(account.id),
                    )
                ):
                    naming = existing.value.get("naming") if isinstance(existing.value, dict) else None
                    if (
                        isinstance(naming, dict)
                        and naming.get("prefix") == prefix
                        and type(naming.get("sequence")) is int
                    ):
                        sequence = max(sequence, naming["sequence"] + 1)
                username = panel_username(prefix, account, sequence)
            else:
                sequence = None
                username = "sunrise_test_" + identity.hex[:20]
            binding = {
                "uuid": str(identity),
                "short_uuid": uuid4().hex[:24],
                "username": username,
            }
            if self.numeric_panel:
                binding = {
                    "user_id": None,
                    "origin_nonce": identity.hex,
                    "short_uuid": uuid4().hex[:24],
                    "username": username,
                }
            record = Setting(
                key=key,
                value={
                    "origin": "new_test_identity",
                    "subscription_id": sub.id,
                    "account_id": str(account.id),
                    "telegram_id": str(account.telegram_id) if account.telegram_id is not None else None,
                    "environment": sub.environment,
                    "panel_host": self.panel.api_host,
                    "contract": "3.4_id" if self.numeric_panel else "2.8_uuid",
                    "binding": binding,
                    **({"naming": {"version": 1, "prefix": prefix, "sequence": sequence}} if scoped and prefix else {}),
                    "connection_url": None,
                },
            )
            session.add(record)
        else:
            value = record.value
            if self.numeric_panel:
                from .panel_v3 import registered_owner

                if not registered_owner(session, value, sub, account, self.panel):
                    raise ValueError("panel_test_owner_denied")
            elif not registered_uuid_owner(session, value, sub, account, self.panel):
                raise ValueError("panel_test_owner_denied")
            if (
                value.get("origin") != "new_test_identity"
                or value.get("subscription_id") != sub.id
                or value.get("environment") != sub.environment
                or value.get("panel_host") != self.panel.api_host
                or not isinstance(value.get("binding"), dict)
                or (self.numeric_panel and value.get("contract") != "3.4_id")
                or (
                    self.numeric_panel
                    and sub.panel_user_id is not None
                    and str(sub.panel_user_id) != value["binding"].get("user_id")
                )
                or (not self.numeric_panel and sub.panel_uuid not in {None, value["binding"].get("uuid")})
                or sub.short_uuid not in {None, value["binding"].get("short_uuid")}
                or (payload.get("panel_binding") is not None and payload["panel_binding"] != value["binding"])
                or sub.connection_url != value.get("connection_url")
            ):
                raise ValueError("panel_test_owner_denied")
            binding = value["binding"]
        self.panel.register_test_binding(
            binding,
            telegram_id=record.value["telegram_id"],
            connection_url=record.value.get("connection_url"),
            **({"account_id": record.value["account_id"]} if scoped else {}),
        )
        return binding

    def _project_binding(self, session, job, sub, payload):
        from .db import Order
        from .panel_v3 import production_binding_key, registered_project_owner

        policy = self.panel.runtime_policy
        policy.require_current(session)
        if (
            not sub
            or sub.environment != "live"
            or job.account_id != sub.account_id
            or payload.get("environment") != "live"
            or payload.get("scope_id") != policy.scope.scope_id
        ):
            raise ValueError("panel_project_origin_denied")
        account = session.get(Account, sub.account_id)
        if not account or session.get(Alias, account.id):
            raise ValueError("panel_project_owner_denied")
        key = production_binding_key(sub.id)
        gift = False
        if "promo_claim_id" in payload or "gift_segment_id" in payload:
            from .promos import verified_gift_claim_in

            gift = verified_gift_claim_in(session, job, sub, policy.scope.scope_id)
            if not gift:
                raise ValueError("panel_gift_origin_denied")
        record = session.scalar(select(Setting).where(Setting.key == key).with_for_update())
        if record is None:
            order = session.get(Order, payload.get("order_id"))
            if (
                job.kind != "provision"
                or not gift
                and (
                    not order
                    or order.account_id != sub.account_id
                    or order.subscription_id != sub.id
                    or order.environment != "live"
                    or order.status != "paid"
                    or order.fulfilled_at is None
                )
                or sub.panel_uuid is not None
                or sub.panel_user_id is not None
                or sub.short_uuid is not None
                or sub.connection_url is not None
            ):
                raise ValueError("panel_project_creation_denied")
            nonce = uuid4().hex
            from .panel_contacts import panel_username, profile_contacts

            prefix = getattr(self.commerce.public_settings, "panel_username_prefix", "")
            binding = {
                "scope_id": policy.scope.scope_id,
                "generation": 1,
                "origin_nonce": nonce,
                "user_id": None,
                "uuid": None,
                "username": panel_username(prefix, account, 1),
                "short_uuid": uuid4().hex,
            }
            record = Setting(
                key=key,
                value={
                    "scope_id": policy.scope.scope_id,
                    "generation": 1,
                    "origin": "new_project_identity",
                    "contract": "3.4_id",
                    "panel_host": self.panel.api_host,
                    "subscription_id": sub.id,
                    "account_id": str(account.id),
                    "environment": "live",
                    "binding": binding,
                    "connection_url": None,
                    "naming": {"version": 1, "prefix": prefix, "sequence": 1},
                    "creation_contacts": profile_contacts(account),
                },
            )
            session.add(record)
            session.flush()
        if not registered_project_owner(session, record.value, sub, account, self.panel):
            raise ValueError("panel_project_owner_denied")
        if (
            record.value.get("origin") == "new_project_identity"
            and sub.panel_user_id is None
            and record.value.get("creation_attempt") is None
            and not payload.get("create_started")
            and job.state != "in_flight"
        ):
            from .panel_contacts import panel_username, profile_contacts

            naming = record.value.get("naming")
            if naming:
                candidate = record.value["binding"] | {
                    "username": panel_username(naming["prefix"], account, naming["sequence"])
                }
                record.value = record.value | {
                    "binding": candidate,
                    "creation_contacts": profile_contacts(account),
                }
                if payload.get("panel_binding") is not None:
                    payload["panel_binding"] = candidate
                    job.payload = job.payload | {"panel_binding": candidate}
        if payload.get("create_started") and sub.panel_user_id is None:
            attempt = record.value.get("creation_attempt")
            if (
                not isinstance(attempt, dict)
                or attempt.get("job_id") != job.id
                or attempt.get("scope_id") != policy.scope.scope_id
                or attempt.get("binding") != record.value["binding"]
            ):
                raise ValueError("panel_creation_origin_denied")
        binding = record.value["binding"]
        if payload.get("panel_binding") is not None and payload["panel_binding"] != binding:
            raise ValueError("panel_project_generation_denied")
        self.panel.register_project_binding(
            binding,
            connection_url=record.value.get("connection_url"),
            capacity_evidence=record.value.get("capacity_evidence")
            if record.value.get("origin") == "imported_project_identity"
            else None,
        )
        return dict(binding)

    def run_jobs(self, limit=50):
        if self.panel is None:
            return 0
        completed = 0
        with self.db.transaction() as s:
            targets = [Job.payload["subscription_id"].as_string().in_(list(self.panel.bindings))]
            policy = getattr(self.panel, "runtime_policy", None)
            if policy and policy.production:
                try:
                    policy.require_current(s, "panel_mutation")
                except DomainError:
                    return 0
                targets.append(
                    (Job.payload["scope_id"].as_string() == policy.scope.scope_id)
                    & Job.payload["subscription_id"]
                    .as_string()
                    .in_(select(Subscription.id).where(Subscription.environment == "live"))
                )
            if getattr(self.panel, "auto_test_telegram_ids", ()):
                scoped = getattr(self.panel, "scoped_runtime_policy", None)
                if scoped:
                    scoped.require_current(s, "panel_mutation")
                eligible_subscriptions = (
                    select(Subscription.id)
                    .join(Account)
                    .where(
                        Account.telegram_id.in_([int(value) for value in self.panel.auto_test_telegram_ids]),
                        Subscription.environment.in_(
                            ["scoped_live"] if scoped else ["local_test", "sandbox"]
                        ),
                    )
                )
                if scoped:
                    eligible_subscriptions = select(Subscription.id).where(
                        Subscription.environment == "scoped_live"
                    )
                registered = (
                    select(Setting.key)
                    .where(Setting.key == literal("panel_test:") + Job.payload["subscription_id"].as_string())
                    .exists()
                )
                eligible = Job.payload["subscription_id"].as_string().in_(eligible_subscriptions)
                eligible = eligible | registered
                targets.append(eligible & (Job.id.not_in(self._historical_auto_jobs) | registered))
            ids = list(
                s.scalars(
                    select(Job.id)
                    .where(
                        Job.kind.in_(["provision", "device_revoke", "credential_rotate"]),
                        Job.state.in_(["pending", "in_flight"]),
                        Job.available_at <= now(),
                        or_(*targets),
                    )
                    .order_by(Job.available_at, Job.id)
                    .limit(limit)
                )
            )
        for jid in ids:
            try:
                completed += self._run(jid)
            except PanelUnavailable:
                if not policy or not policy.production:
                    raise
                # A completion/owner lock failed after transport. The outer
                # transaction rolled back; persisted start evidence and lease
                # retain GET-only recovery instead of a duplicate mutation.
                continue
            except DomainError as failure:
                if not policy or not policy.production or failure.code != "production_stage_denied":
                    raise
                break
        return completed

    def _run(self, jid):
        with self.db.transaction() as s:
            job = s.get(Job, jid)
            sid = job.payload["subscription_id"]
        # Сессионный lock на отдельном соединении не удерживает финансовую транзакцию во время HTTP.
        with self.db.engine.connect() as lock:
            key = {"key": "sunrise:panel:" + sid}
            acquired = lock.scalar(text("SELECT pg_try_advisory_lock(hashtextextended(:key, 0))"), key)
            lock.commit()
            if not acquired:
                return 0
            try:
                return self._locked(jid, sid)
            finally:
                lock.execute(text("SELECT pg_advisory_unlock(hashtextextended(:key, 0))"), key)
                lock.commit()

    def _locked(self, jid, sid):
        with self.db.transaction() as s:
            if getattr(self.panel, "runtime_policy", None):
                hint = s.get(Job, jid)
                if hint is None:
                    return 0
                _, sub, job = self._production_rows(s, jid, sid, hint.account_id)
                self.panel.runtime_policy.require_current(s, "panel_mutation")
            else:
                job = s.scalar(select(Job).where(Job.id == jid).with_for_update())
                sub = s.get(Subscription, sid)
            if job.state not in {"pending", "in_flight"} or job.available_at > now():
                return 0
            from .retention import fenced_in

            if sub is not None and fenced_in(s, sub):
                job.state, job.last_error = "review", "subscription_cleanup_pending"
                return 0
            payload = dict(job.payload)
            if job.kind == "device_revoke":
                account = s.get(Account, sub.account_id) if sub else None
                if account and account.unlink_blocked and not payload.get("revoke_started"):
                    job.state, job.last_error = "blocked", "device_unlink_blocked"
                    return 0
            try:
                if sid in self.panel.bindings and not self.numeric_panel:
                    binding = payload.get("panel_binding") or self.panel.bindings[sid]
                else:
                    binding = self._auto_binding(s, job, sub, payload)
                self.panel.validate_binding(binding)
                if (
                    not sub
                    or sub.environment not in {"local_test", "sandbox", "live", "scoped_live"}
                    or payload.get("environment") != sub.environment
                    or job.account_id != sub.account_id
                    or (
                        self.numeric_panel
                        and sub.panel_user_id is not None
                        and str(sub.panel_user_id) != binding.get("user_id")
                    )
                    or (not self.numeric_panel and sub.panel_uuid not in {None, binding["uuid"]})
                    or sub.short_uuid not in {None, binding["short_uuid"]}
                ):
                    raise ValueError("panel_identity_denied")
                if job.kind == "provision":
                    if payload.get("version") != sub.version:
                        job.state, job.last_error = "done", "superseded"
                        return 1
                    if payload.get("panel_object_type") != "internal_squad":
                        raise ValueError("panel_target_denied")
                    self.panel.validate_target(payload)
                elif job.kind == "credential_rotate":
                    from . import panel_entitlements

                    marker = panel_entitlements.override(s, sub)
                    if (
                        marker
                        and (
                            type(marker.get("devices_limit")) is not int
                            or not 1 <= marker["devices_limit"] <= 100
                        )
                        and not panel_entitlements.imported_null_capacity(s, sub)
                    ):
                        raise ValueError("panel_target_denied")
                    if (
                        sid in self.panel.bindings
                        and not self.numeric_panel
                        or payload.get("version") != sub.version
                        or type(payload.get("revoke_only_passwords")) is not bool
                    ):
                        raise ValueError("panel_target_denied")
                elif self.numeric_panel or sid not in self.panel.bindings:
                    device = s.get(Device, payload.get("device_id"))
                    if (
                        not device
                        or device.subscription_id != sid
                        or device.panel_hwid != payload.get("hwid")
                        or type(payload.get("generation")) is not int
                        or payload["generation"] != device.generation
                    ):
                        raise ValueError("panel_device_denied")
                    self.panel.register_device(binding, device.panel_hwid)
                elif payload.get("hwid") not in self.panel.hwids:
                    raise ValueError("panel_device_denied")
            except (ValueError, PanelReview) as failure:
                if isinstance(failure, PanelReview) and not getattr(self.panel, "runtime_policy", None):
                    raise
                job.state, job.last_error = (
                    ("review" if isinstance(failure, PanelReview) else "blocked"),
                    "panel_target_denied",
                )
                return 0
            create = payload.get(
                "panel_create", sub.panel_user_id is None if self.numeric_panel else sub.panel_uuid is None
            )
            uncertain = payload.get("create_started", False)
            lease = uid()
            job.payload = payload | {
                "panel_binding": binding,
                "panel_create": create,
                "create_started": uncertain
                if getattr(self.panel, "runtime_policy", None)
                else bool(create or uncertain),
                "lease": lease,
            }
            job.attempts += 1
            job.state = "in_flight"
            job.available_at = now() + timedelta(minutes=2)
            kind = job.kind
            original_owner = sub.account_id
        error, review, create_not_started = None, False, False
        try:
            if kind == "provision":

                def authorize_target():
                    with self.db.transaction() as session:
                        current_owner, current_sub, current_job = self._production_rows(
                            session, jid, sid, original_owner
                        )
                        self.panel.runtime_policy.require_current(session, "panel_mutation")
                        from .retention import fenced_in

                        if (
                            not current_sub
                            or current_sub.account_id != current_owner.id
                            or current_sub.version != payload["version"]
                            or not current_job
                            or current_job.state != "in_flight"
                            or current_job.payload.get("lease") != lease
                            or current_job.account_id != current_owner.id
                            or fenced_in(session, current_sub)
                            or self._auto_binding(session, current_job, current_sub, current_job.payload)
                            != binding
                        ):
                            raise PanelReview("panel_mutation_scope_changed")
                        if create and not uncertain:
                            from .panel_v3 import production_binding_key

                            record = session.get(Setting, production_binding_key(sid))
                            from .panel_contacts import profile_contacts

                            if profile_contacts(current_owner) != record.value.get("creation_contacts"):
                                raise PanelUnavailable(create_not_started=True)
                            current_job.payload = current_job.payload | {"create_started": True}
                            record.value = record.value | {
                                "creation_attempt": {
                                    "job_id": jid,
                                    "scope_id": self.panel.runtime_policy.scope.scope_id,
                                    "binding": dict(binding),
                                    "started_at": now().isoformat(),
                                }
                            }

                def select_candidate(previous, occupied):
                    from .panel_contacts import panel_username
                    from .panel_v3 import production_binding_key

                    with self.db.transaction() as session:
                        actor, actual_sub, current = self._production_rows(session, jid, sid, original_owner)
                        self.panel.runtime_policy.require_current(session, "panel_mutation")
                        record = session.get(Setting, production_binding_key(sid))
                        if (
                            current.payload.get("lease") != lease
                            or current.state != "in_flight"
                            or actual_sub.version != payload["version"]
                            or current.payload.get("create_started") is True
                            or record.value.get("creation_attempt") is not None
                            or record.value["binding"] != previous
                        ):
                            raise PanelReview("panel_creation_scope_changed")
                        naming = dict(record.value["naming"])
                        naming["sequence"] += 1
                        candidate = previous | {
                            "username": panel_username(naming["prefix"], actor, naming["sequence"])
                        }
                        record.value = record.value | {"binding": candidate, "naming": naming}
                        current.payload = current.payload | {"panel_binding": candidate}
                        payload["panel_binding"] = candidate
                    binding.clear()
                    binding.update(candidate)
                    self.panel.register_project_binding(binding)
                    return dict(binding)

                live_kwargs = (
                    {"before_mutation": authorize_target, "select_candidate": select_candidate}
                    if getattr(self.panel, "runtime_policy", None)
                    else {}
                )
                scoped_policy = getattr(self.panel, "scoped_runtime_policy", None)
                if scoped_policy:

                    def authorize_scoped_target():
                        with self.db.transaction() as session:
                            actor = session.scalar(
                                select(Account)
                                .where(Account.id == original_owner)
                                .with_for_update()
                                .execution_options(populate_existing=True)
                            )
                            actual_sub = session.scalar(
                                select(Subscription)
                                .where(Subscription.id == sid)
                                .with_for_update()
                                .execution_options(populate_existing=True)
                            )
                            current = session.scalar(
                                select(Job)
                                .where(Job.id == jid)
                                .with_for_update()
                                .execution_options(populate_existing=True)
                            )
                            scoped_policy.require_current(session, "panel_mutation")
                            scoped_policy.require_account_in(session, actor)
                            from .retention import fenced_in

                            if (
                                not actual_sub
                                or not current
                                or actual_sub.account_id != original_owner
                                or current.account_id != original_owner
                                or current.state != "in_flight"
                                or current.payload.get("lease") != lease
                                or actual_sub.version != payload["version"]
                                or fenced_in(session, actual_sub)
                                or self._auto_binding(session, current, actual_sub, current.payload)
                                != binding
                            ):
                                raise PanelReview("panel_mutation_scope_changed")

                    def select_scoped_candidate(previous):
                        from .panel_contacts import panel_username
                        from .retention import fenced_in
                        from .subscription_engagement import CreationIntent

                        with self.db.transaction() as session:
                            actor = session.scalar(
                                select(Account).where(Account.id == original_owner).with_for_update()
                            )
                            actual_sub = session.scalar(
                                select(Subscription).where(Subscription.id == sid).with_for_update()
                            )
                            current = session.scalar(select(Job).where(Job.id == jid).with_for_update())
                            scoped_policy.require_current(session, "panel_mutation")
                            scoped_policy.require_account_in(session, actor)
                            record = session.get(Setting, "panel_test:" + sid, with_for_update=True)
                            naming = dict(record.value.get("naming") or {}) if record else {}
                            intent = CreationIntent.model_validate(record.value.get("creation_intent")) if record else None
                            if (
                                not actual_sub
                                or not current
                                or not record
                                or actual_sub.account_id != original_owner
                                or actual_sub.panel_user_id is not None
                                or current.account_id != original_owner
                                or current.state != "in_flight"
                                or current.payload.get("lease") != lease
                                or actual_sub.version != payload["version"]
                                or fenced_in(session, actual_sub)
                                or record.value.get("binding") != previous
                                or not intent
                                or intent.job_id != jid
                                or intent.order_id != payload.get("order_id")
                                or intent.origin_nonce != previous["origin_nonce"]
                                or intent.short_uuid != previous["short_uuid"]
                                or intent.username != previous["username"]
                                or current.payload.get("creation_intent") != intent.model_dump()
                                or current.payload.get("create_started") is not True
                                or type(naming.get("sequence")) is not int
                                or naming["sequence"] >= 9999
                                or naming.get("prefix") != scoped_policy.config.panel_username_prefix
                            ):
                                raise PanelReview("panel_creation_scope_changed")
                            naming["sequence"] += 1
                            candidate = previous | {
                                "username": panel_username(naming["prefix"], actor, naming["sequence"])
                            }
                            next_intent = intent.model_copy(update={
                                "username": candidate["username"], "attempted_at": now().isoformat(),
                            }).model_dump()
                            record.value = record.value | {
                                "binding": candidate, "naming": naming, "creation_intent": next_intent,
                            }
                            current.payload = current.payload | {
                                "panel_binding": candidate,
                                "creation_intent": next_intent,
                                "create_started": False,
                            }
                            payload["panel_binding"] = candidate
                            saved_telegram_id = record.value.get("telegram_id")
                        binding.clear()
                        binding.update(candidate)
                        self.panel.register_test_binding(
                            binding, telegram_id=saved_telegram_id, account_id=str(original_owner)
                        )
                        return dict(binding)

                    live_kwargs = {"before_mutation": authorize_scoped_target}
                    if create and not uncertain:
                        live_kwargs["select_candidate"] = select_scoped_candidate
                if live_kwargs and create and not scoped_policy:
                    from .panel_v3 import production_binding_key

                    with self.db.transaction() as session:
                        record = session.get(Setting, production_binding_key(sid))
                        live_kwargs["create_contacts"] = record.value.get("creation_contacts")
                if self.numeric_panel and create and payload.get("order_id"):

                    def mark_creation():
                        from .db import Order, Payment, Quote
                        from .subscription_engagement import CreationIntent

                        with self.db.transaction() as session:
                            current_owner = original_owner
                            if getattr(self.panel, "runtime_policy", None):
                                actor, current_sub, current_job = self._production_rows(
                                    session, jid, sid, original_owner
                                )
                                current_owner = actor.id
                                self.panel.runtime_policy.require_current(session, "panel_mutation")
                                from .retention import fenced_in

                                if fenced_in(session, current_sub):
                                    raise PanelReview("panel_mutation_scope_changed")
                            else:
                                session.scalar(
                                    select(Account).where(Account.id == original_owner).with_for_update()
                                )
                                current_sub = session.scalar(
                                    select(Subscription).where(Subscription.id == sid).with_for_update()
                                )
                                current_job = session.scalar(
                                    select(Job).where(Job.id == jid).with_for_update()
                                )
                            if (
                                not current_sub
                                or current_sub.account_id != current_owner
                                or not current_job
                                or current_job.account_id != current_owner
                                or current_job.state != "in_flight"
                                or current_job.payload.get("lease") != lease
                                or current_sub.version != payload["version"]
                                or current_sub.panel_user_id is not None
                                or self._auto_binding(session, current_job, current_sub, current_job.payload)
                                != binding
                            ):
                                raise PanelReview("panel_creation_scope_changed")
                            order = session.get(Order, payload["order_id"])
                            quote = session.get(Quote, order.quote_id) if order else None
                            if (
                                not order
                                or order.account_id != current_owner
                                or order.subscription_id != sid
                                or order.status != "paid"
                                or order.fulfilled_at is None
                                or not quote
                                or quote.snapshot.get("action") != "purchase"
                                or quote.snapshot.get("subscription_id") is not None
                                or not (
                                    getattr(self.panel, "scoped_runtime_policy", None) and order.amount == 0
                                )
                                and session.scalar(
                                    select(Payment.id).where(
                                        Payment.order_id == order.id, Payment.status == "paid"
                                    )
                                )
                                is None
                            ):
                                return
                            from .panel_v3 import production_binding_key

                            record = session.get(
                                Setting,
                                production_binding_key(sid)
                                if current_sub.environment == "live"
                                else "panel_test:" + sid,
                            )
                            if getattr(self.panel, "runtime_policy", None):
                                from .panel_contacts import profile_contacts

                                if profile_contacts(session.get(Account, current_owner)) != record.value.get(
                                    "creation_contacts"
                                ):
                                    raise PanelUnavailable(create_not_started=True)
                            existing = record.value.get("creation_intent")
                            intent = (
                                CreationIntent.model_validate(existing)
                                if existing
                                else CreationIntent(
                                    subscription_id=sid,
                                    owner_id=str(current_owner),
                                    environment=current_sub.environment,
                                    panel_host=self.panel.api_host,
                                    origin_nonce=binding["origin_nonce"],
                                    username=binding["username"],
                                    short_uuid=binding["short_uuid"],
                                    job_id=jid,
                                    order_id=order.id,
                                    attempted_at=now().isoformat(),
                                )
                            )
                            if (
                                intent.job_id != jid
                                or intent.order_id != order.id
                                or intent.origin_nonce != binding["origin_nonce"]
                                or intent.username != binding["username"]
                                or intent.short_uuid != binding["short_uuid"]
                            ):
                                raise PanelReview("panel_creation_origin_changed")
                            record.value = record.value | {"creation_intent": intent.model_dump()}
                            current_job.payload = current_job.payload | {"creation_intent": intent.model_dump()}
                            # Production saves create_started with creation_attempt in before_mutation.
                            if not getattr(self.panel, "runtime_policy", None):
                                current_job.payload = current_job.payload | {"create_started": True}

                    result = self.panel.apply(
                        binding,
                        payload,
                        create=create,
                        uncertain=uncertain,
                        before_create=mark_creation,
                        **live_kwargs,
                    )
                else:
                    result = self.panel.apply(
                        binding, payload, create=create, uncertain=uncertain, **live_kwargs
                    )
            elif kind == "credential_rotate":

                def mark_rotation(proof):
                    with self.db.transaction() as session:
                        if getattr(self.panel, "runtime_policy", None):
                            _, actual_sub, current = self._production_rows(
                                session, jid, sid, original_owner, customer=True
                            )
                            self.panel.runtime_policy.require_current(session, "panel_mutation")
                            from .retention import fenced_in

                            if fenced_in(session, actual_sub):
                                raise PanelReview("panel_mutation_scope_changed")
                        else:
                            current = session.scalar(select(Job).where(Job.id == jid).with_for_update())
                            actual_sub = session.get(Subscription, sid)
                        if (
                            current.payload.get("lease") != lease
                            or not actual_sub
                            or current.account_id != actual_sub.account_id
                            or actual_sub.version != payload["version"]
                        ):
                            raise PanelReview("panel_owner_changed")
                        try:
                            current_binding = self._auto_binding(
                                session, current, actual_sub, current.payload
                            )
                            if current_binding != binding:
                                raise ValueError
                        except ValueError:
                            raise PanelReview("panel_identity_changed") from None
                        current.payload = current.payload | proof | {"rotate_started": True}

                result = self.panel.rotate(
                    binding,
                    revoke_only_passwords=payload["revoke_only_passwords"],
                    uncertain=payload.get("rotate_started", False),
                    before_short_uuid=payload.get("before_short_uuid"),
                    before_sub_revoked_at=payload.get("before_sub_revoked_at"),
                    before_expires_at=payload.get("before_expires_at"),
                    before_rotate=mark_rotation,
                    **(
                        {"expected_short_uuid": payload.get("next_short_uuid")}
                        if not self.numeric_panel
                        else {}
                    ),
                )
            else:

                def mark_started():
                    with self.db.transaction() as session:
                        if getattr(self.panel, "runtime_policy", None):
                            owner, actual_sub, current = self._production_rows(
                                session, jid, sid, original_owner, customer=True
                            )
                            self.panel.runtime_policy.require_current(session, "panel_mutation")
                            from .retention import fenced_in

                            if fenced_in(session, actual_sub):
                                raise PanelReview("panel_mutation_scope_changed")
                        else:
                            current = session.scalar(select(Job).where(Job.id == jid).with_for_update())
                            owner = session.scalar(
                                select(Account).where(Account.id == current.account_id).with_for_update()
                            )
                            actual_sub = session.get(Subscription, sid)
                        if current.payload.get("lease") != lease:
                            raise PanelReview("panel_lease_changed")
                        if not owner or not actual_sub or actual_sub.account_id != owner.id:
                            raise PanelReview("panel_owner_changed")
                        if self.numeric_panel or sid not in self.panel.bindings:
                            actual_device = session.get(Device, payload["device_id"])
                            if (
                                not actual_device
                                or actual_device.subscription_id != sid
                                or actual_device.panel_hwid != payload["hwid"]
                                or actual_device.generation != payload["generation"]
                            ):
                                raise PanelReview("panel_device_changed")
                            try:
                                current_binding = self._auto_binding(
                                    session, current, actual_sub, current.payload
                                )
                                if current_binding != binding:
                                    raise ValueError
                            except ValueError:
                                raise PanelReview("panel_identity_changed") from None
                        if owner.unlink_blocked:
                            raise DomainError(
                                "device_unlink_blocked", "Отвязка устройств ограничена владельцем.", 403
                            )
                        current.payload = current.payload | {"revoke_started": True}

                result = self.panel.revoke(
                    binding,
                    payload["hwid"],
                    uncertain=payload.get("revoke_started", False),
                    before_delete=mark_started,
                )
        except PanelReview:
            error, review = "panel_review_required", True
        except DomainError as failure:
            if failure.code != "device_unlink_blocked":
                raise
            error = failure.code
        except PanelUnavailable as failure:
            error = "panel_unavailable"
            create_not_started = (
                kind == "provision"
                and create is True
                and uncertain is False
                and failure.create_not_started is True
            )
        with self.db.transaction() as s:
            if getattr(self.panel, "runtime_policy", None):
                _, sub, job = self._production_rows(s, jid, sid, original_owner)
            else:
                job = s.scalar(select(Job).where(Job.id == jid).with_for_update())
            if job.payload.get("lease") != lease:
                return 0
            if error:
                if error == "device_unlink_blocked":
                    job.state, job.last_error = "blocked", error
                    return 0
                if create_not_started:
                    job.payload = dict(job.payload) | {"create_started": False}
                    if getattr(self.panel, "runtime_policy", None):
                        from .panel_v3 import production_binding_key

                        record = s.get(Setting, production_binding_key(sid))
                        intent = record.value.get("creation_intent") if record else None
                        if (
                            record
                            and record.value.get("creation_attempt") is None
                            and isinstance(intent, dict)
                            and intent.get("job_id") == jid
                        ):
                            # A verified pre-POST failure is not a creation event.
                            record.value = {
                                key: value for key, value in record.value.items() if key != "creation_intent"
                            }
                            job.payload = {
                                key: value for key, value in job.payload.items() if key != "creation_intent"
                            }
                job.state = "review" if review else "pending"
                job.last_error = error
                job.available_at = now() + timedelta(minutes=min(60, 2 ** min(job.attempts, 6)))
                return 0
            if not getattr(self.panel, "runtime_policy", None):
                sub = s.scalar(select(Subscription).where(Subscription.id == sid).with_for_update())
            if sub.account_id != job.account_id:
                job.state, job.last_error = "review", "panel_test_owner_changed"
                return 0
            if sid not in self.panel.bindings or self.numeric_panel:
                try:
                    self._auto_binding(s, job, sub, payload)
                except ValueError:
                    job.state, job.last_error = "review", "panel_test_owner_changed"
                    return 0
            if kind in {"provision", "credential_rotate"}:
                if (
                    kind == "provision"
                    and sub.connection_url
                    and sub.connection_url != result["connection_url"]
                ):
                    job.state, job.last_error = "review", "panel_connection_changed"
                    return 0
                if self.numeric_panel:
                    from .panel_v3 import numeric_id

                    resolved_id = int(result["user_id"])
                    numeric_id(resolved_id)
                    if s.scalar(
                        select(Subscription.id).where(
                            Subscription.panel_user_id == resolved_id, Subscription.id != sid
                        )
                    ):
                        job.state, job.last_error = "review", "panel_identity_collision"
                        return 0
                    sub.panel_user_id, sub.short_uuid = resolved_id, result["short_uuid"]
                    binding = binding | {"user_id": str(resolved_id), "short_uuid": result["short_uuid"]}
                    job.payload = job.payload | {
                        "panel_binding": binding,
                        "panel_create": False,
                        "create_started": False,
                    }
                else:
                    sub.panel_uuid, sub.short_uuid = binding["uuid"], result["short_uuid"]
                    if kind == "credential_rotate":
                        binding = binding | {"short_uuid": result["short_uuid"]}
                        job.payload = job.payload | {
                            "panel_binding": binding,
                            "panel_create": False,
                            "create_started": False,
                        }
                sub.connection_url = result["connection_url"]
                from .panel_v3 import production_binding_key

                record = s.scalar(
                    select(Setting)
                    .where(
                        Setting.key
                        == (production_binding_key(sid) if sub.environment == "live" else "panel_test:" + sid)
                    )
                    .with_for_update()
                )
                if record is not None:
                    if kind == "credential_rotate":
                        from . import panel_entitlements

                        panel_entitlements.rebase_rotation(
                            s, sub, record.value.get("binding"), binding, payload["version"], job.id
                        )
                    record.value = record.value | {
                        "connection_url": result["connection_url"],
                        "binding": binding,
                    }
                contact_policy = getattr(self.panel, "runtime_policy", None) or getattr(
                    self.panel, "scoped_runtime_policy", None
                )
                if contact_policy:
                    from .panel_contacts import enqueue_contacts_in

                    enqueue_contacts_in(
                        s, s.get(Account, sub.account_id), contact_policy, "provisioned:" + jid
                    )
                if kind == "provision" and sub.version == payload["version"]:
                    sub.status = payload.get("desired_subscription_status") or (
                        "suspended" if payload.get("panel_status") == "DISABLED" else "active"
                    )
            else:
                from .commerce import Commerce
                from .moderation import DeviceModeration

                if result is True:
                    if self.numeric_panel or sid not in self.panel.bindings:
                        actual_device = s.get(Device, payload["device_id"])
                        if (
                            not actual_device
                            or actual_device.subscription_id != sid
                            or actual_device.generation != payload["generation"]
                        ):
                            job.state, job.last_error = "review", "panel_device_changed"
                            return 0
                    DeviceModeration(self.commerce or Commerce(self.db)).complete(
                        s, sub.account_id, sid, payload["device_id"]
                    )
                else:
                    job.state, job.last_error = "review", "device_unlink_not_observed"
                    return 0
            if kind == "provision" and self.numeric_panel:
                job.payload = job.payload | {
                    "applied_target": result.get("applied_target"),
                    "applied_binding": binding,
                    "applied_at": now().isoformat(),
                }
            job.state, job.last_error = "done", None
            if kind == "provision" and self.numeric_panel and self.commerce is not None:
                from .subscription_engagement import SubscriptionEngagement

                SubscriptionEngagement(self.commerce).observe_in(
                    s,
                    sub,
                    {
                        "connection_metadata": result.get("connection_metadata", {}),
                        "status": payload.get("panel_status", "ACTIVE"),
                        "expires_at": payload["expires_at"],
                    },
                )
            return 1
