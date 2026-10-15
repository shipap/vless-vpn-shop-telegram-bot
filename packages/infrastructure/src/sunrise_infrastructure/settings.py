import hashlib
import json
import re
from functools import cached_property
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SUNRISE_",
        env_file=".env",
        extra="ignore",
        hide_input_in_errors=True,
        populate_by_name=True,
    )
    instance_config_path: str | None = Field(
        default="config/instance.json",
        validation_alias=AliasChoices("INSTANCE_CONFIG_PATH", "SUNRISE_INSTANCE_CONFIG_PATH"),
    )
    app_name: str | None = Field(default=None, validation_alias=AliasChoices("APP_NAME", "SUNRISE_APP_NAME"))
    app_short_name: str | None = Field(
        default=None, validation_alias=AliasChoices("APP_SHORT_NAME", "SUNRISE_APP_SHORT_NAME")
    )
    app_slug: str | None = Field(default=None, validation_alias=AliasChoices("APP_SLUG", "SUNRISE_APP_SLUG"))
    app_description: str | None = Field(
        default=None, validation_alias=AliasChoices("APP_DESCRIPTION", "SUNRISE_APP_DESCRIPTION")
    )
    visual_profile: str | None = Field(
        default=None, validation_alias=AliasChoices("VISUAL_PROFILE", "SUNRISE_VISUAL_PROFILE")
    )
    accent_color: str | None = Field(
        default=None, validation_alias=AliasChoices("ACCENT_COLOR", "SUNRISE_ACCENT_COLOR")
    )
    search_indexing_enabled: bool | None = Field(
        default=None,
        validation_alias=AliasChoices("SEARCH_INDEXING_ENABLED", "SUNRISE_SEARCH_INDEXING_ENABLED"),
    )
    telegram_bot_username: str | None = Field(
        default=None, validation_alias=AliasChoices("TELEGRAM_BOT_USERNAME", "SUNRISE_TELEGRAM_BOT_USERNAME")
    )
    telegram_menu_emoji_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("TELEGRAM_MENU_EMOJI_KEY", "SUNRISE_TELEGRAM_MENU_EMOJI_KEY"),
    )
    telegram_custom_emoji_verified: bool = False
    sender_name: str | None = Field(
        default=None, validation_alias=AliasChoices("SENDER_NAME", "SUNRISE_SENDER_NAME")
    )
    instance_asset_root: str | None = Field(
        default=None, validation_alias=AliasChoices("INSTANCE_ASSET_ROOT", "SUNRISE_INSTANCE_ASSET_ROOT")
    )
    logo_asset: str | None = Field(
        default=None, validation_alias=AliasChoices("LOGO_ASSET_PATH", "SUNRISE_LOGO_ASSET_PATH")
    )
    favicon_asset: str | None = Field(
        default=None, validation_alias=AliasChoices("FAVICON_ASSET_PATH", "SUNRISE_FAVICON_ASSET_PATH")
    )
    hero_asset: str | None = Field(
        default=None, validation_alias=AliasChoices("HERO_ASSET_PATH", "SUNRISE_HERO_ASSET_PATH")
    )
    og_image_asset: str | None = Field(
        default=None, validation_alias=AliasChoices("OG_IMAGE_ASSET_PATH", "SUNRISE_OG_IMAGE_ASSET_PATH")
    )
    database_url: str = Field(repr=False)
    redis_url: str = Field(default="redis://127.0.0.1:56379/0", repr=False)
    secret_key: str = Field(min_length=32, repr=False)
    local_test: bool = False
    runtime_mode: Literal["isolated", "production", "scoped_test"] = "isolated"
    scoped_test_admission_file: str | None = None
    scoped_test_admission_sha256: str | None = None
    production_admission_file: str | None = None
    production_admission_sha256: str | None = None
    bot_token: str | None = Field(default=None, repr=False)
    allowed_hosts: list[str] = ["127.0.0.1", "localhost"]
    allowed_origins: list[str] = ["http://127.0.0.1:3200", "http://localhost:3200"]
    admin_email_allowlist: list[str] = []
    admin_telegram_allowlist: list[str] = []
    admin_totp_secrets: dict[str, str] = Field(default_factory=dict, repr=False)
    email_allowlist: list[str] = []
    test_telegram_recipients: str = ""
    test_bot_ids: str = ""
    bot_test_token: str | None = Field(default=None, repr=False)
    telegram_miniapp_enabled: bool = False
    telegram_login_client_id: str | None = None
    telegram_login_client_secret: str | None = Field(default=None, repr=False)
    telegram_login_redirect_uri: str | None = None
    mail_dir: str = ".private/mail"
    dashboard_url: str | None = Field(
        default=None, validation_alias=AliasChoices("DASHBOARD_URL", "SUNRISE_DASHBOARD_URL")
    )
    subscription_public_origin: str | None = Field(
        default=None,
        validation_alias=AliasChoices("SUBSCRIPTION_PUBLIC_ORIGIN", "SUNRISE_SUBSCRIPTION_PUBLIC_ORIGIN"),
    )
    landing_url: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "PUBLIC_SITE_URL", "SUNRISE_PUBLIC_SITE_URL", "SUNRISE_LANDING_URL", "public_site_url"
        ),
    )
    bot_url: str | None = None
    support_url: str | None = Field(
        default=None, validation_alias=AliasChoices("SUPPORT_URL", "SUNRISE_SUPPORT_URL")
    )
    support_email: str | None = Field(
        default=None, validation_alias=AliasChoices("SUPPORT_EMAIL", "SUNRISE_SUPPORT_EMAIL")
    )
    owner_email: str | None = None
    owner_id: str | None = None
    landing_test_domain: str | None = None
    dashboard_test_domain: str | None = None
    legal_offer_url: str | None = Field(
        default=None, validation_alias=AliasChoices("OFFER_URL", "SUNRISE_LEGAL_OFFER_URL")
    )
    legal_privacy_url: str | None = Field(
        default=None, validation_alias=AliasChoices("PRIVACY_URL", "SUNRISE_LEGAL_PRIVACY_URL")
    )
    legal_offer_version: str | None = Field(
        default=None, validation_alias=AliasChoices("OFFER_VERSION", "SUNRISE_LEGAL_OFFER_VERSION")
    )
    legal_privacy_version: str | None = Field(
        default=None, validation_alias=AliasChoices("PRIVACY_VERSION", "SUNRISE_LEGAL_PRIVACY_VERSION")
    )
    turnstile_site_key: str | None = None
    turnstile_secret: str | None = Field(default=None, repr=False)
    turnstile_hostname: str | None = None
    turnstile_test_bypass: bool = False
    cookie_secure: bool = False
    rollypay_signing_secret: str | None = Field(default=None, repr=False)
    rollypay_terminal: str | None = None
    rollypay_api_key: str | None = Field(default=None, repr=False)
    rollypay_base_url: str = "https://rollypay.io"
    rollypay_test_authorized: bool = False
    webhook_base_url: str = "https://webhook.example.test"
    payment_allowed_hosts: list[str] = ["rollypay.io"]
    # Наша локальная политика GET-сверки поздней комиссии, не срок или гарантия банка.
    payment_settlement_interval_seconds: int = Field(default=300, ge=60, le=86400)
    payment_settlement_batch_size: int = Field(default=10, ge=1, le=50)
    recurring_test_authorized: bool = False
    recurring_test_payer_ids: list[str] = []
    panel_test_authorized: bool = False
    panel_webhook_secret: str | None = Field(default=None, repr=False)
    panel_webhook_test_authorized: bool = False
    panel_test_user_ids: list[str] = []
    panel_test_node_uuids: list[str] = []
    panel_webhook_max_bytes: int = Field(default=262144, ge=1024, le=1048576)
    panel_webhook_max_age_seconds: int = Field(default=300, ge=30, le=3600)
    panel_reconcile_interval_seconds: int = Field(default=300, ge=60, le=86400)
    push_private_key: str | None = Field(default=None, repr=False)
    push_subject: str = "mailto:support@example.test"
    push_test_authorized: bool = False
    panel_contract: str | None = None
    panel_username_prefix: str = Field(default="", max_length=12, pattern=r"^[A-Za-z0-9_-]*$")
    panel_base_url: str | None = None
    panel_tunnel_port: int | None = Field(default=None, ge=1024, le=65535)
    panel_token: str | None = Field(default=None, repr=False)
    panel_allowed_hosts: list[str] = []
    panel_subscription_allowed_hosts: list[str] = []
    panel_test_uuids: list[str] = []
    panel_test_short_uuids: list[str] = []
    panel_test_usernames: list[str] = []
    panel_test_squads: list[str] = []
    panel_test_external_squads: list[str] = []
    panel_test_hwids: list[str] = []
    panel_test_bindings: dict[str, dict[str, str]] = Field(default_factory=dict, repr=False)
    panel_test_device_bindings: dict[str, str] = Field(default_factory=dict, repr=False)
    smtp_test_authorized: bool = False
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_mode: str = "starttls"
    smtp_sender: str | None = None
    smtp_username: str | None = None
    smtp_password: str | None = Field(default=None, repr=False)
    smtp_allowed_hosts: list[str] = []
    smtp_tunnel_port: int | None = Field(default=None, ge=1024, le=65535)
    emoji_test_authorized: bool = False
    emoji_test_bot_id: str | None = None
    emoji_bot_token: str | None = Field(default=None, repr=False)
    emoji_test_ids: list[str] = []

    def __init__(self, **values):
        # Explicit legacy composition arguments outrank ambient presentation aliases.
        username_keys = {"telegram_bot_username", "TELEGRAM_BOT_USERNAME", "SUNRISE_TELEGRAM_BOT_USERNAME"}
        if values.get("bot_url") and not username_keys.intersection(values):
            parsed = urlparse(values["bot_url"])
            if parsed.scheme != "https" or parsed.netloc != "t.me" or parsed.query or parsed.fragment:
                raise ValueError("instance_bot_url_invalid")
            values["telegram_bot_username"] = parsed.path.removeprefix("/")
        super().__init__(**values)

    @field_validator(
        "instance_config_path",
        "app_name",
        "app_short_name",
        "app_slug",
        "app_description",
        "visual_profile",
        "accent_color",
        "telegram_bot_username",
        "telegram_menu_emoji_key",
        "sender_name",
        "instance_asset_root",
        "logo_asset",
        "favicon_asset",
        "hero_asset",
        "og_image_asset",
        "dashboard_url",
        "landing_url",
        "support_url",
        "support_email",
        "legal_offer_url",
        "legal_privacy_url",
        "legal_offer_version",
        "legal_privacy_version",
        mode="before",
    )
    @classmethod
    def empty_instance_override(cls, value):
        return None if isinstance(value, str) and not value.strip() else value

    @cached_property
    def instance(self):
        from .instance_config import load_instance_config

        fields = (
            "app_name",
            "app_short_name",
            "app_slug",
            "app_description",
            "visual_profile",
            "search_indexing_enabled",
            "accent_color",
            "dashboard_url",
            "subscription_public_origin",
            "telegram_bot_username",
            "telegram_menu_emoji_key",
            "support_url",
            "support_email",
            "sender_name",
            "logo_asset",
            "favicon_asset",
            "hero_asset",
            "og_image_asset",
            "legal_offer_url",
            "legal_privacy_url",
            "legal_offer_version",
            "legal_privacy_version",
        )
        overrides = {name: getattr(self, name) for name in fields}
        overrides.update(public_site_url=self.landing_url, asset_root=self.instance_asset_root)
        if self.bot_url and not self.telegram_bot_username:
            parsed = urlparse(self.bot_url)
            if parsed.scheme != "https" or parsed.netloc != "t.me" or parsed.query or parsed.fragment:
                raise ValueError("instance_bot_url_invalid")
            overrides["telegram_bot_username"] = parsed.path.removeprefix("/")
        return load_instance_config(self.instance_config_path, overrides=overrides)

    @model_validator(mode="after")
    def resolve_instance(self):
        instance = self.instance
        self.dashboard_url, self.landing_url, self.bot_url = (
            instance.dashboard_url,
            instance.public_site_url,
            instance.bot_url,
        )
        self.support_url, self.support_email = instance.support_url, instance.support_email
        self.subscription_public_origin = instance.subscription_public_origin
        self.legal_offer_url, self.legal_privacy_url = instance.legal_offer_url, instance.legal_privacy_url
        self.legal_offer_version, self.legal_privacy_version = (
            instance.legal_offer_version,
            instance.legal_privacy_version,
        )
        return self

    @field_validator("panel_tunnel_port", "smtp_tunnel_port", mode="before")
    @classmethod
    def optional_panel_tunnel(cls, value):
        return None if value is None or isinstance(value, str) and not value.strip() else value

    @field_validator("rollypay_base_url", mode="before")
    @classmethod
    def default_rollypay_base(cls, value):
        if value is None or isinstance(value, str) and not value.strip():
            return "https://rollypay.io"
        return value

    def webhook_url(self, service: str) -> str:
        if service not in {"rollypay", "remnawave", "telegram"}:
            raise ValueError("Неизвестный сервис webhook.")
        return self.webhook_base_url.rstrip("/") + "/" + service

    @property
    def sandbox_payments_enabled(self) -> bool:
        return bool(
            self.rollypay_test_authorized
            and self.rollypay_api_key
            and self.rollypay_signing_secret
            and self.rollypay_terminal
        )

    @model_validator(mode="after")
    def safe_environment(self):
        webhook = urlparse(self.webhook_base_url)
        if (
            webhook.scheme != "https"
            or not webhook.hostname
            or webhook.username
            or webhook.password
            or webhook.query
            or webhook.fragment
            or webhook.path not in {"", "/"}
        ):
            raise ValueError("Webhook base URL должен быть HTTPS-адресом без секретов и пути.")
        db = make_url(self.database_url)
        if self.runtime_mode == "production":
            scope, _ = load_production_scope(self)
            validate_production_settings(self, scope)
        elif (
            db.host not in {"127.0.0.1", "localhost"}
            or db.port != 55439
            or db.database not in {"sunrise_test", "sunrise_test_backend", "sunrise_test_bot"}
        ):
            raise ValueError("Приложение допускает только локальную синтетическую sunrise_test БД.")
        if self.runtime_mode != "production" and urlparse(self.redis_url).hostname not in {
            "127.0.0.1",
            "localhost",
        }:
            raise ValueError("Разрешён только отдельный локальный Redis.")
        if self.runtime_mode == "scoped_test":
            from .scoped_test import load_scoped_test_scope, validate_scoped_test_settings

            scope, _ = load_scoped_test_scope(self)
            validate_scoped_test_settings(self, scope)
        if "*" in self.allowed_hosts or "*" in self.allowed_origins:
            raise ValueError("Нужен явный список разрешённых адресов.")
        from .mail import configured_mail, synthetic_mail_address, valid_mail_address

        if any(not valid_mail_address(email) for email in self.email_allowlist):
            raise ValueError("Нужен точный список разрешённых почтовых адресов.")
        self.email_allowlist = [email.lower() for email in self.email_allowlist]
        if self.smtp_tunnel_port is not None and (not self.local_test or self.smtp_mode != "starttls"):
            raise ValueError("SMTP-туннель допускается только для локального теста STARTTLS.")
        if self.local_test and any(not synthetic_mail_address(email) for email in self.email_allowlist):
            if not self.smtp_test_authorized:
                raise ValueError("Реальная тестовая почта требует явного разрешения SMTP.")
            configured_mail(self)
        return self


