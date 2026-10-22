from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator
from sunrise_infrastructure.broadcast_media_registry import MediaReference
from sunrise_infrastructure.panel_event_routes import PanelEventRoutes


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


AUTOMATIC_AUDIT_REASON = "Подтверждённое действие через интерфейс"


class DeletionNoticeDismissRequest(Input):
    pass


class PaymentMethodsUpdate(Input):
    policy: dict
    reason: str = Field(default=AUTOMATIC_AUDIT_REASON, min_length=5, max_length=500)
    confirmed: StrictBool


class PanelEventRoutesUpdate(Input):
    policy: PanelEventRoutes
    reason: str = Field(default=AUTOMATIC_AUDIT_REASON, min_length=5, max_length=500)
    confirmed: StrictBool


class PanelEventRegistrationContext(BaseModel):
    version: Literal[1]
    environment: Literal["local_test", "sandbox", "live", "scoped_live"]
    panel_origin: str
    contract: Literal["3.4_id"]


class PanelEventRoutesResponse(BaseModel):
    configured: bool
    policy: PanelEventRoutes | None
    registration_context: PanelEventRegistrationContext | None
    approved_node_uuids: list[str]
    approved_product_ids: list[str]


class RemnaMembershipProbe(Input):
    scope_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    admission_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    resource_kind: Literal["user", "node"]
    resource_digest: str = Field(pattern=r"^[a-f0-9]{64}$")


class NodeNewsItemDto(BaseModel):
    id: str
    title: str
    state: Literal["outage", "recovered"]
    occurred_at: str


class NodeNewsDto(BaseModel):
    items: list[NodeNewsItemDto]
    next_cursor: str | None


class LegalDocument(Input):
    document_type: Literal["offer", "privacy"]
    version: str = Field(max_length=100)
    url: str = Field(max_length=2048)


class EmailRequest(Input):
    email: str = Field(max_length=320)
    turnstile_token: str = Field(max_length=2048)

    @field_validator("email")
    @classmethod
    def validate_email(cls, value):
        from email_validator import validate_email

        return validate_email(value, check_deliverability=False, test_environment=True).normalized.lower()


class ReferralInput(Input):
    referral_code: str | None = Field(default=None, max_length=23, pattern=r"^ref_[1-9][0-9]{0,18}$")

    @field_validator("referral_code")
    @classmethod
    def validate_referral(cls, value):
        from sunrise_infrastructure.referral_attribution import referral_id

        if value is not None and referral_id(value) is None:
            raise ValueError("Некорректный реферальный код.")
        return value


class CodeRequest(ReferralInput):
    challenge_id: str = Field(max_length=64)
    code: str = Field(pattern=r"^\d{6}$")
    legal_accepted: StrictBool = False
    documents: list[LegalDocument] = Field(default_factory=list, max_length=2)


class MfaRequest(Input):
    code: str = Field(pattern=r"^\d{6}$")


class LinkEmailRequest(Input):
    pass


class LinkEmailCodeRequest(Input):
    challenge_id: str = Field(min_length=1, max_length=64)
    code: str = Field(pattern=r"^[0-9]{6}$")


class LinkEmailChallengeDto(BaseModel):
    challenge_id: str
    resend_after: int
    expires_in: int


class LinkEmailVerificationDto(BaseModel):
    confirmed: bool
    expires_in: int


class TelegramConsent(ReferralInput):
    legal_accepted: StrictBool = False
    documents: list[LegalDocument] = Field(default_factory=list, max_length=2)


class MiniAppRequest(TelegramConsent):
    init_data: str = Field(min_length=1, max_length=16384)


class TelegramStartDto(BaseModel):
    authorization_url: str


class MfaStatusDto(BaseModel):
    enrolled: bool
    enrollment_required: bool
    trusted: bool
    trust_expires_at: str | None


class MfaEnrollmentDto(BaseModel):
    secret: str
    provisioning_uri: str
    expires_at: str


