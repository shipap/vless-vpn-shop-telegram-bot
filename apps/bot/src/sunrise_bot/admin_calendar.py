"""Календарь диапазона дат: состояние и описания кнопок без сетевых зависимостей."""

from calendar import Calendar, monthrange
from dataclasses import dataclass, replace
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

MIN_YEAR = 1900
MAX_YEAR = 2199
MOSCOW = ZoneInfo("Europe/Moscow")
MONTHS = (
    "Январь",
    "Февраль",
    "Март",
    "Апрель",
    "Май",
    "Июнь",
    "Июль",
    "Август",
    "Сентябрь",
    "Октябрь",
    "Ноябрь",
    "Декабрь",
)
WEEKDAYS = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")
ButtonDescriptor = tuple[str, str, dict[str, Any]]


@dataclass(frozen=True)
class CalendarState:
    year: int
    month: int
    start: date | None = None
    end: date | None = None
    view: str = "days"

    def __post_init__(self):
        if type(self.year) is not int or not MIN_YEAR <= self.year <= MAX_YEAR:
            raise ValueError("Год календаря вне допустимого диапазона")
        if type(self.month) is not int or not 1 <= self.month <= 12:
            raise ValueError("Неверный месяц календаря")
        if self.view not in {"days", "months", "years"}:
            raise ValueError("Неверный вид календаря")
        for day in (self.start, self.end):
            if day is not None and (type(day) is not date or not MIN_YEAR <= day.year <= MAX_YEAR):
                raise ValueError("Дата вне допустимого диапазона")
        if self.end is not None and (self.start is None or self.end < self.start):
            raise ValueError("Неверный диапазон дат")

    @classmethod
    def current(cls, now: datetime | None = None):
        now = now or datetime.now(MOSCOW)
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Для календаря нужен часовой пояс")
        local = now.astimezone(MOSCOW)
        return cls(local.year, local.month)


@dataclass(frozen=True)
class CalendarView:
    body: str
    rows: list[list[ButtonDescriptor]]


def transition(state: CalendarState, operation: str, value: str | int | None = None) -> CalendarState:
    if operation == "noop":
        return state
    if operation in {"show_days", "show_months", "show_years"}:
        return replace(state, view=operation.removeprefix("show_"))
    if operation == "clear":
        return replace(state, start=None, end=None)
    if operation == "select_day":
        if not isinstance(value, str):
            raise ValueError("Неверная дата календаря")
        chosen = date.fromisoformat(value)
        if state.start is None or state.end is not None:
            return replace(state, year=chosen.year, month=chosen.month, start=chosen, end=None, view="days")
        start, end = sorted((state.start, chosen))
        return replace(state, year=chosen.year, month=chosen.month, start=start, end=end, view="days")
    if operation == "select_month":
        return replace(state, month=value, view="days")
    if operation == "select_year":
        return replace(state, year=value, view="months")
    if operation in {"previous", "next"}:
        offset = 1 if operation == "next" else -1
        if state.view == "days":
            index = state.year * 12 + state.month - 1 + offset
            index = max(MIN_YEAR * 12, min(MAX_YEAR * 12 + 11, index))
            year, month = divmod(index, 12)
            return replace(state, year=year, month=month + 1)
        offset *= 12 if state.view == "years" else 1
        return replace(state, year=max(MIN_YEAR, min(MAX_YEAR, state.year + offset)))
    raise ValueError("Неизвестное действие календаря")


def _intersects(state: CalendarState, start: date, end: date) -> bool:
    return state.start is not None and start <= (state.end or state.start) and end >= state.start


def _button(label, operation, *, value=None, icon_key=None, selected=False):
    data = {"operation": operation, "semantic": "success" if selected else "neutral"}
    if value is not None:
        data["value"] = value
    if icon_key:
        data["icon_key"] = icon_key
    else:
        data["icon"] = {"fallback": ""}
    return label, "admin_calendar_update", data


def render_calendar(state: CalendarState) -> CalendarView:
    month_start = date(state.year, state.month, 1)
    month_end = date(state.year, state.month, monthrange(state.year, state.month)[1])
    month_selected = _intersects(state, month_start, month_end)
    year_selected = _intersects(state, date(state.year, 1, 1), date(state.year, 12, 31))
    rows = [
        [
            _button(
                MONTHS[state.month - 1], "show_months", icon_key="period_select", selected=month_selected
            ),
            _button(str(state.year), "show_years", icon_key="quote_expiry", selected=year_selected),
        ]
    ]
    if state.view == "days":
        rows.append([_button(day, "noop") for day in WEEKDAYS])
        for week in Calendar(firstweekday=0).monthdayscalendar(state.year, state.month):
            row = []
            for day in week:
                if day:
                    chosen = date(state.year, state.month, day)
                    row.append(
                        _button(
                            str(day),
                            "select_day",
                            value=chosen.isoformat(),
                            selected=_intersects(state, chosen, chosen),
                        )
                    )
                else:
                    row.append(_button("·", "noop"))
            rows.append(row)
        page = f"{MONTHS[state.month - 1]} {state.year}"
        previous_label, next_label = "Пред. месяц", "След. месяц"
    elif state.view == "months":
        buttons = [
            _button(
                label,
                "select_month",
                value=month,
                selected=_intersects(
                    state,
                    date(state.year, month, 1),
                    date(state.year, month, monthrange(state.year, month)[1]),
                ),
            )
            for month, label in enumerate(MONTHS, 1)
        ]
        rows.extend([buttons[index : index + 3] for index in range(0, 12, 3)])
        page = f"Месяцы · {state.year}"
        previous_label, next_label = "Пред. год", "След. год"
    else:
        first = MIN_YEAR + ((state.year - MIN_YEAR) // 12) * 12
        last = min(first + 11, MAX_YEAR)
        buttons = [
            _button(
                str(year),
                "select_year",
                value=year,
                selected=_intersects(state, date(year, 1, 1), date(year, 12, 31)),
            )
            for year in range(first, last + 1)
        ]
        rows.extend([buttons[index : index + 3] for index in range(0, len(buttons), 3)])
        page = f"Годы · {first}–{last}"
        previous_label, next_label = "Пред. годы", "След. годы"
    rows.append(
        [
            _button(previous_label, "previous", icon_key="admin_prev"),
            _button(next_label, "next", icon_key="admin_next"),
        ]
    )
    rows.append([_button("Сбросить период", "clear", icon_key="revoke_confirm")])
    if state.start is None:
        selection = "Выберите начало периода, затем конец."
    elif state.end is None:
        selection = f"Начало: {state.start:%d.%m.%Y}. Выберите конец периода."
    else:
        selection = f"Период: {state.start:%d.%m.%Y} — {state.end:%d.%m.%Y} включительно."
        rows.append(
            [
                (
                    "Применить период",
                    "admin_calendar_apply",
                    {
                        "icon_key": "checkout",
                        "semantic": "success",
                    },
                )
            ]
        )
    return CalendarView(f"{page}\n{selection}\nДаты указаны по Москве.", rows)
