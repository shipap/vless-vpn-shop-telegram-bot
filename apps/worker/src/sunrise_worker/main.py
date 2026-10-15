import signal
from datetime import timedelta
from threading import Event

from redis.exceptions import LockError
from sqlalchemy import select
from sunrise_core.errors import DomainError
from sunrise_infrastructure.billing import Billing
from sunrise_infrastructure.broadcast_media_registry import configured_media_store
from sunrise_infrastructure.broadcasts import BroadcastService
from sunrise_infrastructure.commerce import Commerce
from sunrise_infrastructure.db import Database, Order, ProviderOperation, Quote, now
from sunrise_infrastructure.mail import configured_mail
from sunrise_infrastructure.notifications import NotificationService
from sunrise_infrastructure.operational_monitor import OperationalMonitor
from sunrise_infrastructure.panel import configured_panel
from sunrise_infrastructure.panel_contacts import PanelContactSync
from sunrise_infrastructure.panel_events import PanelEvents
from sunrise_infrastructure.providers import RollyPay
from sunrise_infrastructure.recurring import Recurring
from sunrise_infrastructure.scheduler import Scheduler
from sunrise_infrastructure.settings import Settings
from sunrise_infrastructure.subscription_archive import DeferredArchiveOwnerNotices


class Worker:
    def __init__(self, config, redis_client=None, provider=None, panel_provider=None, mail_transport=None):
        self.config = config
        self.db = Database(config.database_url)
        from sunrise_infrastructure.providers import configured_payments
        from sunrise_infrastructure.security import configured_redis
        from sunrise_infrastructure.settings import configured_runtime

        self.runtime_policy = configured_runtime(config, self.db)
        self.redis = configured_redis(config, self.runtime_policy, client=redis_client)
        self.commerce = Commerce(
            self.db,
            local_test=config.local_test,
            notification_dir=config.mail_dir + "/notifications",
            notification_allowlist=config.email_allowlist,
            telegram_notification_allowlist=frozenset(
                item.strip()
                for item in getattr(config, "test_telegram_recipients", "").split(",")
                if item.strip()
            ),
            panel_provider=panel_provider or configured_panel(config),
            mail_transport=mail_transport
            or configured_mail(
                config,
                media_store=configured_media_store(config)
                if config.smtp_test_authorized or self.runtime_policy.production
                else None,
            ),
            device_bindings=config.panel_test_device_bindings,
            sandbox_payments=config.sandbox_payments_enabled,
            owner_id=config.owner_id,
            public_settings=config,
        )
        self.provider = provider or configured_payments(config, self.runtime_policy)
        self.notifications = NotificationService(self.db, config, mail_transport=self.commerce.mail_transport)
        self.scheduler = Scheduler(self.commerce, config)
        self.broadcasts = BroadcastService(self.commerce, config)
        self.operational_monitor = OperationalMonitor(self.db, config)
        self.panel_events = PanelEvents(
            self.db, self.commerce.panel_provider, config, notifications=self.notifications
        )
        from sunrise_infrastructure.subscription_engagement import SubscriptionEngagement

        self.engagement = SubscriptionEngagement(self.commerce, self.notifications)
        self.archive_owner_notices = DeferredArchiveOwnerNotices(self.commerce, self.notifications)
        self.panel_contacts = PanelContactSync(self.commerce)
        self._next_reminders = now()
        self._next_route_sync = now()
        self._news_cleanup_cursor = None

    def tick(self):
        self.runtime_policy.require()
        if getattr(self.runtime_policy, "scoped_payments", False):
            return self._scoped_tick()
        # Only the scheduler needs leadership; durable financial mutations also enforce row locks/uniqueness.
        lock = self.redis.lock("sunrise:worker:leader", timeout=30, blocking_timeout=0)
        if not lock.acquire(blocking=False):
            return 0
        try:
            completed = self.broadcasts.expand_pending() if self._stage("broadcast_delivery") else 0
            if getattr(self.commerce.panel_provider, "contract", None) in {"3.4_id", "2.8_uuid"}:
                completed += self.panel_events.reconcile_pending()
            completed += self.commerce.run_jobs()
            if self.runtime_policy.production and self._stage("panel_mutation"):
                completed += self.panel_contacts.run_pending()
            self.scheduler.notify_ready()
            daily_due = now() >= self._next_reminders
            if daily_due:
                if self.runtime_policy.production and self.commerce.panel_provider:
                    from sunrise_infrastructure.panel_news import cleanup_node_news

                    removed, self._news_cleanup_cursor = cleanup_node_news(
                        self.db,
                        self.commerce.panel_provider,
                        self.config,
                        after=self._news_cleanup_cursor,
                    )
                    completed += removed
                if self.runtime_policy.production:
                    completed += self.archive_owner_notices.run_pending()
                self.commerce.retention.schedule_due()
                self.scheduler.remind()
                if not self.runtime_policy.production:
                    self.scheduler.schedule()
                self.operational_monitor.notify_owner()
                self._next_reminders = now() + timedelta(minutes=1)
            completed += self.commerce.retention.run_jobs() if self._stage("retention") else 0
            completed += self.engagement.run_due()
            if not self.runtime_policy.production:
                completed += self.scheduler.deliver_backups()
            completed += self.notifications.deliver_pending()
            completed += self.broadcasts.report_completed() if self._stage("broadcast_delivery") else 0
            if daily_due and self.runtime_policy.production and self.commerce.panel_provider:
                completed += self.scheduler.sync_panel_routes()
        finally:
            try:
                lock.release()
            except LockError:
                pass
        if self.provider:
            environment = self.runtime_policy.payment_environment
            billing = Billing(self.commerce, self.provider.terminal, environment)
            with self.db.transaction() as s:
                ids = list(
                    s.scalars(
                        select(Order.id)
                        .join(Quote, Quote.id == Order.quote_id)
                        .where(
                            Order.environment == environment,
                            Order.status == "awaiting_payment",
                            Quote.expires_at > now(),
                            ~select(ProviderOperation.id)
                            .where(ProviderOperation.order_id == Order.id)
                            .exists(),
                        )
                        .order_by(Order.created_at, Order.id)
                        .limit(10)
                    )
                )
            for oid in ids:
                try:
                    billing.prepare_payment(oid, self.provider)
                except DomainError:
                    pass
            billing.poll_known_payments(limit=10)
            if self._stage("payment_reconcile"):
                completed += billing.recover_unknown_invoices(self.provider)
                completed += billing.reconcile_inbox(self.provider, limit=10)
                completed += billing.refresh_missing_settlements(
                    self.provider,
                    interval_seconds=self.config.payment_settlement_interval_seconds,
                    limit=self.config.payment_settlement_batch_size,
                )
            # Enrollment OFF never suppresses status, paid-cycle or cancellation recovery.
            if (
                not isinstance(self.provider, RollyPay)
                or self.provider.recurring_authorized
                or self.runtime_policy.production
            ):
                Recurring(self.commerce, self.provider).reconcile(limit=10)
        return completed

    def _scoped_tick(self):
        """Счета, собственная выдача и уведомления по оплаченным заказам тестового контура."""
        if self.provider is None:
            return 0
        lock = self.redis.lock("sunrise:scoped-test:worker:leader", timeout=30, blocking_timeout=0)
        if not lock.acquire(blocking=False):
            return 0
        try:
            billing = Billing(self.commerce, self.provider.terminal, "scoped_live")
            with self.db.transaction() as session:
                ids = list(
                    session.scalars(
                        select(Order.id)
                        .join(Quote, Quote.id == Order.quote_id)
                        .where(
                            Order.environment == "scoped_live",
                            Order.status == "awaiting_payment",
                            Quote.expires_at > now(),
                            ~select(ProviderOperation.id)
                            .where(ProviderOperation.order_id == Order.id)
                            .exists(),
                        )
                        .order_by(Order.created_at, Order.id)
                        .limit(10)
                    )
                )
            for order_id in ids:
                try:
                    billing.prepare_payment(order_id, self.provider)
                except DomainError:
                    pass
            billing.poll_known_payments(limit=10)
            completed = billing.reconcile_inbox(self.provider, limit=10)
            completed += billing.refresh_missing_settlements(
                self.provider,
                interval_seconds=self.config.payment_settlement_interval_seconds,
                limit=self.config.payment_settlement_batch_size,
            )
            if self.commerce.panel_provider:
                completed += self.commerce.provisioning.run_jobs(limit=10)
                completed += self.panel_contacts.run_pending(limit=10)
                completed += self.panel_events.reconcile_pending(limit=10)
                completed += self.engagement.run_due(limit=10)
            self.scheduler.notify_ready(scoped_only=True)
            completed += self.notifications.deliver_pending(scoped_only=True)
            if self.commerce.panel_provider and now() >= self._next_route_sync:
                from sunrise_infrastructure.panel_news import cleanup_node_news

                removed, self._news_cleanup_cursor = cleanup_node_news(
                    self.db,
                    self.commerce.panel_provider,
                    self.config,
                    after=self._news_cleanup_cursor,
                )
                completed += removed
                completed += self.scheduler.sync_panel_routes()
                self._next_route_sync = now() + timedelta(minutes=1)
            return completed
        finally:
            try:
                lock.release()
            except LockError:
                pass

    def _stage(self, capability):
        if not self.runtime_policy.production:
            return True
        try:
            self.runtime_policy.require(capability)
            return True
        except DomainError as failure:
            if failure.code != "production_stage_denied":
                raise
            return False


def main():
    worker = Worker(Settings())
    stopped = Event()
    previous = {
        kind: signal.signal(kind, lambda *_: stopped.set()) for kind in (signal.SIGTERM, signal.SIGINT)
    }
    try:
        while not stopped.is_set():
            worker.tick()
            stopped.wait(2)
    finally:
        for kind, handler in previous.items():
            signal.signal(kind, handler)
        worker.redis.close()
        worker.db.engine.dispose()


if __name__ == "__main__":
    main()
