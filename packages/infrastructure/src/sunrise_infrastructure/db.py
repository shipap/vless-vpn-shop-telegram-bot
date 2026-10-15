from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


def now():
    return datetime.now(UTC)


def uid():
    return str(uuid4())


class Base(DeclarativeBase):
    pass


class Account(Base):
    __tablename__ = "v5_accounts"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    email: Mapped[str | None] = mapped_column(String(320), unique=True)
    email_verified: Mapped[bool] = mapped_column(default=False)
    telegram_id: Mapped[int | None] = mapped_column(BigInteger, unique=True)
    telegram_username: Mapped[str | None] = mapped_column(String(32), index=True)
    telegram_notifications_blocked: Mapped[bool] = mapped_column(default=False)
    unlink_warnings: Mapped[int] = mapped_column(Integer, default=0)
    unlink_warnings_total: Mapped[int] = mapped_column(Integer, default=0)
    unlink_blocked: Mapped[bool] = mapped_column(default=False)
    password_hash: Mapped[str | None] = mapped_column(Text)
    credit_balance: Mapped[Decimal] = mapped_column(Numeric(24, 6), default=0)
    credit_reserved: Mapped[Decimal] = mapped_column(Numeric(24, 6), default=0)
    credit_currency: Mapped[str | None] = mapped_column(String(3))
    credit_provenance: Mapped[str] = mapped_column(String(40), default="unverified")
    bonus_balance: Mapped[Decimal] = mapped_column(Numeric(24, 6), default=0)
    bonus_reserved: Mapped[Decimal] = mapped_column(Numeric(24, 6), default=0)
    referrer_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    legacy_consent: Mapped[dict] = mapped_column(JSON, default=dict)
    pending_promo: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class LegalConsent(Base):
    __tablename__ = "v6_legal_consents"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=uid)
    account_id: Mapped[int] = mapped_column(ForeignKey(Account.id), index=True)
    original_account_id: Mapped[int] = mapped_column(BigInteger)
    document_type: Mapped[str] = mapped_column(String(20))
    document_version: Mapped[str] = mapped_column(String(100))
    document_url: Mapped[str] = mapped_column(Text)
    accepted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    source: Mapped[str] = mapped_column(String(20))
    context: Mapped[str] = mapped_column(String(40))
    __table_args__ = (
        CheckConstraint("document_type IN ('offer', 'privacy') AND source IN ('website', 'telegram')"),
    )


class Alias(Base):
    __tablename__ = "v5_account_aliases"
    old_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    canonical_id: Mapped[int] = mapped_column(ForeignKey(Account.id))


class Product(Base):
    __tablename__ = "v5_products"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=uid)
    code: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    tier_level: Mapped[int] = mapped_column(Integer)
    price: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    currency: Mapped[str] = mapped_column(String(3))
    included_devices: Mapped[int] = mapped_column(Integer)
    extra_device_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    internal_squad_uuid: Mapped[str | None] = mapped_column(String(36))
    available: Mapped[bool] = mapped_column(default=True)
    version: Mapped[int] = mapped_column(default=1)
    content: Mapped[dict] = mapped_column(JSON, default=dict)
    presentation: Mapped[dict] = mapped_column(JSON, default=dict)
    __table_args__ = (CheckConstraint("price > 0 AND included_devices > 0"),)


class Setting(Base):
    __tablename__ = "v5_settings"
    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON)


class Subscription(Base):
    __tablename__ = "v5_subscriptions"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=uid)
    account_id: Mapped[int] = mapped_column(ForeignKey(Account.id), index=True)
    product_id: Mapped[str | None] = mapped_column(ForeignKey(Product.id))
    name: Mapped[str] = mapped_column(String(200), default="Моя подписка")
    status: Mapped[str] = mapped_column(String(40), default="provisioning_pending")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    devices_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    panel_uuid: Mapped[str | None] = mapped_column(String(64), unique=True)
    panel_user_id: Mapped[int | None] = mapped_column(BigInteger, unique=True)
    short_uuid: Mapped[str | None] = mapped_column(String(200), unique=True)
    connection_url: Mapped[str | None] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, default=1)
    environment: Mapped[str] = mapped_column(String(30), default="local_test")


class Segment(Base):
    __tablename__ = "v5_segments"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=uid)
    subscription_id: Mapped[str] = mapped_column(ForeignKey(Subscription.id), index=True)
    order_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    product_id: Mapped[str] = mapped_column(ForeignKey(Product.id))
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    devices: Mapped[int] = mapped_column(Integer)
    paid_value: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    price_basis: Mapped[dict] = mapped_column(JSON)
    kind: Mapped[str] = mapped_column(String(30), default="paid")