class QuoteRequest(Input):
    payment_method: Literal["crypto", "xrocket", "sbp"] | None = None
    product_id: str = Field(max_length=64)
    period_days: int = Field(ge=1, le=365)
    action: Literal["purchase", "renew", "upgrade", "downgrade", "devices"]
    subscription_id: str | None = Field(default=None, max_length=64)
    bonus_amount: str = Field(default="0.00", pattern=r"^\d{1,12}(\.\d{1,2})?$")
    credit_amount: str = Field(default="0.00", pattern=r"^[0-9]{1,12}(\.[0-9]{1,2})?$")
    extra_devices: int = Field(default=0, ge=0, le=20)
    promo_code: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,40}$")


class OrderRequest(Input):
    quote_id: str = Field(max_length=64)
    idempotency_key: str = Field(min_length=1, max_length=128)


class PromoActivationRequest(Input):
    code: str = Field(pattern=r"^[A-Za-z0-9_-]{1,40}$")


class PromoClaimRequest(PromoActivationRequest):
    subscription_id: str | None = Field(default=None, max_length=64)


class RecurringRequest(Input):
    plan_id: str = Field(min_length=1, max_length=128)
    confirmed: bool
    idempotency_key: str = Field(min_length=1, max_length=128)


class Reason(Input):
    reason: str = Field(default=AUTOMATIC_AUDIT_REASON, min_length=5, max_length=500)
    confirmed: bool


class ProviderOperationReviewRequest(Reason):
    ack_token: str = Field(pattern=r"^[a-f0-9]{64}$")
    confirmed: StrictBool


class NotificationDeliveryReviewRequest(ProviderOperationReviewRequest):
    pass


class ToggleRequest(Reason):
    enabled: bool


class RetentionUpdate(Reason):
    enabled: StrictBool
    days_after_expiry: Literal[7] = 7
    confirmed: StrictBool


class ExtraDeviceRule(Input):
    through_extra: int | None = Field(ge=1, le=100)
    price: str = Field(pattern=r"^[0-9]{1,8}\.[0-9]{2}$")


class ProductValues(Input):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=4000)
    price: str | None = Field(default=None, pattern=r"^\d{1,8}\.\d{2}$")
    included_devices: int | None = Field(default=None, ge=1, le=100)
    extra_device_price: str | None = Field(default=None, pattern=r"^\d{1,8}\.\d{2}$")
    available: bool | None = None
    internal_squad_uuid: str | None = Field(default=None, pattern=r"^[a-fA-F0-9-]{36}$")
    content: dict | None = None
    tier_level: int | None = Field(default=None, ge=1, le=1000)
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    semantic_role: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_]{1,64}$")
    visible_landing: bool | None = None
    visible_bot: bool | None = None
    sort_order: int | None = Field(default=None, ge=-10000, le=10000)
    highlighted: bool | None = None
    icon: dict | None = None
    base_period_days: int | None = Field(default=None, ge=1, le=730)
    periods: list[dict] | None = Field(default=None, max_length=20)
    eligibility: dict | None = None
    recurring_plan_mapping: dict[str, str] | None = None
    extra_device_rules: list[ExtraDeviceRule] | None = Field(default=None, max_length=20)


class ProductUpdate(ProductValues, Reason):
    pass


class ProductCreate(ProductValues, Reason):
    code: str = Field(pattern=r"^[a-z0-9_]{1,64}$")
    name: str = Field(min_length=1, max_length=200)
    tier_level: int = Field(ge=1, le=1000)
    price: str = Field(pattern=r"^\d{1,8}\.\d{2}$")
    included_devices: int = Field(ge=1, le=100)
    currency: str = Field(pattern=r"^[A-Z]{3}$")


class DiscountPreview(Input):
    product_id: str = Field(max_length=64)
    period_days: int = Field(ge=1, le=730)
    secondary_percent: str = Field(default="0", pattern=r"^\d{1,3}(\.\d{1,2})?$")
    bonus_amount: str = Field(default="0.00", pattern=r"^\d{1,12}(\.\d{1,2})?$")
    cap: str | None = Field(default=None, pattern=r"^\d{1,3}(\.\d{1,2})?$")
    bonus_cap: str | None = Field(default=None, pattern=r"^\d{1,3}(\.\d{1,2})?$")
    period_discount_percent: str | None = Field(default=None, pattern=r"^\d{1,3}(\.\d{1,2})?$")