PRODUCTION_CAPABILITIES = frozenset(
    {
        "invoice_create",
        "payment_reconcile",
        "financial_apply",
        "existing_recurring_status",
        "existing_recurring_cancel",
        "panel_mutation",
        "retention",
        "auth_mail",
        "notice_email",
        "notice_telegram",
        "push_delivery",
        "broadcast_delivery",
    }
)


def payment_context_matches(data, environment):
    if not isinstance(data, dict):
        return False
    if environment in {"live", "scoped_live"}:
        return data.get("environment") == "prod" and ("test" not in data or data["test"] is False)
    return environment == "sandbox" and data.get("environment") == "sandbox" and data.get("test") is True


class StrictScope(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)


class DatabaseScope(StrictScope):
    host: str
    port: int = Field(ge=1, le=65535)
    name: str
    role: str
    schema_name: str = Field(alias="schema")
    database_oid: int = Field(ge=1)
    schema_oid: int = Field(ge=1)
    schema_owner: str
    search_path: str

    @field_validator("host", "name", "role", "schema_name", "schema_owner", "search_path")
    @classmethod
    def exact(cls, value):
        if not value or value != value.strip() or "*" in value or any(ord(c) < 32 for c in value):
            raise ValueError("production_scope_invalid")
        return value


class RedisScope(StrictScope):
    host: str
    port: int = Field(ge=1, le=65535)
    database: int = Field(ge=0, le=15)
    key_prefix: str = Field(pattern=r"^[a-z0-9_-]+:[a-z0-9_-]+:$", max_length=100)