class Quote(Base):
    __tablename__ = "v5_quotes"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=uid)
    account_id: Mapped[int] = mapped_column(ForeignKey(Account.id), index=True)
    subscription_id: Mapped[str | None] = mapped_column(ForeignKey(Subscription.id))
    snapshot: Mapped[dict] = mapped_column(JSON)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Order(Base):
    __tablename__ = "v5_orders"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=uid)
    account_id: Mapped[int] = mapped_column(ForeignKey(Account.id), index=True)
    quote_id: Mapped[str] = mapped_column(ForeignKey(Quote.id), unique=True)
    idempotency_key: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(40), default="awaiting_payment")
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    currency: Mapped[str] = mapped_column(String(3))
    source: Mapped[str] = mapped_column(String(20), default="site")
    environment: Mapped[str] = mapped_column(String(30), default="local_test")
    subscription_id: Mapped[str | None] = mapped_column(ForeignKey(Subscription.id))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    fulfilled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint("account_id", "idempotency_key"),)


class Payment(Base):
    __tablename__ = "v5_payments"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=uid)
    provider: Mapped[str] = mapped_column(String(32))
    provider_id: Mapped[str] = mapped_column(String(128))
    terminal: Mapped[str] = mapped_column(String(64), default="synthetic")
    environment: Mapped[str] = mapped_column(String(30))
    order_id: Mapped[str] = mapped_column(ForeignKey(Order.id))
    account_id: Mapped[int] = mapped_column(ForeignKey(Account.id), index=True)
    original_account_id: Mapped[int | None] = mapped_column(BigInteger)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    currency: Mapped[str] = mapped_column(String(3))
    fee: Mapped[Decimal | None] = mapped_column(Numeric(24, 8))
    fee_currency: Mapped[str | None] = mapped_column(String(5))
    net_usdt: Mapped[Decimal | None] = mapped_column(Numeric(24, 8))
    status: Mapped[str] = mapped_column(String(32), default="paid")
    paid_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    __table_args__ = (UniqueConstraint("provider", "terminal", "environment", "provider_id"),)


class Settlement(Base):
    __tablename__ = "v5_settlements"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=uid)
    payment_id: Mapped[str] = mapped_column(ForeignKey(Payment.id), index=True)
    operation_id: Mapped[str] = mapped_column(String(200), unique=True)
    fee: Mapped[Decimal | None] = mapped_column(Numeric(24, 8))
    fee_currency: Mapped[str | None] = mapped_column(String(5))
    net_usdt: Mapped[Decimal | None] = mapped_column(Numeric(24, 8))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    source: Mapped[str] = mapped_column(String(32), default="provider_confirmed")
    time_source: Mapped[str] = mapped_column(String(32), default="provider_confirmed")


class ProviderOperation(Base):
    __tablename__ = "v5_provider_operations"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=uid)
    order_id: Mapped[str] = mapped_column(ForeignKey(Order.id), unique=True)
    provider_id: Mapped[str | None] = mapped_column(String(128), unique=True)
    terminal: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(32), default="prepared")
    request: Mapped[dict] = mapped_column(JSON)
    pay_url: Mapped[str | None] = mapped_column(Text)


