"""Подтверждённые контакты только собственных пользователей production-панели."""

import hashlib
import re
from datetime import timedelta

from sqlalchemy import select, text
from sunrise_core.errors import DomainError

from .db import Account, Job, Setting, Subscription, now, uid
from .panel import PanelReview, PanelUnavailable


def profile_contacts(account):
    telegram = account.telegram_id
    if telegram is not None and (type(telegram) is not int or not 1 <= telegram < 2**52):
        raise PanelReview("panel_contact_invalid")
    result = {"telegramId": telegram}
    email = getattr(account, "email", None)
    if email and getattr(account, "email_verified", False) is True:
        if (
            not isinstance(email, str)
            or len(email) > 320
            or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email)
        ):
            raise PanelReview("panel_contact_invalid")
        result["email"] = email
    return result


def panel_username(prefix, account, sequence):
    if not isinstance(prefix, str) or not re.fullmatch(r"[A-Za-z0-9_-]{0,12}", prefix):
        raise PanelReview("panel_name_invalid")
    if type(sequence) is not int or not 1 <= sequence <= 9999:
        raise PanelReview("panel_name_exhausted")
    contacts = profile_contacts(account)
    lead = prefix + "_" if prefix else ""
    tail = "_" + str(sequence)
    if contacts["telegramId"] is not None:
        telegram = str(contacts["telegramId"])
        basis = getattr(account, "telegram_username", None)
        if not basis:
            return lead + telegram + tail
        tail = "_" + telegram + tail
    else:
        basis = contacts.get("email")
        if not basis:
            raise PanelReview("panel_contact_required")
    if not isinstance(basis, str):
        raise PanelReview("panel_name_invalid")
    basis = re.sub(r"_+", "_", re.sub(r"[^A-Za-z0-9_-]", "_", basis)).strip("_-") or "email"
    basis = basis[: 36 - len(lead) - len(tail)]
    value = lead + basis + tail
    return value if len(value) >= 3 else "u" + value


def contact_key(sid):
    return "panel_contact:" + hashlib.sha256(sid.encode()).hexdigest()


def enqueue_contacts_in(session, account, policy, source_event):
    """Identity confirmation and its durable intent commit or roll back together."""
    scoped = bool(policy and getattr(policy, "scoped_payments", False))
    if not policy or not (policy.production or scoped):
        return 0
    from types import SimpleNamespace
    from urllib.parse import urlparse

    from .panel_v3 import production_binding_key, registered_project_owner

    policy.require_current(session)
    session.flush()
    account = session.scalar(select(Account).where(Account.id == account.id).with_for_update())
    if scoped:
        policy.require_account_in(session, account)
    contacts = profile_contacts(account)
    panel = SimpleNamespace(runtime_policy=policy, api_host=urlparse(policy.scope.panel.origin).hostname)
    count = 0
    for sub in session.scalars(
        select(Subscription)
        .where(
            Subscription.account_id == account.id,
            Subscription.environment == ("scoped_live" if scoped else "live"),
        )
        .order_by(Subscription.id)
        .with_for_update()
    ):
        if session.get(Setting, "archive:" + sub.id) is not None:
            continue
        # Lock order matches merge/provisioning: account, subscription, job, markers.
        key = contact_key(sub.id)
        job = session.scalar(select(Job).where(Job.key == key).with_for_update())
        record = session.scalar(
            select(Setting)
            .where(Setting.key == ("panel_test:" + sub.id if scoped else production_binding_key(sub.id)))
            .with_for_update()
        )
        if (
            record is None
            or not scoped
            and not registered_project_owner(session, record.value, sub, account, panel)
            or sub.panel_user_id is None
        ):
            continue
        if scoped:
            policy.require_subscription_in(session, sub)
        marker = session.scalar(select(Setting).where(Setting.key == key).with_for_update())
        binding = dict(record.value["binding"])
        if (
            job is not None
            and job.payload.get("goal") == contacts
            and job.payload.get("binding") == binding
            and job.account_id == account.id
        ):
            continue
        generation = 1 if marker is None else marker.value["goal_generation"] + 1
        payload = {
            "version": 1,
            "scope_id": policy.scope.scope_id,
            "environment": "scoped_live" if scoped else "live",
            "subscription_id": sub.id,
            "account_id": str(account.id),
            "binding": binding,
            "goal_generation": generation,
            "goal": contacts,
            "source_event": source_event,
            "attempt": job.payload.get("attempt") if job else None,
        }
        if marker is None:
            marker = Setting(key=key, value={})
            session.add(marker)
        marker.value = {
            "version": 1,
            "scope_id": policy.scope.scope_id,
            "subscription_id": sub.id,
            "goal_generation": generation,
        }
        if job is None:
            job = Job(key=key, kind="panel_contact", account_id=account.id, payload=payload)
            session.add(job)
        else:
            job.payload, job.state, job.available_at, job.last_error = payload, "pending", now(), None
        count += 1
    return count