class LinkRequest(Input):
    telegram_id: str | None = Field(default=None, pattern=r"^[1-9]\d{0,18}$")
    telegram_username: str | None = Field(default=None, pattern=r"^@?[A-Za-z0-9_]{5,32}$")

    @model_validator(mode="after")
    def single_identity(self):
        if (self.telegram_id is None) == (self.telegram_username is None):
            raise ValueError("Укажите ID или имя пользователя Telegram.")
        if self.telegram_id is not None and int(self.telegram_id) > 2**63 - 1:
            raise ValueError("Проверьте ID Telegram.")
        return self


class BonusAdjustment(Reason):
    amount: str = Field(pattern=r"^-?\d{1,7}\.\d{2}$")
    operation_id: str = Field(min_length=1, max_length=100)


class Compensation(Reason):
    days: int = Field(ge=1, le=365)


class CreditAdjustment(BonusAdjustment):
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    provenance: Literal["owner_adjustment", "refund_compensation", "verified_legacy"]
    confirm_existing_currency: bool = False


class ContentUpdate(Reason):
    nodes: list[dict] = Field(max_length=100)


class OrdinaryContentUpdate(ContentUpdate):
    reason: str = Field(default="Изменение текста или описания", min_length=5, max_length=500)


class RegistryUpdate(Reason):
    registry: dict[str, dict]


class PromoInput(Input):
    code: str = Field(pattern=r"^[A-Z0-9_-]{1,40}$")
    reward_type: Literal["percent", "fixed_rub", "free_days"] = "percent"
    percent: str = Field(default="0", pattern=r"^\d{1,3}(\.\d{1,2})?$")
    amount_rub: str | None = Field(default=None, pattern=r"^\d{1,8}\.\d{2}$")
    reward_days: int | None = Field(default=None, ge=1, le=3650)
    product_id: str | None = Field(default=None, max_length=64)
    enabled: bool = True
    audience: Literal["all", "first_purchase", "renewal"] = "all"
    per_account_limit: Literal[1] = 1
    total_limit: int | None = Field(default=None, ge=1)
    starts_at: str | None = None
    ends_at: str | None = None

    @model_validator(mode="after")
    def valid_reward(self):
        from sunrise_core.errors import DomainError
        from sunrise_infrastructure.promos import validate_promo

        try:
            validate_promo(self.model_dump())
        except DomainError as exc:
            raise ValueError(exc.message) from None
        return self


class LoyaltyMilestone(Input):
    elapsed_periods: int = Field(ge=1, le=120)
    percent: str = Field(pattern=r"^\d{1,3}(\.\d{1,2})?$")


class DiscountCampaign(Input):
    enabled: bool = False
    percent: str = Field(default="0", pattern=r"^\d{1,3}(\.\d{1,2})?$")


class DiscountsUpdate(Reason):
    cap: str = Field(pattern=r"^\d{1,3}(\.\d{1,2})?$")
    bonus_cap: str = Field(pattern=r"^\d{1,3}(\.\d{1,2})?$")
    referral_percent: str = Field(pattern=r"^\d{1,3}(\.\d{1,2})?$")
    promos: list[PromoInput] = Field(max_length=100)
    loyalty_period_days: int | None = Field(default=None, ge=1, le=365)
    loyalty_milestones: list[LoyaltyMilestone] | None = Field(default=None, max_length=20)
    renewal_campaign: DiscountCampaign | None = None
    near_expiry_renewal: DiscountCampaign | None = None
    referral_welcome: DiscountCampaign | None = None


class PeriodInput(Input):
    days: int = Field(ge=1, le=730)
    months: int = Field(ge=1, le=24)
    discount_percent: str = Field(pattern=r"^(100|\d{1,2})(\.\d{1,2})?$")