class Refund(Base):
    __tablename__ = "v5_refunds"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=uid)
    payment_id: Mapped[str] = mapped_column(ForeignKey(Payment.id))
    operation_id: Mapped[str] = mapped_column(String(128), unique=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class RefundReview(Base):
    __tablename__ = "v6_refund_reviews"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=uid)
    refund_id: Mapped[str] = mapped_column(ForeignKey(Refund.id), unique=True)
    payment_id: Mapped[str] = mapped_column(ForeignKey(Payment.id), index=True)
    segment_id: Mapped[str | None] = mapped_column(ForeignKey(Segment.id))
    state: Mapped[str] = mapped_column(String(32), default="pending")
    evidence: Mapped[dict] = mapped_column(JSON, default=dict)
    resolution: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Ledger(Base):
    __tablename__ = "v5_bonus_ledger"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=uid)
    account_id: Mapped[int] = mapped_column(ForeignKey(Account.id), index=True)
    operation: Mapped[str] = mapped_column(String(200), unique=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(24, 6))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Job(Base):
    __tablename__ = "v5_jobs"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=uid)
    key: Mapped[str] = mapped_column(String(200), unique=True)
    kind: Mapped[str] = mapped_column(String(40))
    account_id: Mapped[int | None] = mapped_column(ForeignKey(Account.id))
    payload: Mapped[dict] = mapped_column(JSON)
    state: Mapped[str] = mapped_column(String(32), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    last_error: Mapped[str | None] = mapped_column(String(100))


class CreditLedger(Base):
    __tablename__ = "v5_credit_ledger"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=uid)
    account_id: Mapped[int] = mapped_column(ForeignKey(Account.id), index=True)
    original_account_id: Mapped[int] = mapped_column(BigInteger)
    operation: Mapped[str] = mapped_column(String(200), unique=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(24, 6))
    currency: Mapped[str] = mapped_column(String(3))
    provenance: Mapped[str] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Inbox(Base):
    __tablename__ = "v5_payment_inbox"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=uid)
    event_key: Mapped[str] = mapped_column(String(200), unique=True)
    payload: Mapped[dict] = mapped_column(JSON)
    state: Mapped[str] = mapped_column(String(32), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    last_error: Mapped[str | None] = mapped_column(String(100))


class Agreement(Base):
    __tablename__ = "v5_agreements"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=uid)
    account_id: Mapped[int] = mapped_column(ForeignKey(Account.id))
    subscription_id: Mapped[str] = mapped_column(ForeignKey(Subscription.id))
    provider_id: Mapped[str | None] = mapped_column(String(128), unique=True)
    state: Mapped[str] = mapped_column(String(32), default="consent_pending")
    consent_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    currency: Mapped[str] = mapped_column(String(3))
    consent: Mapped[dict] = mapped_column(JSON, default=dict)
    next_charge_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    __table_args__ = (
        Index(
            "v5_agreements_active_subscription",
            "subscription_id",
            unique=True,
            postgresql_where=text("state <> 'stopped'"),
        ),
    )


class Device(Base):
    __tablename__ = "v5_devices"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=uid)
    subscription_id: Mapped[str] = mapped_column(ForeignKey(Subscription.id))
    name: Mapped[str] = mapped_column(String(200))
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked: Mapped[bool] = mapped_column(default=False)
    panel_hwid: Mapped[str | None] = mapped_column(String(512))
    generation: Mapped[int] = mapped_column(Integer, default=1)
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    platform: Mapped[str | None] = mapped_column(String(100))
    __table_args__ = (UniqueConstraint("subscription_id", "panel_hwid"),)


class DeviceUnlink(Base):
    __tablename__ = "v9_device_unlinks"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=uid)
    account_id: Mapped[int] = mapped_column(ForeignKey(Account.id), index=True)
    original_account_id: Mapped[int] = mapped_column(BigInteger)
    subscription_id: Mapped[str] = mapped_column(ForeignKey(Subscription.id))
    device_id: Mapped[str] = mapped_column(ForeignKey(Device.id))
    generation: Mapped[int] = mapped_column(Integer, default=1)
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    unlink_mode: Mapped[str | None] = mapped_column(String(16))
    unlink_operation: Mapped[str | None] = mapped_column(String(200))
    device_name: Mapped[str | None] = mapped_column(String(200))
    __table_args__ = (
        UniqueConstraint("device_id", "generation"),
        CheckConstraint(
            "unlink_mode IS NULL OR (unlink_mode IN ('single', 'bulk') AND unlink_operation IS NOT NULL)",
            name="ck_device_unlink_provenance",
        ),
    )


class Session(Base):
    __tablename__ = "v5_sessions"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey(Account.id), index=True)
    csrf: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    confirmed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    email_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    mfa_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    public_id: Mapped[str | None] = mapped_column(String(64), unique=True, default=uid)
    auth_method: Mapped[str] = mapped_column(String(32), default="unknown")
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=now)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ip_address: Mapped[str | None] = mapped_column(String(64))
    browser_label: Mapped[str | None] = mapped_column(String(200))


class StaffRole(Base):
    __tablename__ = "v11_staff_roles"
    account_id: Mapped[int] = mapped_column(ForeignKey(Account.id), primary_key=True)
    role: Mapped[str] = mapped_column(String(20))
    granted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    granted_by: Mapped[int | None] = mapped_column(BigInteger)
    __table_args__ = (CheckConstraint("role IN ('owner', 'admin')"),)


