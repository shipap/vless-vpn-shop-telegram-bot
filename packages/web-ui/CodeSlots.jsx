// Copyright (c) 2026 shipap. SPDX-License-Identifier: AGPL-3.0-only
// Самостоятельная реализация; прежнее имя сохранено для совместимости импортов.
import React, { useEffect, useId, useRef, useState } from "react";
import "./CodeSlots.css";

export const authCodeSlotsProps = { length: 6, required: true };

export default function CodeSlots({
  length = 6,
  value,
  defaultValue = "",
  onChange,
  onComplete,
  status = "idle",
  mask = false,
  disabled = false,
  autoFocus = false,
  ariaLabel = "Код подтверждения",
  ariaDescribedBy,
  required = false,
  name = "code",
  className = "",
  accentColor,
  inkColor,
  slotColor,
  digitColor,
  dangerColor,
  slotSize,
  gap,
  radius,
}) {
  const size =
    Number.isInteger(length) && length > 0 ? Math.min(length, 32) : 6;
  const normalize = (text) =>
    String(text ?? "")
      .replace(/[^0-9]/g, "")
      .slice(0, size);
  const [localValue, setLocalValue] = useState(() => normalize(defaultValue));
  const current = normalize(value === undefined ? localValue : value);
  const field = useRef(null);
  const completed = useRef("");
  useEffect(() => {
    if (current.length < size) completed.current = "";
  }, [current, size]);
  const helpId = useId();
  const update = (text) => {
    const next = normalize(text);
    if (value === undefined) setLocalValue(next);
    onChange?.(next);
    if (next.length === size && completed.current !== next) {
      completed.current = next;
      onComplete?.(next);
    } else if (next.length < size) completed.current = "";
  };
  return (
    <span
      className={`code-slots ${className}`}
      data-status={status}
      style={{
        "--otp-count": size,
        "--otp-accent": accentColor || inkColor,
        "--otp-paper": slotColor,
        "--otp-text": digitColor,
        "--otp-error": dangerColor,
        "--otp-size": slotSize ? `${slotSize}px` : undefined,
        "--otp-gap": gap !== undefined ? `${gap}px` : undefined,
        "--otp-radius": radius !== undefined ? `${radius}px` : undefined,
      }}
    >
      <span
        className="code-slots__row"
        aria-hidden="true"
        onClick={() => field.current?.focus()}
      >
        {Array.from({ length: size }, (_, index) => (
          <span
            className="code-slots__slot"
            data-filled={current[index] ? "" : undefined}
            key={index}
          >
            {current[index] ? (mask ? "•" : current[index]) : ""}
          </span>
        ))}
      </span>
      <input
        ref={field}
        className="code-slots__input"
        type={mask ? "password" : "text"}
        name={name}
        value={current}
        onChange={(event) => update(event.target.value)}
        onPaste={(event) => {
          if (disabled) return;
          event.preventDefault();
          const input = event.currentTarget;
          const start = input.selectionStart ?? current.length;
          const end = input.selectionEnd ?? start;
          const inserted = event.clipboardData
            .getData("text/plain")
            .replace(/[^0-9]/g, "");
          update(current.slice(0, start) + inserted + current.slice(end));
        }}
        autoComplete="one-time-code"
        inputMode="numeric"
        pattern={`[0-9]{${size}}`}
        minLength={size}
        maxLength={size}
        disabled={disabled}
        autoFocus={autoFocus}
        required={required}
        aria-label={ariaLabel}
        aria-invalid={status === "error"}
        aria-describedby={[ariaDescribedBy, helpId].filter(Boolean).join(" ")}
      />
      <span id={helpId} className="code-slots__hint">
        Введите {size} цифр.
      </span>
    </span>
  );
}
