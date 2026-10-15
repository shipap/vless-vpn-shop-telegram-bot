from datetime import UTC, date, datetime

import pytest


def test_current_month_uses_moscow_date_at_utc_month_boundary():
    from sunrise_bot.admin_calendar import CalendarState

    state = CalendarState.current(datetime(2026, 9, 30, 22, 30, tzinfo=UTC))
    assert (state.year, state.month, state.view) == (2026, 10, "days")


def test_current_month_rejects_naive_clock():
    from sunrise_bot.admin_calendar import CalendarState

    with pytest.raises(ValueError, match="часов"):
        CalendarState.current(datetime(2026, 10, 8))


def test_two_dates_include_every_day_and_swap_backwards_selection():
    from sunrise_bot.admin_calendar import CalendarState, render_calendar, transition

    state = transition(CalendarState(2024, 2), "select_day", "2024-02-29")
    state = transition(state, "select_day", "2024-02-26")
    assert (state.start, state.end) == (date(2024, 2, 26), date(2024, 2, 29))
    view = render_calendar(state)
    selected = [
        data["value"]
        for row in view.rows
        for _, _, data in row
        if data.get("operation") == "select_day" and data["semantic"] == "success"
    ]
    assert selected == ["2024-02-26", "2024-02-27", "2024-02-28", "2024-02-29"]
    apply = [(action, data) for row in view.rows for _, action, data in row if action.endswith("apply")]
    assert apply == [("admin_calendar_apply", {"icon_key": "checkout", "semantic": "success"})]


def test_single_date_requires_second_click_even_when_same_day():
    from sunrise_bot.admin_calendar import CalendarState, render_calendar, transition

    state = transition(CalendarState(2026, 10), "select_day", "2026-10-08")
    assert state.end is None
    assert not any(action.endswith("apply") for row in render_calendar(state).rows for _, action, _ in row)
    state = transition(state, "select_day", "2026-10-08")
    assert (state.start, state.end) == (date(2026, 10, 8), date(2026, 10, 8))


def test_third_click_starts_new_range_and_clear_resets_selection():
    from sunrise_bot.admin_calendar import CalendarState, transition

    state = CalendarState(2026, 10, date(2026, 10, 1), date(2026, 10, 4))
    state = transition(state, "select_day", "2026-10-12")
    assert (state.start, state.end) == (date(2026, 10, 12), None)
    cleared = transition(state, "clear")
    assert (cleared.start, cleared.end, cleared.year, cleared.month) == (None, None, 2026, 10)


def test_month_navigation_preserves_cross_year_range():
    from sunrise_bot.admin_calendar import CalendarState, render_calendar, transition

    state = CalendarState(2024, 12, date(2024, 12, 30), date(2025, 1, 2))
    state = transition(state, "next")
    assert (state.year, state.month, state.start, state.end) == (
        2025,
        1,
        date(2024, 12, 30),
        date(2025, 1, 2),
    )
    days = [
        data["value"]
        for row in render_calendar(state).rows
        for _, _, data in row
        if data.get("operation") == "select_day" and data["semantic"] == "success"
    ]
    assert days == ["2025-01-01", "2025-01-02"]
    assert transition(state, "previous").month == 12


def test_month_selection_is_actionable_and_green_for_intersecting_span():
    from sunrise_bot.admin_calendar import CalendarState, render_calendar, transition

    state = transition(CalendarState(2026, 3, date(2026, 2, 28), date(2026, 4, 1)), "show_months")
    months = [
        (label, data)
        for row in render_calendar(state).rows
        for label, _, data in row
        if data.get("operation") == "select_month"
    ]
    assert len(months) == 12
    assert [data["value"] for _, data in months if data["semantic"] == "success"] == [2, 3, 4]
    april = transition(state, "select_month", 4)
    assert (april.year, april.month, april.view) == (2026, 4, "days")
    assert april.start == date(2026, 2, 28)


def test_year_selection_is_actionable_and_green_for_intersecting_span():
    from sunrise_bot.admin_calendar import CalendarState, render_calendar, transition

    state = transition(CalendarState(2026, 10, date(2025, 12, 31), date(2027, 1, 1)), "show_years")
    years = [
        data
        for row in render_calendar(state).rows
        for _, _, data in row
        if data.get("operation") == "select_year"
    ]
    assert [data["value"] for data in years if data["semantic"] == "success"] == [2025, 2026, 2027]
    selected = transition(state, "select_year", 2027)
    assert (selected.year, selected.month, selected.view) == (2027, 10, "months")
    assert selected.end == date(2027, 1, 1)


@pytest.mark.parametrize(
    "operation,view,expected",
    [
        ("next", "months", (2027, 2)),
        ("previous", "months", (2025, 2)),
        ("next", "years", (2038, 2)),
        ("previous", "years", (2014, 2)),
    ],
)
def test_month_year_views_have_year_and_page_navigation(operation, view, expected):
    from sunrise_bot.admin_calendar import CalendarState, transition

    state = transition(CalendarState(2026, 2, view=view), operation)
    assert (state.year, state.month) == expected
    assert state.view == view


@pytest.mark.parametrize("year,has_leap_day", [(2024, True), (2025, False), (2000, True), (2100, False)])
def test_calendar_uses_real_leap_years(year, has_leap_day):
    from sunrise_bot.admin_calendar import CalendarState, render_calendar

    values = [
        data.get("value")
        for row in render_calendar(CalendarState(year, 2)).rows
        for _, _, data in row
        if data.get("operation") == "select_day"
    ]
    assert (f"{year}-02-29" in values) is has_leap_day
    assert len(values) == (29 if has_leap_day else 28)


def test_future_days_are_selectable_and_render_uses_existing_unique_control_icons():
    from sunrise_bot.admin_calendar import CalendarState, render_calendar, transition
    from sunrise_bot.theme import ADMIN_ICON_ALIASES, ICONS

    state = transition(CalendarState(2032, 6), "select_day", "2032-06-18")
    rows = render_calendar(state).rows
    controls = [
        data
        for row in rows
        for _, _, data in row
        if data.get("operation") in {"previous", "next", "show_months", "show_years", "clear"}
    ]
    keys = [data["icon_key"] for data in controls]
    assert len(keys) == len(set(keys)) == 5
    assert all(key in ICONS or ADMIN_ICON_ALIASES.get(key) in ICONS for key in keys)
    assert all(action.startswith("admin_calendar_") for row in rows for _, action, _ in row)
    assert all("callback_data" not in data for row in rows for _, _, data in row)


@pytest.mark.parametrize(
    "operation,value",
    [
        ("select_day", "2025-02-29"),
        ("select_day", "1800-01-01"),
        ("select_month", 13),
        ("select_year", 2200),
        ("unknown", None),
    ],
)
def test_invalid_transitions_are_rejected(operation, value):
    from sunrise_bot.admin_calendar import CalendarState, transition

    with pytest.raises(ValueError):
        transition(CalendarState(2026, 10), operation, value)


def test_navigation_stays_in_bounds():
    from sunrise_bot.admin_calendar import CalendarState, transition

    assert transition(CalendarState(1900, 1), "previous") == CalendarState(1900, 1)
    assert transition(CalendarState(2199, 12), "next") == CalendarState(2199, 12)
    assert transition(CalendarState(1900, 1, view="years"), "previous").year == 1900
    assert transition(CalendarState(2199, 1, view="years"), "next").year == 2199