class AccountIdentityOperation(Base):
    __tablename__ = "v11_identity_operations"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=uid)
    account_id: Mapped[int] = mapped_column(ForeignKey(Account.id), index=True)
    session_id: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(32))
    state: Mapped[str] = mapped_column(String(32), default="pending")
    old_email: Mapped[str | None] = mapped_column(String(320))
    old_telegram_id: Mapped[int | None] = mapped_column(BigInteger)
    new_email: Mapped[str | None] = mapped_column(String(320))
    secret_hash: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)


class Notification(Base):
    __tablename__ = "v11_notifications"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=uid)
    account_id: Mapped[int] = mapped_column(ForeignKey(Account.id), index=True)
    event_key: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(String(200))
    audience: Mapped[str] = mapped_column(String(20), default="account", server_default="account")
    body: Mapped[str] = mapped_column(Text)
    severity: Mapped[str] = mapped_column(String(20), default="info")
    target: Mapped[str | None] = mapped_column(String(300))
    nodes: Mapped[list] = mapped_column(JSON, default=list)
    actions: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint("account_id", "event_key"),)


class NotificationPreference(Base):
    __tablename__ = "v11_notification_preferences"
    account_id: Mapped[int] = mapped_column(ForeignKey(Account.id), primary_key=True)
    browser_enabled: Mapped[bool] = mapped_column(default=False)


class NotificationDelivery(Base):
    __tablename__ = "v11_notification_deliveries"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=uid)
    notification_id: Mapped[str] = mapped_column(ForeignKey(Notification.id), index=True)
    channel: Mapped[str] = mapped_column(String(20))
    recipient: Mapped[str] = mapped_column(String(512))
    state: Mapped[str] = mapped_column(String(32), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(100))
    __table_args__ = (UniqueConstraint("notification_id", "channel", "recipient"),)


class PushSubscription(Base):
    __tablename__ = "v11_push_subscriptions"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=uid)
    account_id: Mapped[int] = mapped_column(ForeignKey(Account.id), index=True)
    endpoint_digest: Mapped[str] = mapped_column(String(64), unique=True)
    subscription_ciphertext: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PanelEvent(Base):
    __tablename__ = "v11_panel_events"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=uid)
    event_key: Mapped[str] = mapped_column(String(200), unique=True)
    kind: Mapped[str] = mapped_column(String(100))
    panel_user_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    subscription_id: Mapped[str | None] = mapped_column(ForeignKey(Subscription.id), index=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    state: Mapped[str] = mapped_column(String(32), default="pending")
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(String(100))


class PanelObservation(Base):
    __tablename__ = "v11_panel_observations"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=uid)
    subscription_id: Mapped[str | None] = mapped_column(ForeignKey(Subscription.id), index=True)
    kind: Mapped[str] = mapped_column(String(20), default="user")
    resource_id: Mapped[str] = mapped_column(String(100))
    status: Mapped[str | None] = mapped_column(String(40))
    traffic_bytes: Mapped[int | None] = mapped_column(BigInteger)
    traffic_limit_bytes: Mapped[int | None] = mapped_column(BigInteger)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    last_error: Mapped[str | None] = mapped_column(String(100))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    __table_args__ = (UniqueConstraint("kind", "resource_id"),)


class CampaignVisit(Base):
    __tablename__ = "v11_campaign_visits"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=uid)
    campaign_id: Mapped[str] = mapped_column(String(100), index=True)
    visitor_hash: Mapped[str] = mapped_column(String(64))
    secret_hash: Mapped[str] = mapped_column(String(64), unique=True)
    account_id: Mapped[int | None] = mapped_column(ForeignKey(Account.id), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    __table_args__ = (UniqueConstraint("campaign_id", "visitor_hash"),)


class AdminAuthenticator(Base):
    __tablename__ = "v10_admin_authenticators"
    account_id: Mapped[int] = mapped_column(ForeignKey(Account.id), primary_key=True)
    secret_ciphertext: Mapped[str | None] = mapped_column(Text)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pending_ciphertext: Mapped[str | None] = mapped_column(Text)
    pending_session_id: Mapped[str | None] = mapped_column(String(64))
    pending_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_totp_step: Mapped[int | None] = mapped_column(BigInteger)
    trusted_ip_digest: Mapped[str | None] = mapped_column(String(64))
    trust_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        CheckConstraint("(secret_ciphertext IS NULL) = (activated_at IS NULL)", name="ck_mfa_active"),
        CheckConstraint(
            "(pending_ciphertext IS NULL AND pending_session_id IS NULL AND pending_expires_at IS NULL) "
            "OR (pending_ciphertext IS NOT NULL AND pending_session_id IS NOT NULL AND pending_expires_at IS NOT NULL)",
            name="ck_mfa_pending",
        ),
        CheckConstraint("(trusted_ip_digest IS NULL) = (trust_verified_at IS NULL)", name="ck_mfa_trust"),
    )


