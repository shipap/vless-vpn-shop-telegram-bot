"""A verified panel absence remains representable on the customer dashboard."""

from sunrise_api.schemas import SubscriptionDeletionNoticeDto


def test_verified_panel_absence_is_a_valid_dashboard_notice_reason():
    notice = SubscriptionDeletionNoticeDto.model_validate(
        {
            "subscription_id": "sub-1",
            "name": "Тариф",
            "deleted_at": "2026-10-13T00:00:00+00:00",
            "reason": "verified_panel_absence",
        }
    )
    assert notice.reason == "verified_panel_absence"
