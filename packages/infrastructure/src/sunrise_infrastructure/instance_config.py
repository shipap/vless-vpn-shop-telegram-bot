"""Типизированные публичные настройки экземпляра без секретов и бизнес-политик."""

import json
import re
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    ValidationInfo,
    field_validator,
    model_validator,
)

PROJECT_ROOT = Path(__file__).resolve().parents[4]
ASSET_ROLES = {"logo": "logo_asset", "favicon": "favicon_asset", "hero": "hero_asset", "og": "og_image_asset"}


def public_url(value):
    if value is None or value == "":
        return None
    if not isinstance(value, str) or len(value) > 2048 or any(ord(c) < 32 for c in value):
        raise ValueError("instance_url_invalid")
    parsed = urlsplit(value)
    if (
        not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
        or parsed.scheme != "https"
        and not (parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "[::1]", "::1"})
    ):
        raise ValueError("instance_url_invalid")
    return value


class InstanceTerminology(BaseModel):
    """Фразы интерфейса экземпляра; не меняют предмет услуги или платёжный контракт."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, hide_input_in_errors=True)

    service_label: str = "VPN"
    service_summary: str = "VPN для ваших устройств"
    connections_label: str = "Мой VPN"
    connect_label: str = "Подключить VPN"
    client_label: str = "VPN-приложение"
    client_installation_step: str = "Установите совместимый VPN-клиент, например Incy или Happ."
    connection_label: str = "VPN-подключение"
    hero_title: str = "Ваш VPN."
    welcome_body: str = "Ваш VPN, подписки и платежи — в одном месте."
    provisioning_pending_body: str = (
        "Оплата получена, подключаем VPN. Повторно оплачивать не нужно. Мы сообщим, когда доступ будет готов."
    )
    provisioning_ready_body: str = "Оплата получена. Доступ выдан. Откройте подписку для подключения VPN."

    @field_validator("*")
    @classmethod
    def phrase(cls, value, info: ValidationInfo):
        if info.field_name == "welcome_body" and value == "":
            return value
        if not value.strip() or len(value) > 240 or any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("instance_terminology_invalid")
        return value.strip()


class InstanceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    app_name: str = "VLESS VPN Shop"
    app_short_name: str | None = None
    app_slug: str = "vpn-shop"
    app_description: str = "Подписки VPN и управление подключением."
    visual_profile: str = "neutral"
    search_indexing_enabled: StrictBool = False
    terminology: InstanceTerminology = Field(default_factory=InstanceTerminology)
    accent_color: str = "#F0A45D"
    public_site_url: str | None = None
    dashboard_url: str | None = None
    subscription_public_origin: str | None = None
    telegram_bot_username: str | None = None
    telegram_menu_emoji_key: str | None = None
    support_url: str | None = None
    support_email: str | None = None
    sender_name: str | None = None
    asset_root: str = "instance-assets"
    logo_asset: str | None = None
    favicon_asset: str | None = None
    hero_asset: str | None = None
    og_image_asset: str | None = None
    legal_offer_url: str | None = None
    legal_privacy_url: str | None = None
    legal_offer_version: str = ""
    legal_privacy_version: str = ""

    @field_validator("app_name", "app_short_name", "sender_name", "app_description")
    @classmethod
    def text(cls, value, info):
        if value is None and info.field_name in {"app_short_name", "sender_name"}:
            return None
        limit = 500 if info.field_name == "app_description" else 120
        if (
            not isinstance(value, str)
            or not value.strip()
            or len(value) > limit
            or any(ord(c) < 32 for c in value)
        ):
            raise ValueError("instance_text_invalid")
        return value.strip()

    @field_validator("app_slug")
    @classmethod
    def slug(cls, value):
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,47}", value):
            raise ValueError("instance_slug_invalid")
        return value

    @field_validator("visual_profile")
    @classmethod
    def profile(cls, value):
        if value not in {"neutral", "private"}:
            raise ValueError("instance_profile_invalid")
        return value

    @field_validator("accent_color")
    @classmethod
    def color(cls, value):
        if not re.fullmatch(r"#[0-9a-fA-F]{6}", value):
            raise ValueError("instance_accent_invalid")
        return value

    @field_validator(
        "public_site_url", "dashboard_url", "support_url", "legal_offer_url", "legal_privacy_url"
    )
    @classmethod
    def url(cls, value):
        return public_url(value)

    @field_validator("subscription_public_origin")
    @classmethod
    def subscription_origin(cls, value):
        if value is None or value == "":
            return None
        if not isinstance(value, str) or "\\" in value or any(ord(c) <= 32 or ord(c) == 127 for c in value):
            raise ValueError("subscription_origin_invalid")
        value = public_url(value)
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https" or parsed.port not in {None, 443}
            or parsed.path not in {"", "/"} or parsed.query or parsed.fragment or "%" in parsed.netloc
        ):
            raise ValueError("subscription_origin_invalid")
        return "https://" + parsed.netloc

    @field_validator("telegram_bot_username")
    @classmethod
    def username(cls, value):
        if value is None or value == "":
            return None
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{4,31}", value):
            raise ValueError("instance_bot_username_invalid")
        return value

    @field_validator("support_email")
    @classmethod
    def email(cls, value):
        if value is None or value == "":
            return None
        from .mail import valid_mail_address

        if not valid_mail_address(value):
            raise ValueError("instance_support_email_invalid")
        return value

    @field_validator("telegram_menu_emoji_key")
    @classmethod
    def emoji_key(cls, value):
        if value is None:
            return None
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,47}", value):
            raise ValueError("instance_emoji_key_invalid")
        return value

    @field_validator("asset_root", "logo_asset", "favicon_asset", "hero_asset", "og_image_asset")
    @classmethod
    def relative_path(cls, value, info):
        if value is None or value == "":
            if info.field_name == "asset_root":
                raise ValueError("instance_asset_root_invalid")
            return None
        if (
            not isinstance(value, str)
            or len(value) > 240
            or value.startswith("/")
            or "\\" in value
            or any(part in {"", ".", ".."} for part in value.split("/"))
            or not re.fullmatch(r"[A-Za-z0-9_./-]+", value)
        ):
            raise ValueError("instance_asset_path_invalid")
        if info.field_name == "asset_root" and not re.fullmatch(
            r"instance-assets(?:\.[a-z0-9_-]{1,40})?", value.rsplit("/", 1)[-1]
        ):
            raise ValueError("instance_asset_root_invalid")
        return value

    @model_validator(mode="after")
    def names(self):
        object.__setattr__(self, "app_short_name", self.app_short_name or self.app_name)
        object.__setattr__(self, "sender_name", self.sender_name or self.app_name)
        return self

    @property
    def bot_url(self):
        return "https://t.me/" + self.telegram_bot_username if self.telegram_bot_username else None

    def public(self, *, project_root=None):
        fields = (
            "app_name",
            "app_short_name",
            "app_slug",
            "app_description",
            "visual_profile",
            "search_indexing_enabled",
            "accent_color",
            "public_site_url",
            "dashboard_url",
            "telegram_bot_username",
            "support_url",
            "support_email",
            "sender_name",
        )
        result = {field: getattr(self, field) for field in fields}
        result["terminology"] = self.terminology.model_dump()
        for role, field in ASSET_ROLES.items():
            asset_url = None
            if getattr(self, field):
                from .instance_assets import read_asset

                if read_asset(self, role, project_root=project_root) is not None:
                    asset_url = "/api/v1/instance/assets/" + role
            result["og_image_url" if role == "og" else role + "_url"] = asset_url
        return result


def load_instance_config(config_path="config/instance.json", *, overrides=None, project_root=None):
    root = Path(project_root or PROJECT_ROOT).resolve()
    values = {}
    if config_path is not None:
        relative = Path(config_path)
        path = root / relative
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or relative.suffix != ".json"
            or relative.parts[0] != "config"
        ):
            raise ValueError("instance_config_path_invalid")
        from .instance_assets import reparse

        if any(reparse(part) for part in [path, *path.parents] if part != root.parent):
            raise ValueError("instance_config_path_invalid")
        if path.exists():
            if (
                not path.is_file()
                or path.stat().st_size > 65536
                or not path.resolve().is_relative_to(root / "config")
            ):
                raise ValueError("instance_config_path_invalid")
            try:
                values = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
            except (OSError, UnicodeError, ValueError):
                raise ValueError("instance_config_invalid") from None
            if not isinstance(values, dict):
                raise ValueError("instance_config_invalid")
    values.update({key: value for key, value in (overrides or {}).items() if value is not None})
    return InstanceConfig.model_validate(values)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("instance_config_invalid")
        result[key] = value
    return result


def configured_instance(settings):
    instance = getattr(settings, "instance", None)
    return instance if isinstance(instance, InstanceConfig) else InstanceConfig()