class PeriodsUpdate(Reason):
    periods: list[PeriodInput] = Field(min_length=1, max_length=20)


class BroadcastRequest(ContentUpdate):
    recipients: list[str] = Field(default_factory=list, max_length=100)
    media: MediaReference | None = None


class BroadcastEstimate(Input):
    nodes: list[dict] = Field(max_length=100)
    recipients: list[str] = Field(default_factory=list, max_length=100)
    media: MediaReference | None = None


class BroadcastSelfRequest(ContentUpdate):
    media: MediaReference | None = None


class AccountDto(BaseModel):
    id: str
    email: str | None
    telegram_id: str | None
    telegram_username: str | None = None
    unlink_blocked: bool = False
    email_verified: bool


class DeviceModerationRequest(Reason):
    action: Literal["warn", "block", "unblock", "remove_warning"]
    confirmed: bool
    idempotency_key: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")


class AuthDto(BaseModel):
    account: AccountDto
    csrf_token: str
    is_admin: bool
    mfa_verified: bool
    staff_role: Literal["owner", "admin"] | None = None
    capabilities: list[str] = Field(default_factory=list)


class IconDto(BaseModel):
    fallback: str
    custom_emoji_id: str | None = None


class ProductPeriodDto(BaseModel):
    days: int
    months: int | str
    discount_percent: str
    price: str


class EligibilityDto(BaseModel):
    allow_upgrade: bool
    allow_downgrade: bool


class ProductDto(BaseModel):
    id: str
    code: str
    name: str
    description: str
    tier_level: int
    price: str
    currency: str
    included_devices: int
    version: int
    available: bool
    extra_device_price: str | None
    content: dict = Field(default_factory=dict)
    semantic_role: str
    visible_landing: bool
    visible_bot: bool
    sort_order: int
    highlighted: bool
    icon: IconDto | None
    base_period_days: int | None
    periods: list[ProductPeriodDto]
    eligibility: EligibilityDto
    extra_device_rules: list[ExtraDeviceRule] = Field(default_factory=list)


class LegalDto(BaseModel):
    configured: bool
    documents: list[LegalDocument]
    revision: str


class LegalStatusDto(LegalDto):
    accepted: bool
    missing: list[str]
    history: list[dict]
    legacy_evidence: dict


class LegalAccept(Input):
    documents: list[LegalDocument] = Field(min_length=2, max_length=2)
    context: Literal["registration", "checkout", "first_use"]


class LegalUpdate(Reason):
    partial: bool = False
    expected_revision: str | None = Field(default=None, max_length=100)
    documents: list[LegalDocument] = Field(min_length=1, max_length=2)

    @model_validator(mode="after")
    def selected_document_count(self):
        if len(self.documents) != (1 if self.partial else 2):
            raise ValueError("Выберите один документ для отдельного сохранения или оба для общего.")
        return self


class PublicContentUpdate(Reason):
    reason: str = Field(default="Изменение публичного контента", min_length=5, max_length=500)
    faq: list[dict] = Field(max_length=30)
    how_to_connect: list[dict] = Field(max_length=30)
    promo_channels: list[dict] | None = Field(default=None, max_length=10)


class ThemeUpdate(Reason):
    theme: dict


class PanelMappingDto(BaseModel):
    external_squad_uuid: str | None
    project_prefix: str


class PanelMappingUpdate(PanelMappingDto, Reason):
    pass


class RefundPolicyUpdate(Reason):
    max_age_hours: int = Field(ge=0, le=24)
    max_traffic_bytes: int = Field(ge=0, le=100000000)


class RefundResolve(Reason):
    action: Literal["keep", "revoke", "shorten", "recalculate", "manual"]
    ends_at: str | None = None
    expected_subscription_version: int | None = Field(default=None, ge=1)


class PeriodDto(BaseModel):
    days: int
    months: int | str
    discount_percent: str


class CapabilitiesDto(BaseModel):
    payments: str
    recurring: bool
    dynamic_recurring_amount: bool
    provisioning: str