class Challenge(Base):
    __tablename__ = "v5_challenges"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=uid)
    email: Mapped[str] = mapped_column(String(320))
    digest: Mapped[str] = mapped_column(String(64))
    context: Mapped[str] = mapped_column(String(128))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used: Mapped[bool] = mapped_column(default=False)


class Audit(Base):
    __tablename__ = "v5_audit"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=uid)
    actor_id: Mapped[int | None] = mapped_column(BigInteger)
    action: Mapped[str] = mapped_column(String(80))
    target: Mapped[str] = mapped_column(String(200))
    reason: Mapped[str] = mapped_column(String(500))
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Block(Base):
    __tablename__ = "v5_security_blocks"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=uid)
    context: Mapped[str] = mapped_column(String(128), index=True)
    rule: Mapped[str] = mapped_column(String(64))
    count: Mapped[int] = mapped_column(Integer)
    threshold: Mapped[int] = mapped_column(Integer)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MergeProof(Base):
    __tablename__ = "v5_merge_proofs"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=uid)
    source_id: Mapped[int] = mapped_column(ForeignKey(Account.id))
    target_id: Mapped[int] = mapped_column(ForeignKey(Account.id))
    secret_hash: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used: Mapped[bool] = mapped_column(default=False)


class LegacyIssue(Base):
    __tablename__ = "v5_legacy_issues"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=uid)
    source_table: Mapped[str] = mapped_column(String(80))
    source_id: Mapped[str] = mapped_column(String(128))
    reason: Mapped[str] = mapped_column(String(100))
    __table_args__ = (UniqueConstraint("source_table", "source_id", "reason"),)


class PromoUse(Base):
    __tablename__ = "v5_promo_uses"
    order_id: Mapped[str] = mapped_column(ForeignKey(Order.id), primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey(Account.id), index=True)
    code: Mapped[str] = mapped_column(String(40), index=True)
    state: Mapped[str] = mapped_column(String(20), default="reserved")


class PromoClaim(Base):
    __tablename__ = "v6_promo_claims"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey(Account.id), index=True)
    code: Mapped[str] = mapped_column(String(40), index=True)
    segment_id: Mapped[str] = mapped_column(ForeignKey(Segment.id), unique=True)
    result: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    __table_args__ = (UniqueConstraint("account_id", "code"),)


class RecurringCycle(Base):
    __tablename__ = "v5_recurring_cycles"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=uid)
    agreement_id: Mapped[str] = mapped_column(ForeignKey(Agreement.id))
    cycle_number: Mapped[int] = mapped_column(Integer)
    order_id: Mapped[str] = mapped_column(ForeignKey(Order.id), unique=True)
    provider_payment_id: Mapped[str] = mapped_column(String(128), unique=True)
    state: Mapped[str] = mapped_column(String(30), default="pending")
    __table_args__ = (UniqueConstraint("agreement_id", "cycle_number"),)


class LegacyPurchase(Base):
    __tablename__ = "v5_legacy_purchases"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    account_id: Mapped[int | None] = mapped_column(ForeignKey(Account.id), index=True)
    original_account_id: Mapped[int] = mapped_column(BigInteger)
    amount: Mapped[Decimal] = mapped_column(Numeric(24, 6))
    status: Mapped[str] = mapped_column(String(40))
    item_name: Mapped[str] = mapped_column(Text)
    raw_date: Mapped[str | None] = mapped_column(String(64))
    provider: Mapped[str | None] = mapped_column(String(80))
    invoice_id: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(80), default="legacy_unverified_accounting")


class RecurringAttempt(Base):
    __tablename__ = "v5_recurring_attempts"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    agreement_id: Mapped[str] = mapped_column(ForeignKey(Agreement.id), index=True)
    cycle_number: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(32))
    occurred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Database:
    def __init__(self, url):
        self.engine = create_engine(url, pool_pre_ping=True, hide_parameters=True)
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)

    @contextmanager
    def transaction(self):
        with self.sessions() as session, session.begin():
            yield session
