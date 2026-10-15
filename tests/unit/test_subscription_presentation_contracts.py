"""Customer subscription projection uses current catalog titles and trusted public origins."""

from types import SimpleNamespace

import pytest
from sunrise_infrastructure import commerce
from sunrise_infrastructure.instance_config import InstanceConfig
from sunrise_infrastructure.panel import RemnawaveTest

SHORT = "syntheticShort123456789"
OLD = "https://provider.synthetic.invalid/sub/" + SHORT
PUBLIC = "https://connect.synthetic.invalid"


def test_subscription_title_uses_current_validated_catalog_content_and_safe_fallback():
    product = SimpleNamespace(name="Тариф из базы", content={"name": [
        {"type": "emoji", "fallback": "💎", "label": "Тариф"},
        {"type": "bold", "children": [{"type": "text", "text": " Универсальный"}]},
    ]})
    assert commerce.subscription_display_name(product, "Моя подписка") == "💎 Универсальный"
    product.content = {"name": [{"type": "script", "text": "unsafe"}]}
    assert commerce.subscription_display_name(product, "Рабочая подписка") == "Тариф из базы"
    product.content = {}
    product.name = '<tg-emoji emoji-id="9007199254740993">💎</tg-emoji> Другой тариф'
    assert commerce.subscription_display_name(product, "Моя подписка") == "💎 Другой тариф"
    assert commerce.subscription_display_name(None, "Сохранённое имя") == "Сохранённое имя"


def test_public_connection_projection_preserves_raw_path_short_and_original_storage():
    sub = SimpleNamespace(connection_url=OLD, short_uuid=SHORT)
    panel = RemnawaveTest.__new__(RemnawaveTest)
    panel.subscription_hosts = {"provider.synthetic.invalid", "connect.synthetic.invalid"}
    assert commerce.subscription_public_url(sub, panel, PUBLIC, panel.subscription_hosts) == PUBLIC + "/sub/" + SHORT
    assert sub.connection_url == OLD and sub.short_uuid == SHORT
    assert commerce.subscription_public_url(sub, panel, None, panel.subscription_hosts) == OLD
    assert commerce.subscription_public_url(sub, panel, PUBLIC, {"provider.synthetic.invalid"}) == PUBLIC + "/sub/" + SHORT


@pytest.mark.parametrize("link", [
    "https://foreign.synthetic.invalid/sub/" + SHORT,
    "https://provider.synthetic.invalid/sub/differentShort123456789",
    OLD + "?redirect=other", OLD + "#fragment", OLD.replace("https://", "http://"),
    "https://user:password@provider.synthetic.invalid/sub/" + SHORT,
])
def test_public_connection_projection_denies_foreign_or_wrong_identity_source(link):
    sub = SimpleNamespace(connection_url=link, short_uuid=SHORT)
    panel = RemnawaveTest.__new__(RemnawaveTest)
    panel.subscription_hosts = {"provider.synthetic.invalid", "connect.synthetic.invalid"}
    assert commerce.subscription_public_url(sub, panel, PUBLIC, panel.subscription_hosts) is None


def test_subscription_public_origin_is_typed_https_origin_without_secret_or_path():
    assert InstanceConfig(subscription_public_origin=PUBLIC + "/").subscription_public_origin == PUBLIC
    for origin in ("http://connect.synthetic.invalid", PUBLIC + "/other", PUBLIC + "?secret=value", PUBLIC + "#other", "https://user:password@connect.synthetic.invalid", "https://connect.synthetic.invalid:444", "https://connect.synthetic.invalid\\evil", "https://connect.synthetic.invalid%2fother", PUBLIC + "\n"):
        with pytest.raises(ValueError):
            InstanceConfig(subscription_public_origin=origin)