class BankScope(StrictScope):
    origin: str
    terminal: str = Field(min_length=1, max_length=64)
    checkout_hosts: list[str]


class PanelScope(StrictScope):
    origin: str
    contract: Literal["3.4_id"]
    version: Literal["3.4.5"]
    internal_squads: list[str]
    external_squads: list[str]
    subscription_hosts: list[str]
    node_uuids: list[str]


class MailScope(StrictScope):
    host: str
    port: int = Field(ge=1, le=65535)
    mode: Literal["smtps", "starttls"]
    sender: str


def validate_capabilities(value):
    if set(value) != PRODUCTION_CAPABILITIES or any(type(v) is not bool for v in value.values()):
        raise ValueError("production_capabilities_invalid")
    return value


class ProductionAdmission(StrictScope):
    version: int = Field(ge=1, le=1)
    scope_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    database: DatabaseScope
    redis: RedisScope
    instance_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_manifest_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    bot_id: str = Field(pattern=r"^[1-9][0-9]{0,18}$")
    rollypay: BankScope
    panel: PanelScope
    smtp: MailScope
    maximum_capabilities: dict[str, bool]

    _caps = field_validator("maximum_capabilities")(validate_capabilities)

    @model_validator(mode="after")
    def resources(self):
        from uuid import UUID

        for origin in (self.rollypay.origin, self.panel.origin):
            url = urlparse(origin)
            if not url.hostname or origin != "https://" + url.hostname:
                raise ValueError("production_origin_invalid")
        for host in [
            self.redis.host,
            self.smtp.host,
            *self.rollypay.checkout_hosts,
            *self.panel.subscription_hosts,
        ]:
            if not re.fullmatch(r"[a-z0-9][a-z0-9.-]*", host) or ".." in host:
                raise ValueError("production_host_invalid")
        if int(self.bot_id) >= 2**63 or not self.panel.internal_squads or not self.rollypay.checkout_hosts:
            raise ValueError("production_identity_invalid")
        for value in [*self.panel.internal_squads, *self.panel.external_squads, *self.panel.node_uuids]:
            if str(UUID(value)) != value:
                raise ValueError("production_resource_invalid")
        if self.database.name in {
            "sunrise_test",
            "sunrise_test_backend",
            "sunrise_test_bot",
            "sunrise_legacy",
        }:
            raise ValueError("production_database_invalid")
        return self


