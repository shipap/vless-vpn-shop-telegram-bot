// Copyright (c) 2026 shipap. SPDX-License-Identifier: AGPL-3.0-only
// Самостоятельный элемент; путь сохранён для совместимости импортов.
import React, { useState, type ReactNode } from "react";
import { Bell } from "lucide-react";
import "./BellToggle.css";

export type BellToggleSize = "sm" | "md" | "lg";
export interface BellToggleProps {
  popoverTarget?: string;
  controls?: string;
  expanded?: boolean;
  offLabel?: string;
  onLabel?: string;
  icon?: ReactNode;
  label?: string;
  color?: string;
  background?: string;
  onColor?: string;
  onBackground?: string;
  size?: BellToggleSize;
  radius?: number;
  count?: number;
  badge?: boolean;
  badgeColor?: string;
  badgeTextColor?: string;
  pressed?: boolean;
  defaultPressed?: boolean;
  onChange?: (pressed: boolean) => void;
  disabled?: boolean;
  className?: string;
  ringAmplitude?: number;
  ringPasses?: number;
  ringDecay?: number;
  ringDuration?: number;
  ringPivot?: number;
  crossfadeMs?: number;
  revealBounce?: number;
  waves?: boolean;
  clapper?: boolean;
}
export default function BellToggle({
  popoverTarget,
  controls,
  expanded,
  label,
  offLabel = "Уведомления",
  onLabel = "Уведомления",
  icon,
  count = 0,
  badge = true,
  pressed,
  defaultPressed = false,
  onChange,
  disabled,
  className = "",
  size = "md",
  radius,
  color,
  background,
  onColor,
  onBackground,
  badgeColor,
  badgeTextColor,
}: BellToggleProps) {
  const [local, setLocal] = useState(defaultPressed);
  const active = pressed ?? local;
  const unread = Number.isFinite(count) ? Math.max(0, Math.floor(count)) : 0;
  return (
    <span className={`bell-toggle ${className}`}>
      <button
        type="button"
        className="bell-toggle__button"
        data-size={size}
        popoverTarget={popoverTarget}
        aria-controls={controls ?? popoverTarget}
        aria-expanded={expanded}
        aria-pressed={popoverTarget ? undefined : active}
        aria-label={label ?? (active ? onLabel : offLabel)}
        disabled={disabled}
        style={{
          color: active ? (onColor ?? color) : color,
          background: active ? (onBackground ?? background) : background,
          borderRadius: radius,
        }}
        onClick={() => {
          if (!popoverTarget) {
            setLocal(!active);
            onChange?.(!active);
          }
        }}
      >
        <span aria-hidden="true">{icon ?? <Bell size={22} />}</span>
        {badge && unread > 0 && (
          <span
            className="bell-toggle__badge"
            aria-hidden="true"
            style={{ background: badgeColor, color: badgeTextColor }}
          >
            {unread > 99 ? "99+" : unread}
          </span>
        )}
      </button>
    </span>
  );
}
