"""Общая рассылка: Telegram либо подтверждённая почта."""

from types import SimpleNamespace

from sqlalchemy import func, or_, select
from sunrise_core.content import validate_content
from sunrise_core.errors import DomainError, missing

from .db import Account, Alias, Job, Notification, NotificationDelivery, StaffRole, uid
from .instance_config import configured_instance
from .mail import render_mail_text
from .notifications import NotificationService, validate_telegram_content
from .staff_permissions import StaffPermissions, telegram_admin_ids


class BroadcastService:
    def __init__(self, commerce, settings):
        self.commerce, self.db, self.settings = commerce, commerce.db, settings
        self.runtime_policy = getattr(commerce, "runtime_policy", None)
        self.staff = StaffPermissions(commerce.accounts or SimpleNamespace(db=self.db, settings=settings))
        self.notifications = NotificationService(self.db, settings, mail_transport=commerce.mail_transport)

    def eligible_in(self, session):
        return [
            account
            for account in session.scalars(select(Account).order_by(Account.id))
            if (self.live or self.commerce.legal.status_in(session, account)["accepted"])
            and (account.telegram_id or account.email_verified and account.email)
            and (not self.live or session.get(Alias, account.id, populate_existing=True) is None)
        ]

    @property
    def live(self):
        return bool(self.runtime_policy and self.runtime_policy.production)

    def _admit(self, session, *, delivery=False):
        if self.live or getattr(self.runtime_policy, "scoped_payments", False):
            self.runtime_policy.require_current(session, "broadcast_delivery" if delivery else None)

    def _job_scope(self, job):
        return not self.live or (
            job.payload.get("environment") == "live"
            and job.payload.get("scope_id") == self.runtime_policy.scope.scope_id
        )

    def _current_actor(self, session, identifier):
        actor = session.get(Account, identifier, populate_existing=True)
        session.get(StaffRole, identifier, populate_existing=True)
        return bool(
            actor
            and session.get(Alias, identifier, populate_existing=True) is None
            and "broadcasts_write" in self.staff.capabilities_in(session, actor, bot=True)
        )

    def _allowed(self, account):
        if self.live:
            return (
                type(account.telegram_id) is int
                and account.telegram_id > 0
                and not account.telegram_notifications_blocked
                if account.telegram_id is not None
                else bool(account.email and account.email_verified)
            )
        if account.telegram_id:
            return str(account.telegram_id) in {
                v.strip() for v in self.settings.test_telegram_recipients.split(",") if v.strip()
            }
        return account.email in self.settings.email_allowlist

    def _nodes(self, nodes):
        try:
            validate_content(nodes)
        except ValueError:
            raise DomainError("content_invalid", "Проверьте текст рассылки.", 400) from None
        body = render_mail_text(nodes)
        if not body or len(body) > 10000:
            raise DomainError("content_invalid", "Текст рассылки пуст или слишком длинный.", 400)
        validate_telegram_content(self._title, body, nodes)
        return body

    @property
    def _title(self):
        return "Сообщение " + configured_instance(self.settings).app_name

    def preview(self, actor, nodes, *, media=None):
        self._nodes(nodes)
        with self.db.transaction() as session:
            self.staff.require_in(session, actor, "broadcasts_write", bot=True)
            self._admit(session)
            if media is not None:
                self._media_in(session, actor, media, nodes)
            accounts = self.eligible_in(session)
            count = sum(self._allowed(a) for a in accounts)
            return {
                "nodes": nodes,
                "eligible_total": len(accounts),
                "recipient_count": count,
                "denied_count": len(accounts) - count,
                "delivery": "durable_notifications",
            }

    def create(self, actor, nodes, reason, draft=True, *, media=None):
        self._nodes(nodes)
        if not isinstance(reason, str) or not 5 <= len(reason.strip()) <= 500:
            raise DomainError("reason_required", "Укажите причину рассылки.", 400)
        with self.db.transaction() as session:
            account = self.staff.require_in(session, actor, "broadcasts_write", bot=True)
            self._admit(session, delivery=True)
            if media is not None:
                media = self._media_in(session, account.id, media, nodes)
            accounts = self.eligible_in(session)
            ids = [str(a.id) for a in accounts if self._allowed(a)]
            self._admit(session, delivery=True)
            job = Job(
                key="broadcast:" + uid(),
                kind="broadcast",
                account_id=account.id,
                state="draft" if draft else "pending",
                payload={
                    "nodes": nodes,
                    "account_ids": ids,
                    "environment": "live" if self.live else "local_test",
                    **({"scope_id": self.runtime_policy.scope.scope_id} if self.live else {}),
                    "source": "unified",
                    "delivery": "durable_notifications",
                    "recipient_count": len(ids),
                    **({"media": media} if media is not None else {}),
                },
            )
            session.add(job)
            session.flush()
            from .db import Audit

            session.add(
                Audit(
                    actor_id=int(actor),
                    action="broadcast_create",
                    target=job.id,
                    reason=reason.strip(),
                    details={"recipient_count": len(ids)},
                )
            )
            return self._dto(session, job)

    def _job(self, session, actor, identifier):
        self.staff.require_in(session, actor, "broadcasts_write", bot=True)
        job = session.scalar(
            select(Job)
            .where(
                Job.id == identifier, Job.kind == "broadcast", Job.payload["source"].as_string() == "unified"
            )
            .with_for_update()
        )
        if not job or not self._job_scope(job):
            raise missing()
        self._admit(session)
        return job

    def start(self, actor, identifier, reason):
        self._reason(reason)
        with self.db.transaction() as session:
            job = self._job(session, actor, identifier)
            self._admit(session, delivery=True)
            if job.payload.get("media") is not None:
                self._media_in(session, job.account_id, job.payload["media"], job.payload["nodes"])
            if job.state not in {"draft", "pending", "done"}:
                raise DomainError("broadcast_state_invalid", "Эту рассылку нельзя запустить.", 409)
            if job.state == "draft":
                job.state = "pending"
            self._audit(session, actor, job.id, "broadcast_start", reason)
            return self._dto(session, job)

    def stop(self, actor, identifier, reason):
        self._reason(reason)
        with self.db.transaction() as session:
            job = self._job(session, actor, identifier)
            job.state = "stopped"
            rows = session.scalars(
                select(NotificationDelivery)
                .join(Notification, Notification.id == NotificationDelivery.notification_id)
                .where(
                    Notification.event_key == "broadcast:" + job.id, NotificationDelivery.state == "pending"
                )
                .with_for_update()
            )
            for row in rows:
                row.state, row.last_error = "cancelled", "broadcast_stopped"
            self._audit(session, actor, job.id, "broadcast_stop", reason)
            return self._dto(session, job)

    def detail(self, actor, identifier):
        with self.db.transaction() as session:
            return self._dto(session, self._job(session, actor, identifier))

    def _reason(self, reason):
        if not isinstance(reason, str) or not 5 <= len(reason.strip()) <= 500:
            raise DomainError("reason_required", "Укажите причину действия.", 400)

    def _audit(self, session, actor, identifier, action, reason):
        from .db import Audit

        session.add(
            Audit(actor_id=int(actor), action=action, target=identifier, reason=reason.strip(), details={})
        )

    def list(self, actor, page=1, limit=10):
        from sqlalchemy import func

        page, limit = max(1, int(page)), max(1, min(int(limit), 100))
        with self.db.transaction() as session:
            self.staff.require_in(session, actor, "broadcasts_write", bot=True)
            query = select(Job).where(Job.kind == "broadcast", Job.payload["source"].as_string() == "unified")
            total = session.scalar(select(func.count()).select_from(query.subquery()))
            jobs = session.scalars(query.order_by(Job.id.desc()).offset((page - 1) * limit).limit(limit))
            return {
                "items": [self._dto(session, job) for job in jobs],
                "total": total,
                "page": page,
                "page_size": limit,
            }

    def _dto(self, session, job):
        rows = session.execute(
            select(NotificationDelivery.state, NotificationDelivery.last_error, func.count())
            .join(Notification, Notification.id == NotificationDelivery.notification_id)
            .where(
                Notification.event_key == "broadcast:" + job.id,
                NotificationDelivery.channel.in_(["email", "telegram"]),
            )
            .group_by(NotificationDelivery.state, NotificationDelivery.last_error)
        ).all()
        counts = dict.fromkeys(
            (
                "delivered_count",
                "blocked_count",
                "provider_rejected_count",
                "unknown_count",
                "waiting_count",
                "cancelled_count",
            ),
            0,
        )
        for state, error, count in rows:
            if state == "delivered":
                bucket = "delivered_count"
            elif error in {"telegram_forbidden", "telegram_rejected", "mail_rejected"}:
                bucket = "provider_rejected_count"
            elif state == "blocked":
                bucket = "blocked_count"
            elif state in {"pending", "in_flight"}:
                bucket = "waiting_count"
            elif state == "cancelled":
                bucket = "cancelled_count"
            else:
                bucket = "unknown_count"
            counts[bucket] += count
        counts["blocked_count"] += job.payload.get("blocked_before_enqueue", 0)
        missing = max(0, job.payload.get("recipient_count", 0) - sum(counts.values()))
        counts[
            "cancelled_count"
            if job.state == "stopped"
            else "unknown_count"
            if job.state == "done"
            else "waiting_count"
        ] += missing
        return {
            "id": job.id,
            "state": job.state,
            "recipient_count": job.payload.get("recipient_count", 0),
            **counts,
            "failed_count": counts["blocked_count"]
            + counts["provider_rejected_count"]
            + counts["unknown_count"],
            "attempts": job.attempts,
            "last_error": job.last_error,
            "nodes": job.payload["nodes"],
            "delivery": "durable_notifications",
            "report_state": job.payload.get("report_state", "pending"),
        }

    def report_completed(self, limit=10):
        from .panel import PanelUnavailable

        try:
            return self._report_completed(limit)
        except PanelUnavailable:
            # A busy canonical owner rolls back the whole report transaction;
            # the existing pending report remains available for the next tick.
            return 0

    def _report_completed(self, limit=10):
        """Фиксирует единственный отчёт владельцу после завершения первичных доставок."""
        emitted = 0
        with self.db.transaction() as session:
            self._admit(session, delivery=True)
            owner_id = str(getattr(self.settings, "owner_id", ""))
            owner_telegram = int(owner_id) if owner_id.isdigit() else None
            if self.live:
                from .panel_news import owner_account_in

                owner = owner_account_in(session, self.settings)
            else:
                owner = session.scalar(
                    select(Account)
                    .outerjoin(StaffRole, StaffRole.account_id == Account.id)
                    .where(
                        Account.telegram_id.in_(telegram_admin_ids(self.settings)),
                        or_(
                            Account.telegram_id == owner_telegram if owner_telegram is not None else False,
                            StaffRole.role == "owner",
                        ),
                    )
                    .order_by((Account.telegram_id == owner_telegram).desc().nulls_last(), Account.id)
                    .with_for_update(of=Account)
                    .execution_options(populate_existing=True)
                    .limit(1)
                )
                if owner and self.staff.role_in(session, owner) != "owner":
                    owner = None
            waiting = (
                select(NotificationDelivery.id)
                .join(Notification, Notification.id == NotificationDelivery.notification_id)
                .where(
                    Notification.event_key == "broadcast:" + Job.id,
                    NotificationDelivery.channel.in_(["telegram", "email"]),
                    NotificationDelivery.state.in_(["pending", "in_flight"]),
                )
                .exists()
            )
            jobs = session.scalars(
                select(Job)
                .where(
                    Job.kind == "broadcast",
                    Job.state.in_(["done", "stopped"]),
                    Job.payload["source"].as_string() == "unified",
                    Job.payload["report_state"].as_string().is_distinct_from("queued"),
                    ~waiting,
                )
                .order_by(Job.available_at, Job.id)
                .with_for_update(skip_locked=True)
                .limit(max(0, min(limit, 100)))
            )
            for job in jobs:
                if not self._job_scope(job):
                    continue
                self._admit(session, delivery=True)
                result = self._dto(session, job)
                if result["waiting_count"]:
                    continue
                if not owner:
                    job.payload = {**job.payload, "report_state": "owner_unavailable"}
                    continue
                body = "\n".join(
                    f"{label}: {result[key]}"
                    for key, label in (
                        ("recipient_count", "Получателей"),
                        ("delivered_count", "Доставлено"),
                        ("blocked_count", "Заблокировано правилами"),
                        ("provider_rejected_count", "Отклонено провайдером"),
                        ("unknown_count", "Неизвестный результат"),
                        ("waiting_count", "Ожидают"),
                        ("cancelled_count", "Отменено"),
                    )
                )
                self.notifications.emit_in(
                    session,
                    owner.id,
                    "broadcast-report:" + job.id,
                    "broadcast_report",
                    "Результаты рассылки",
                    body,
                    target="/admin#broadcasts",
                    owner_only=True,
                )
                job.payload = {**job.payload, "report_state": "queued"}
                emitted += 1
        return emitted

    def expand_pending(self, limit=10):
        expanded = 0
        with self.db.transaction() as session:
            self._admit(session, delivery=True)
            jobs = session.scalars(
                select(Job)
                .where(
                    Job.kind == "broadcast",
                    Job.state == "pending",
                    Job.payload["source"].as_string() == "unified",
                )
                .with_for_update(skip_locked=True)
                .limit(limit)
            )
            for job in jobs:
                if not self._job_scope(job):
                    continue
                self._admit(session, delivery=True)
                if self.live and not self._current_actor(session, job.account_id):
                    job.state, job.last_error = "review", "broadcast_permission_revoked"
                    continue
                if job.payload.get("media") is not None:
                    try:
                        self._media_in(
                            session,
                            job.account_id,
                            job.payload["media"],
                            job.payload["nodes"],
                            lock_actor=False,
                        )
                    except DomainError as exc:
                        job.state, job.last_error = "review", exc.code
                        continue
                blocked = 0
                for identifier in job.payload["account_ids"]:
                    account = session.get(Account, int(identifier))
                    if (
                        account
                        and self._allowed(account)
                        and (not self.live or session.get(Alias, account.id, populate_existing=True) is None)
                        and (self.live or self.commerce.legal.status_in(session, account)["accepted"])
                    ):
                        item = self.notifications.emit_in(
                            session,
                            account.id,
                            "broadcast:" + job.id,
                            "broadcast",
                            self._title,
                            self._nodes(job.payload["nodes"]),
                            nodes=job.payload["nodes"],
                            target="/#notifications",
                        )
                        if job.payload.get("media") is not None:
                            from .broadcast_media_registry import BroadcastMediaRegistry

                            BroadcastMediaRegistry(self.commerce, self.settings).bind_in(
                                session, item.id, job
                            )
                    else:
                        blocked += 1
                job.payload = {**job.payload, "blocked_before_enqueue": blocked}
                job.state, job.attempts = "done", job.attempts + 1
                expanded += 1
        return expanded

    def _media_in(self, session, actor, media, nodes, *, lock_actor=True):
        from .broadcast_media_registry import BroadcastMediaRegistry

        validate_telegram_content(self._title, self._nodes(nodes), nodes, max_units=1024)
        return BroadcastMediaRegistry(self.commerce, self.settings).reference_in(
            session, actor, media, lock_actor=lock_actor
        )

    def test_self(self, actor, nodes, reason, *, media=None):
        if self.live:
            raise DomainError(
                "test_operation_denied", "Тестовая отправка недоступна в рабочем окружении.", 403
            )
        self._nodes(nodes)
        self._reason(reason)
        with self.db.transaction() as session:
            account = self.staff.require_in(session, actor, "broadcasts_write", bot=True)
            if not account.email_verified or account.email not in self.settings.email_allowlist:
                raise DomainError("recipient_not_authorized", "Тестовая почта не разрешена.", 403)
            media = self._media_in(session, account.id, media, nodes) if media is not None else None
            job = Job(
                key="broadcast:" + uid(),
                kind="broadcast",
                account_id=account.id,
                state="done",
                payload={
                    "source": "media_self",
                    "nodes": nodes,
                    "account_ids": [str(account.id)],
                    "environment": "local_test",
                    **({"media": media} if media else {}),
                },
            )
            session.add(job)
            session.flush()
            item = self.notifications.emit_in(
                session,
                account.id,
                "broadcast:" + job.id,
                "broadcast",
                self._title,
                self._nodes(nodes),
                nodes=nodes,
                channel="email",
                allow_push=False,
            )
            if media is not None:
                from .broadcast_media_registry import BroadcastMediaRegistry

                BroadcastMediaRegistry(self.commerce, self.settings).bind_in(session, item.id, job)
            self._audit(session, account.id, job.id, "broadcast_create", reason)
            return {
                "id": job.id,
                "state": "pending",
                "recipient_count": 1,
                "delivery": "durable_notifications",
            }
