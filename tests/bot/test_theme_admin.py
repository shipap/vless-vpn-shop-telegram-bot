from sunrise_bot.theme import presentation


def test_admin_style_uses_owner_override_with_primary_default():
    assert presentation({}, "admin", custom_emoji=False)[1]["style"] == "primary"
    assert presentation({"styles": {"admin": "danger"}}, "admin", custom_emoji=False)[1]["style"] == "danger"


def test_navigation_is_red_despite_theme_and_explicit_semantic_preserving_premium_icon():
    theme = {"styles": {"danger": "success", "primary": "success"},
             "icons": {"back": {"custom_emoji_id": "123", "fallback": "⬅️"},
                       "main": {"custom_emoji_id": "456", "fallback": "🏠"}}}
    for key, identifier in (("back", "123"), ("main", "456")):
        fallback, fields = presentation(theme, key, custom_emoji=True, semantic="primary")
        assert fields["style"] == "danger"
        assert fields["icon_custom_emoji_id"] == identifier
        assert fallback == ""
        assert presentation(theme, key, custom_emoji=False)[1]["style"] == "danger"


def test_actual_checkout_stays_green_and_explicit_neutral_has_no_color():
    assert presentation({}, "checkout", custom_emoji=False)[1]["style"] == "success"
    assert "style" not in presentation({}, "checkout", custom_emoji=False, semantic="neutral")[1]


def test_payment_methods_keep_distinct_fallbacks_when_premium_is_unavailable():
    for key, fallback in (
        ("payment_sbp", "💠"),
        ("payment_crypto", "🦋"),
        ("payment_xrocket", "🚀"),
        ("payment_bank_card", "💳"),
    ):
        theme = {"icons": {key: {"custom_emoji_id": "9007199254740993"}}}
        assert presentation(theme, key, custom_emoji=False) == (fallback, {})
        assert presentation({}, key, custom_emoji=True) == (fallback, {})
        assert presentation(theme, key, custom_emoji=True) == (
            "", {"icon_custom_emoji_id": "9007199254740993"}
        )