class PanelContactSync:
    def __init__(self, commerce):
        self.commerce, self.db, self.panel, self.policy = (
            commerce,
            commerce.db,
            commerce.panel_provider,
            commerce.runtime_policy,
        )

    def enqueue_in(self, session, account, source_event):
        return enqueue_contacts_in(session, account, self.policy, source_event)

    def _rows(self, session, jid, sid, owner, *, gate=True):
        if getattr(self.policy, "scoped_payments", False):
            return self._scoped_rows(session, jid, sid, owner, gate=gate)
        from .panel_v3 import production_binding_key, registered_project_owner
        from .provisioning import Provisioning
        from .retention import fenced_in

        account, sub, job = Provisioning(self.db, self.panel, self.commerce)._production_rows(
            session, jid, sid, owner, customer=True
        )
        self.policy.require_current(session, "panel_mutation" if gate else None)
        record = session.scalar(
            select(Setting)
            .where(Setting.key == production_binding_key(sid))
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        marker = session.scalar(
            select(Setting)
            .where(Setting.key == contact_key(sid))
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        payload = job.payload
        if (
            not record
            or not marker
            or fenced_in(session, sub)
            or not registered_project_owner(session, record.value, sub, account, self.panel)
            or sub.panel_user_id is None
            or job.kind != "panel_contact"
            or payload.get("version") != 1
            or payload.get("scope_id") != self.policy.scope.scope_id
            or payload.get("environment") != "live"
            or payload.get("subscription_id") != sid
            or payload.get("account_id") != str(account.id)
            or payload.get("binding") != record.value["binding"]
            or marker.value
            != {
                "version": 1,
                "scope_id": self.policy.scope.scope_id,
                "subscription_id": sid,
                "goal_generation": payload.get("goal_generation"),
            }
        ):
            raise PanelReview("panel_contact_scope_changed")
        self.panel.register_project_binding(
            record.value["binding"],
            connection_url=record.value["connection_url"],
            capacity_evidence=record.value.get("capacity_evidence"),
        )
        return account, sub, job

    def _scoped_rows(self, session, jid, sid, owner, *, gate=True):
        from .retention import fenced_in

        hint_sub, hint_job = session.get(Subscription, sid), session.get(Job, jid)
        if not hint_sub or not hint_job:
            raise PanelReview("panel_contact_scope_changed")
        owners = self.commerce._lock_accounts_in(session, [owner, hint_sub.account_id, hint_job.account_id])
        account = session.get(Account, owners[owner], populate_existing=True)
        sub = session.scalar(
            select(Subscription)
            .where(Subscription.id == sid)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        job = session.scalar(
            select(Job).where(Job.id == jid).with_for_update().execution_options(populate_existing=True)
        )
        self.policy.require_current(session, "panel_mutation" if gate else None)
        self.policy.require_account_in(session, account)
        self.policy.require_subscription_in(session, sub)
        record = session.scalar(
            select(Setting)
            .where(Setting.key == "panel_test:" + sid)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        marker = session.scalar(
            select(Setting)
            .where(Setting.key == contact_key(sid))
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        payload = job.payload if job else {}
        if (
            not record
            or not marker
            or not job
            or sub.account_id != account.id
            or job.account_id != account.id
            or any(value != account.id for value in owners.values())
            or fenced_in(session, sub)
            or sub.panel_user_id is None
            or job.kind != "panel_contact"
            or payload.get("version") != 1
            or payload.get("scope_id") != self.policy.scope.scope_id
            or payload.get("environment") != "scoped_live"
            or payload.get("subscription_id") != sid
            or not self.policy.owns_original_in(session, account, payload.get("account_id"))
            or payload.get("binding") != record.value.get("binding")
            or marker.value
            != {
                "version": 1,
                "scope_id": self.policy.scope.scope_id,
                "subscription_id": sid,
                "goal_generation": payload.get("goal_generation"),
            }
        ):
            raise PanelReview("panel_contact_scope_changed")
        self.panel.register_test_binding(
            record.value["binding"],
            telegram_id=record.value["telegram_id"],
            account_id=record.value["account_id"],
            connection_url=record.value["connection_url"],
        )
        return account, sub, job

    def run_pending(self, limit=20):
        if (
            not self.policy
            or not (self.policy.production or getattr(self.policy, "scoped_payments", False))
            or self.panel is None
        ):
            return 0
        try:
            with self.db.transaction() as session:
                self.policy.require_current(session, "panel_mutation")
                ids = list(
                    session.scalars(
                        select(Job.id)
                        .where(
                            Job.kind == "panel_contact",
                            Job.state.in_(["pending", "in_flight"]),
                            Job.available_at <= now(),
                            Job.payload["scope_id"].as_string() == self.policy.scope.scope_id,
                        )
                        .order_by(Job.available_at, Job.id)
                        .limit(limit)
                    )
                )
        except DomainError as failure:
            if failure.code != "production_stage_denied":
                raise
            return 0
        completed = 0
        for jid in ids:
            completed += self._run(jid)
        return completed

    def _run(self, jid):
        with self.db.transaction() as session:
            hint = session.get(Job, jid)
            sid, owner, generation = (
                hint.payload["subscription_id"],
                hint.account_id,
                hint.payload["goal_generation"],
            )
        key = {"key": "sunrise:panel:" + sid}
        with self.db.engine.connect() as lock:
            acquired = lock.scalar(text("SELECT pg_try_advisory_lock(hashtextextended(:key, 0))"), key)
            lock.commit()
            if not acquired:
                return 0
            try:
                try:
                    return self._locked(jid, sid, owner)
                except (PanelReview, PanelUnavailable, DomainError) as failure:
                    # Defer while the same per-user advisory lock is held;
                    # a newer profile generation must never be overwritten.
                    with self.db.transaction() as session:
                        try:
                            _, _, job = self._rows(session, jid, sid, owner, gate=False)
                        except (PanelReview, DomainError):
                            if getattr(self.policy, "scoped_payments", False):
                                return 0
                            from .provisioning import Provisioning

                            try:
                                _, _, job = Provisioning(self.db, self.panel, self.commerce)._production_rows(
                                    session, jid, sid, owner
                                )
                            except (PanelReview, DomainError):
                                return 0
                            if (
                                job.kind == "panel_contact"
                                and job.payload.get("scope_id") == self.policy.scope.scope_id
                                and job.payload.get("subscription_id") == sid
                                and job.payload.get("goal_generation") == generation
                                and job.state in {"pending", "in_flight"}
                            ):
                                # Stop an owned rejected intent without touching
                                # the foreign/archive marker or transport.
                                job.state, job.last_error = "review", "panel_contact_scope_changed"
                            return 0
                        if job.payload["goal_generation"] == generation and job.state in {
                            "pending",
                            "in_flight",
                        }:
                            job.state, job.available_at = "pending", now() + timedelta(seconds=30)
                            job.last_error = (
                                "panel_contact_review"
                                if isinstance(failure, PanelReview)
                                else "panel_contact_unavailable"
                            )
                    return 0
            finally:
                lock.execute(text("SELECT pg_advisory_unlock(hashtextextended(:key, 0))"), key)
                lock.commit()

    def _locked(self, jid, sid, owner):
        with self.db.transaction() as session:
            actor, _, job = self._rows(session, jid, sid, owner)
            if job.state not in {"pending", "in_flight"} or job.available_at > now():
                return 0
            if profile_contacts(actor) != job.payload["goal"]:
                self.enqueue_in(session, actor, "profile-recheck")
                return 0
            payload = dict(job.payload)
            lease = uid()
            job.payload = payload | {"lease": lease}
            job.state, job.available_at = "in_flight", now() + timedelta(minutes=2)
            job.attempts += 1
        binding, goal, generation = payload["binding"], payload["goal"], payload["goal_generation"]

        def authorize(current=None):
            with self.db.transaction() as session:
                actor, _, job = self._rows(session, jid, sid, owner)
                if (
                    job.state != "in_flight"
                    or job.payload.get("lease") != lease
                    or job.payload["goal_generation"] != generation
                    or profile_contacts(actor) != goal
                ):
                    raise PanelReview("panel_contact_goal_changed")
                if current is not None:
                    if job.payload.get("attempt") is not None:
                        raise PanelReview("panel_contact_attempt_unknown")
                    job.payload = job.payload | {
                        "attempt": {
                            "generation": generation,
                            "binding": binding,
                            "goal": goal,
                            "before": {name: current.get(name) for name in goal},
                            "started_at": now().isoformat(),
                        }
                    }

        attempt = payload.get("attempt")
        if attempt:
            # One unknown PATCH can still finish later. A different goal is
            # withheld until the old target is observed, not just an old value.
            if attempt.get("binding") != binding:
                raise PanelReview("panel_contact_attempt_changed")
            current = self.panel._request("GET", "users/" + binding["user_id"])
            self.panel._identity(current, binding)
            self.panel._connection(current, binding)
            if (
                not all(name in current and current[name] == value for name, value in attempt["goal"].items())
                or attempt["before"] == attempt["goal"]
            ):
                raise PanelReview("panel_contact_attempt_unknown")
        else:
            self.panel.sync_contacts(binding, goal, before_patch=authorize, before_start=authorize)
        with self.db.transaction() as session:
            actor, _, job = self._rows(session, jid, sid, owner)
            if job.payload.get("lease") != lease or job.payload["goal_generation"] != generation:
                return 0
            job.payload = job.payload | {"attempt": None}
            if attempt and attempt["goal"] != goal or profile_contacts(actor) != goal:
                job.state, job.available_at = "pending", now()
                return 0
            job.state, job.last_error = "done", None
            return 1