class RuntimeGates(StrictScope):
    version: int = Field(ge=1, le=1)
    scope_id: str
    generation: int = Field(ge=1)
    gates: dict[str, bool]

    _caps = field_validator("gates")(validate_capabilities)


def _unique_json(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("production_scope_invalid")
        result[key] = value
    return result


def _owned_file(root, relative):
    from .instance_assets import reparse

    name = Path(relative)
    target = root / name
    if name.is_absolute() or ".." in name.parts or not name.parts or "\\" in str(relative):
        raise ValueError("production_path_invalid")
    if any(reparse(item) for item in [target, *target.parents] if item != root.parent):
        raise ValueError("production_path_invalid")
    if not target.resolve().is_relative_to(root) or not target.is_file():
        raise ValueError("production_path_invalid")
    return target


def load_production_scope(config):
    from .instance_config import PROJECT_ROOT

    root = PROJECT_ROOT.resolve()
    relative = config.production_admission_file or ""
    if not relative.startswith(".private/production/") or not re.fullmatch(
        r"[a-f0-9]{64}", config.production_admission_sha256 or ""
    ):
        raise ValueError("production_admission_missing")
    path = _owned_file(root, relative)
    raw = path.read_bytes()
    if len(raw) > 65536 or hashlib.sha256(raw).hexdigest() != config.production_admission_sha256:
        raise ValueError("production_admission_changed")
    return ProductionAdmission.model_validate(json.loads(raw, object_pairs_hook=_unique_json)), path


def validate_production_settings(config, scope):
    db = make_url(config.database_url)
    target = scope.database
    if (db.get_backend_name(), db.host, db.port, db.database, db.username) != (
        "postgresql",
        target.host,
        target.port,
        target.name,
        target.role,
    ) or db.query:
        raise ValueError("production_database_scope_denied")
    redis = urlparse(config.redis_url)
    if (redis.scheme, redis.hostname, redis.port, redis.path, redis.query, redis.fragment) != (
        "redis",
        scope.redis.host,
        scope.redis.port,
        "/" + str(scope.redis.database),
        "",
        "",
    ):
        raise ValueError("production_redis_scope_denied")
    flags = [name for name in Settings.model_fields if name.endswith("_test_authorized")]
    if (
        config.local_test
        or config.turnstile_test_bypass
        or not config.cookie_secure
        or any(getattr(config, name) for name in flags)
        or config.panel_tunnel_port
        or config.smtp_tunnel_port
    ):
        raise ValueError("production_test_transport_denied")
    if (
        config.rollypay_base_url.rstrip("/") != scope.rollypay.origin
        or config.rollypay_terminal != scope.rollypay.terminal
    ):
        raise ValueError("production_bank_scope_denied")
    if (
        config.panel_base_url != scope.panel.origin
        or config.panel_contract != scope.panel.contract
        or set(config.panel_allowed_hosts) != {urlparse(scope.panel.origin).hostname}
    ):
        raise ValueError("production_panel_scope_denied")
    if (config.smtp_host, config.smtp_port, config.smtp_mode, config.smtp_sender) != (
        scope.smtp.host,
        scope.smtp.port,
        scope.smtp.mode,
        scope.smtp.sender,
    ):
        raise ValueError("production_mail_scope_denied")
    if not config.bot_token or config.bot_token.split(":", 1)[0] != scope.bot_id:
        raise ValueError("production_bot_scope_denied")


class RuntimePolicy:
    def __init__(self, config, scope=None, path=None):
        from types import MappingProxyType

        self.config, self.scope, self.path = config, scope, path
        self._production = config.runtime_mode == "production"
        self._admission_sha256 = getattr(config, "production_admission_sha256", None)
        self._maximum = MappingProxyType(dict(scope.maximum_capabilities)) if scope else None
        self._database, self._attested = None, False

    @property
    def production(self):
        return self._production

    @property
    def scoped_payments(self):
        return False

    @property
    def payment_environment(self):
        return "live" if self.production else "sandbox"

    @classmethod
    def for_settings(cls, config):
        if config.runtime_mode == "scoped_test":
            from .scoped_test import ScopedTestPolicy, load_scoped_test_scope

            return ScopedTestPolicy(config, *load_scoped_test_scope(config))
        return (
            cls(config, *load_production_scope(config))
            if config.runtime_mode == "production"
            else cls(config)
        )

    def _marker(self):
        return {
            "version": 1,
            "scope_id": self.scope.scope_id,
            "instance_sha256": self.scope.instance_sha256,
            "admission_sha256": self._admission_sha256,
            "source_manifest_sha256": self.scope.source_manifest_sha256,
        }

    def _check_database(self, session):
        from sqlalchemy import text

        bind = session.get_bind()
        if getattr(bind, "engine", bind) is not self._database.engine:
            raise ValueError("production_database_scope_denied")
        row = (
            session.execute(
                text("""SELECT current_database() AS name, current_user AS role,
          current_schema() AS schema, d.oid::bigint AS database_oid, n.oid::bigint AS schema_oid,
          pg_get_userbyid(n.nspowner) AS schema_owner, current_setting('search_path') AS search_path
          FROM pg_database d CROSS JOIN pg_namespace n
          WHERE d.datname=current_database() AND n.nspname=current_schema()""")
            )
            .mappings()
            .one()
        )
        expected = self.scope.database.model_dump(exclude={"host", "port"}, by_alias=True)
        if dict(row) != expected:
            raise ValueError("production_database_scope_denied")

    def admit(self, database):
        if not self.production:
            return self
        from .instance_config import PROJECT_ROOT

        root = PROJECT_ROOT.resolve()
        instance = _owned_file(root, self.config.instance_config_path)
        if hashlib.sha256(instance.read_bytes()).hexdigest() != self.scope.instance_sha256:
            raise ValueError("production_instance_changed")
        manifest = _owned_file(root, ".private/production/source-manifest.json").read_bytes()
        if hashlib.sha256(manifest).hexdigest() != self.scope.source_manifest_sha256:
            raise ValueError("production_source_changed")
        records = json.loads(manifest, object_pairs_hook=_unique_json)
        if not isinstance(records, list) or not records:
            raise ValueError("production_source_invalid")
        paths = set()
        for record in records:
            if (
                not isinstance(record, dict)
                or set(record) != {"path", "sha256"}
                or record["path"] in paths
                or not re.fullmatch(r"[a-f0-9]{64}", record["sha256"])
            ):
                raise ValueError("production_source_invalid")
            if record["path"].startswith((".private/", ".env", "config/", "instance-assets")):
                raise ValueError("production_source_invalid")
            target = _owned_file(root, record["path"])
            if hashlib.sha256(target.read_bytes()).hexdigest() != record["sha256"]:
                raise ValueError("production_source_changed")
            paths.add(record["path"])
        runtime_files = {
            p.relative_to(root).as_posix()
            for base in ("apps", "packages")
            for p in (root / base).rglob("*.py")
            if "__pycache__" not in p.parts
        }
        if not runtime_files <= paths:
            raise ValueError("production_source_incomplete")
        self._database = database
        with database.transaction() as session:
            self._check_database(session)
            self._check_current(session)
        self._attested = True
        return self

    def _check_current(self, session):
        from .db import Setting
        from .instance_assets import reparse

        if (
            any(reparse(part) for part in [self.path, *self.path.parents])
            or hashlib.sha256(self.path.read_bytes()).hexdigest() != self._admission_sha256
        ):
            raise ValueError("production_admission_changed")
        marker = session.get(Setting, "production_admission", populate_existing=True)
        if not marker or marker.value != self._marker() or type(marker.value.get("version")) is not int:
            raise ValueError("production_admission_changed")
        row = session.get(Setting, "production_runtime_gates", populate_existing=True)
        gates = RuntimeGates.model_validate(row.value if row else {})
        if gates.scope_id != self.scope.scope_id:
            raise ValueError("production_admission_changed")
        return gates.gates

    def require_current(self, session, capability=None):
        if not self.production:
            return
        from sunrise_core.errors import DomainError

        try:
            if not self._attested:
                raise ValueError("production_admission_missing")
            self._check_database(session)
            gates = self._check_current(session)
        except (OSError, ValueError, TypeError, AttributeError):
            raise DomainError(
                "production_admission_denied", "Текущий допуск окружения не подтверждён.", 503
            ) from None
        if capability is not None and (
            capability not in PRODUCTION_CAPABILITIES
            or not self._maximum[capability]
            or not gates[capability]
        ):
            raise DomainError("production_stage_denied", "Операция временно недоступна.", 503)

    def require(self, capability=None):
        if self.production:
            if self._database is None:
                from sunrise_core.errors import DomainError

                raise DomainError(
                    "production_admission_denied", "Текущий допуск окружения не подтверждён.", 503
                )
            with self._database.transaction() as session:
                self.require_current(session, capability)


def configured_runtime(config, database):
    cached = getattr(config, "_runtime_policy", None)
    if cached is not None:
        if (cached.production or cached.scoped_payments) and cached._database is not database:
            raise ValueError("production_database_scope_denied")
        return cached
    policy = RuntimePolicy.for_settings(config).admit(database)
    object.__setattr__(config, "_runtime_policy", policy)
    return policy


def runtime_policy(config):
    policy = getattr(config, "_runtime_policy", None)
    if getattr(config, "runtime_mode", "isolated") in {"production", "scoped_test"} and policy is None:
        raise ValueError("production_admission_missing")
    return policy
