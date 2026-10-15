import { useEffect, useId, useRef, useState } from "react";
import { CalendarDays, ChevronLeft, ChevronRight } from "lucide-react";

type Mode = "date" | "datetime-local";
function isoDay(date: Date) {
  return `${String(date.getUTCFullYear()).padStart(4, "0")}-${String(date.getUTCMonth() + 1).padStart(2, "0")}-${String(date.getUTCDate()).padStart(2, "0")}`;
}
function dayDate(value: string) {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
  if (!match || Number(match[1]) < 1) return null;
  const date = new Date(0);
  date.setUTCHours(0, 0, 0, 0);
  date.setUTCFullYear(Number(match[1]), Number(match[2]) - 1, Number(match[3]));
  return isoDay(date) === value ? date : null;
}
export function validAdminDate(
  value: string,
  mode: Mode,
  min?: string,
  max?: string,
) {
  if (!value) return true;
  if (!dayDate(value.slice(0, 10))) return false;
  if (
    mode === "date"
      ? value.length !== 10
      : !/^\d{4}-\d{2}-\d{2}T([01]\d|2[0-3]):[0-5]\d$/.test(value)
  )
    return false;
  return (!min || value >= min) && (!max || value <= max);
}
export function AdminDateField({
  value,
  onChange,
  label,
  mode = "date",
  min,
  max,
  required = false,
}: {
  value: string;
  onChange: (value: string) => void;
  label: string;
  mode?: Mode;
  min?: string;
  max?: string;
  required?: boolean;
}) {
  const [draft, setDraft] = useState(value);
  const [clock, setClock] = useState(value.slice(11) || "00:00");
  const [open, setOpen] = useState(false);
  const [position, setPosition] = useState({ left: 16, top: 16 });
  const [month, setMonth] = useState(
    () => dayDate(value.slice(0, 10)) ?? new Date(),
  );
  const input = useRef<HTMLInputElement>(null),
    trigger = useRef<HTMLButtonElement>(null),
    popup = useRef<HTMLDivElement>(null);
  const popupId = useId();
  useEffect(() => {
    setDraft(value);
    setClock(value.slice(11) || "00:00");
  }, [value]);
  useEffect(() => {
    input.current?.setCustomValidity(
      validAdminDate(draft, mode, min, max)
        ? ""
        : "Введите существующую дату в указанном формате и допустимом диапазоне.",
    );
  }, [draft, mode, min, max]);
  useEffect(() => {
    if (!open) return;
    const selected = dayDate(draft.slice(0, 10));
    const selectedButton = selected
      ? popup.current?.querySelector<HTMLButtonElement>(
          `button[data-day="${isoDay(selected)}"]:not(:disabled)`,
        )
      : null;
    (
      selectedButton ??
      popup.current?.querySelector<HTMLButtonElement>(
        "button[data-day]:not(:disabled)",
      )
    )?.focus();
  }, [open]);
  useEffect(() => {
    if (!open) return;
    function place() {
      if (!input.current || !popup.current) return;
      const control = input.current.getBoundingClientRect();
      const panel = popup.current.getBoundingClientRect();
      setPosition({
        left: Math.max(
          16,
          Math.min(control.left, innerWidth - panel.width - 16),
        ),
        top: Math.max(
          16,
          Math.min(control.bottom + 8, innerHeight - panel.height - 16),
        ),
      });
    }
    place();
    window.addEventListener("resize", place);
    window.addEventListener("scroll", place, true);
    return () => {
      window.removeEventListener("resize", place);
      window.removeEventListener("scroll", place, true);
    };
  }, [open, month]);
  function change(next: string) {
    setDraft(next);
    if (mode === "datetime-local" && next.includes("T"))
      setClock(next.slice(11));
    if (validAdminDate(next, mode, min, max)) onChange(next);
  }
  function close() {
    setOpen(false);
    trigger.current?.focus();
  }
  function candidate(day: string) {
    return mode === "date" ? day : `${day}T${clock}`;
  }
  function moveMonth(offset: number) {
    const next = new Date(month);
    next.setUTCDate(1);
    next.setUTCMonth(next.getUTCMonth() + offset);
    if (next.getUTCFullYear() >= 1 && next.getUTCFullYear() <= 9999)
      setMonth(next);
  }
  const year = month.getUTCFullYear(),
    index = month.getUTCMonth();
  const first = new Date(month);
  first.setUTCDate(1);
  const last = new Date(first);
  last.setUTCMonth(index + 1);
  last.setUTCDate(0);
  const offset = (first.getUTCDay() + 6) % 7;
  const days = Array.from(
    { length: Math.ceil((offset + last.getUTCDate()) / 7) * 7 },
    (_, i) => {
      const number = i - offset + 1;
      return number > 0 && number <= last.getUTCDate()
        ? `${String(year).padStart(4, "0")}-${String(index + 1).padStart(2, "0")}-${String(number).padStart(2, "0")}`
        : null;
    },
  );
  const monthTitle = new Intl.DateTimeFormat("ru-RU", {
    month: "long",
    year: "numeric",
    timeZone: "UTC",
  }).format(first);
  return (
    <div
      className="admin-date-field"
      onBlurCapture={(event) => {
        if (!event.currentTarget.contains(event.relatedTarget as Node | null))
          setOpen(false);
      }}
      onKeyDown={(event) => {
        if (open && event.key === "Escape") {
          event.preventDefault();
          event.stopPropagation();
          close();
        }
      }}
    >
      <div className="admin-date-control">
        <input
          ref={input}
          value={draft}
          aria-label={label}
          required={required}
          placeholder={mode === "date" ? "ГГГГ-ММ-ДД" : "ГГГГ-ММ-ДД ЧЧ:ММ"}
          onChange={(event) => change(event.target.value.replace(" ", "T"))}
          onKeyDown={(event) => {
            if (event.altKey && event.key === "ArrowDown") {
              event.preventDefault();
              setMonth(dayDate(draft.slice(0, 10)) ?? new Date());
              setOpen(true);
            }
          }}
        />
        <button
          ref={trigger}
          type="button"
          className="icon-button"
          aria-label={`Открыть календарь: ${label}`}
          aria-expanded={open}
          aria-controls={popupId}
          onClick={() => {
            setMonth(dayDate(draft.slice(0, 10)) ?? new Date());
            setOpen(!open);
          }}
        >
          <CalendarDays size={18} aria-hidden="true" />
        </button>
      </div>
      {open && (
        <div
          ref={popup}
          id={popupId}
          role="dialog"
          aria-label={`Календарь: ${label}`}
          className="admin-calendar"
          style={position}
        >
          <div className="admin-calendar-heading">
            <button
              type="button"
              className="icon-button"
              aria-label="Предыдущий месяц"
              disabled={year === 1 && index === 0}
              onClick={() => moveMonth(-1)}
            >
              <ChevronLeft size={18} />
            </button>
            <span aria-live="polite">{monthTitle}</span>
            <button
              type="button"
              className="icon-button"
              aria-label="Следующий месяц"
              disabled={year === 9999 && index === 11}
              onClick={() => moveMonth(1)}
            >
              <ChevronRight size={18} />
            </button>
          </div>
          <table aria-label={monthTitle}>
            <thead>
              <tr>
                {["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"].map((day) => (
                  <th key={day} scope="col">
                    {day}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {Array.from({ length: days.length / 7 }, (_, row) => (
                <tr key={row}>
                  {days.slice(row * 7, row * 7 + 7).map((day, column) => (
                    <td key={column}>
                      {day && (
                        <button
                          type="button"
                          data-day={day}
                          aria-pressed={draft.slice(0, 10) === day}
                          aria-label={`${Number(day.slice(8))} ${new Intl.DateTimeFormat("ru-RU", { month: "long", day: "numeric", timeZone: "UTC" }).formatToParts(dayDate(day)!).find((part) => part.type === "month")!.value} ${year}`}
                          disabled={
                            !validAdminDate(candidate(day), mode, min, max)
                          }
                          onClick={() => {
                            change(candidate(day));
                            close();
                          }}
                          onKeyDown={(event) => {
                            const shift = {
                              ArrowLeft: -1,
                              ArrowRight: 1,
                              ArrowUp: -7,
                              ArrowDown: 7,
                            }[event.key as "ArrowLeft"];
                            if (!shift) return;
                            event.preventDefault();
                            const next = dayDate(day)!;
                            next.setUTCDate(next.getUTCDate() + shift);
                            const nextDay = isoDay(next);
                            if (
                              !validAdminDate(
                                candidate(nextDay),
                                mode,
                                min,
                                max,
                              )
                            )
                              return;
                            setMonth(next);
                            requestAnimationFrame(() =>
                              popup.current
                                ?.querySelector<HTMLButtonElement>(
                                  `button[data-day="${nextDay}"]`,
                                )
                                ?.focus(),
                            );
                          }}
                        >
                          {Number(day.slice(8))}
                        </button>
                      )}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
          {mode === "datetime-local" && (
            <label>
              Время · ЧЧ:ММ
              <input
                aria-label={`Время: ${label}`}
                inputMode="numeric"
                value={clock}
                onChange={(event) => {
                  setClock(event.target.value);
                  if (draft.slice(0, 10))
                    change(`${draft.slice(0, 10)}T${event.target.value}`);
                }}
              />
            </label>
          )}
          <div className="admin-calendar-actions">
            <button
              type="button"
              className="button secondary"
              onClick={() => {
                change("");
                close();
              }}
            >
              Очистить
            </button>
            <button type="button" className="button secondary" onClick={close}>
              Закрыть календарь
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