class PaymentMethodDto(BaseModel):
    id: Literal["crypto", "xrocket", "sbp"]
    label: str
    discount_percent: str
    available: bool
    unavailable_reason: str | None = None


class CatalogDto(BaseModel):
    payment_methods: list[PaymentMethodDto] = Field(default_factory=list)
    products: list[ProductDto]
    periods: list[PeriodDto]
    recurring_enrollment_enabled: bool
    capabilities: CapabilitiesDto
    content: dict = Field(default_factory=dict)
    legal: LegalDto


class DeviceDto(BaseModel):
    id: str
    name: str
    last_seen_at: str | None


class SegmentDto(BaseModel):
    id: str
    product_id: str
    starts_at: str
    ends_at: str
    devices: int
    paid_value: str


class RecurringDto(BaseModel):
    id: str
    state: str
    amount: str
    currency: str
    next_charge_at: str | None


class ConnectionLinkDto(BaseModel):
    text: str
    url: str


class ConnectionStepDto(BaseModel):
    title: str
    description: str
    links: list[ConnectionLinkDto]


class ConnectionAppDto(BaseModel):
    name: str
    steps: list[ConnectionStepDto]


class ConnectionPlatformDto(BaseModel):
    id: str
    name: str
    apps: list[ConnectionAppDto]


class ConnectionInstructionsDto(BaseModel):
    source: Literal["remnawave", "fallback"]
    platforms: list[ConnectionPlatformDto]
    notice: str


class SubscriptionDto(BaseModel):
    id: str
    connection_help_due: bool = False
    ever_connected: bool = False
    first_connected_at: str | None = None
    traffic_progress_percent: str | None = None
    traffic_warning: bool = False
    retention_state: Literal["none", "pending", "in_flight", "unknown", "review", "archived"] = "none"
    cleanup_at: str | None = None
    archived_at: str | None = None
    manual_renewal_discount_eligible: bool = False
    manual_renewal_discount_percent: str | None = None
    promo_claim_eligible: bool = False
    product_id: str | None
    name: str
    status: str
    expires_at: str | None
    devices_limit: int | None
    version: int
    connection_url: str | None
    devices: list[DeviceDto]
    segments: list[SegmentDto]
    recurring: RecurringDto | None
    panel_status: str | None = None
    observed_at: str | None = None
    traffic_bytes: str | None = None
    traffic_limit_bytes: str | None = None
    device_inventory_state: Literal["unknown", "fresh", "stale", "unavailable"] = "unknown"
    credential_rotation_state: str | None = None
    credential_rotation_job_id: str | None = None
    credential_rotation_cancelable: bool = False
    paid_horizon: str | None = None
    panel_sync_state: Literal["fresh", "stale", "unknown", "pending", "denied"] = "unknown"
    panel_sync_error: str | None = None
    panel_device_limit: int | None = None
    renewal_pricing_state: Literal["ready", "blocked"] = "blocked"
    renewal_pricing_error: (
        Literal[
            "panel_sync_required",
            "panel_access_restricted",
            "panel_product_unverified",
            "panel_capacity_unpriced",
        ]
        | None
    ) = None
    renewal_device_limit_required: bool = False
    panel_product_state: Literal["matched", "unknown", "ambiguous"] = "unknown"
    panel_product_id: str | None = None


class PaymentDto(BaseModel):
    id: str
    order_id: str
    amount: str
    currency: str
    status: str
    paid_at: str
    fee: str | None
    fee_currency: str | None = None
    environment: str
    product_name: str | None = None
    action: str | None = None
    payment_method: str | None = None
    period_days: int | None = None
    devices: int | None = None
    provisioning_status: str | None = None


class PendingPromoDto(BaseModel):
    code: str
    state: Literal["pending"]
    reward_type: Literal["percent", "fixed_rub", "free_days"] = "percent"
    percent: str = "0"
    amount_rub: str | None = None
    reward_days: int | None = None
    product_id: str | None = None
    available: bool = True
    unavailable_reason: str | None = None


class PromoClaimDto(BaseModel):
    code: str
    state: Literal["used"]
    subscription_id: str
    reward_days: int
    expires_at: str
    provisioning_state: str


class ReferralDto(BaseModel):
    referral_code: str
    referral_url: str | None
    website_referral_url: str | None = None
    reward_percent: str
    invited_count: int
    rewarded_amount: str
    currency: str
    enrollment_available: bool


class SubscriptionDeletionNoticeDto(BaseModel):
    subscription_id: str
    name: str
    deleted_at: str
    reason: Literal["manual_panel", "expiry_cleanup", "verified_panel_absence"]


class ArchivedSubscriptionAgreementDto(BaseModel):
    subscription_id: str
    name: str
    recurring: RecurringDto


class DashboardDto(BaseModel):
    account: AccountDto
    subscriptions: list[SubscriptionDto]
    subscription_deletion_notices: list[SubscriptionDeletionNoticeDto] = Field(default_factory=list)
    archived_subscription_agreements: list[ArchivedSubscriptionAgreementDto] = Field(default_factory=list)
    payments: list[PaymentDto]
    bonus_balance: str
    bonus_reserved: str
    credit_balance: str
    credit_reserved: str
    credit_available: str
    credit_currency: str | None
    credit_provenance: str
    referrer_id: str | None
    recurring_enrollment_enabled: bool
    active_promo: PendingPromoDto | None = None
    referral: ReferralDto | None = None


class BreakdownDto(BaseModel):
    label: str
    amount: str


class DevicePricingDto(BaseModel):
    amount: str
    unit_price: str | None
    extra_devices: int
    billing_months: int | str | None
    mode: Literal["fixed_remaining_term", "monthly_renewal"]


class QuoteDto(BaseModel):
    period_discount_amount: str = "0.00"
    secondary_discount_amount: str = "0.00"
    promo_discount_amount: str = "0.00"
    promo_reward_amount_rub: str | None = None
    payment_method: PaymentMethodDto | None = None
    payment_method_discount_amount: str = "0.00"
    minimum_subscription_amount: str | None = None
    minimum_external_required: bool = False
    period_days: int | None = None
    device_amount: str | None = None
    device_pricing: DevicePricingDto | None = None
    id: str
    action: str
    product_id: str
    subscription_id: str | None
    base_amount: str
    discount_amount: str
    bonus_amount: str
    credit_amount: str = "0.00"
    credit_currency: str | None = None
    total: str
    currency: str
    resulting_expires_at: str
    devices_limit: int
    expires_at: str
    version: int
    breakdown: list[BreakdownDto]
    promo_code: str | None = None
    discount_candidates: list[dict] = Field(default_factory=list)
    selected_secondary: dict | None = None


class OrderDto(BaseModel):
    payment_expires_at: str | None = None
    payment_method: PaymentMethodDto | None = None
    id: str
    status: str
    total: str
    currency: str
    checkout_url: str | None
    quote_id: str
    environment: str
    subscription_id: str | None
    provisioning_status: Literal["pending", "ready", "review"] | None = None


class ItemsDto(BaseModel):
    items: list[dict[str, Any]]
    total: int = 0
    page: int = 1
    page_size: int = 25


class InstanceDto(BaseModel):
    app_name: str
    app_short_name: str
    app_slug: str
    app_description: str
    visual_profile: Literal["neutral", "private"]
    search_indexing_enabled: bool = False
    accent_color: str
    public_site_url: str | None
    dashboard_url: str | None
    telegram_bot_username: str | None
    support_url: str | None
    support_email: str | None
    sender_name: str
    logo_url: str | None
    favicon_url: str | None
    hero_url: str | None
    og_image_url: str | None


class ConfigDto(BaseModel):
    instance: InstanceDto
    web_push_public_key: str | None = None
    smtp_email_enabled: bool = False
    dashboard_url: str | None
    bot_url: str | None
    support_url: str | None
    turnstile_site_key: str | None
    local_test: bool
    turnstile_test_bypass: bool
    telegram_miniapp_enabled: bool
    telegram_login_enabled: bool
