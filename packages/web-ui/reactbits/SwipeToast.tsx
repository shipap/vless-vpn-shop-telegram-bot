// Copyright (c) 2026 shipap. SPDX-License-Identifier: AGPL-3.0-only
// Самостоятельная реализация; прежний путь — только контракт импортов.
import React, {
  useCallback,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { X } from "lucide-react";
import "./SwipeToast.css";

export type SwipeToastCloseReason =
  "timeout" | "swipe" | "action" | "close" | "escape" | "programmatic";
export type SwipeToastFuse = "bottom" | "top" | "none";
export type SwipeToastPhase = "open" | "closing" | "gone";
export interface SwipeToastProps {
  title?: ReactNode;
  description?: ReactNode;
  icon?: ReactNode;
  actionLabel?: ReactNode;
  onAction?: () => void;
  open?: boolean;
  onClose?: (reason: SwipeToastCloseReason) => void;
  background?: string;
  color?: string;
  fuseColor?: string;
  width?: number;
  radius?: number;
  slideMs?: number;
  settleBounce?: number;
  swipeDistance?: number;
  duration?: number;
  fuse?: SwipeToastFuse;
  pauseOnHover?: boolean;
  closeButton?: boolean;
  inline?: boolean;
  dismissible?: boolean;
  className?: string;
}
export default function SwipeToast({
  title,
  description,
  icon,
  actionLabel,
  onAction,
  open = true,
  onClose,
  background,
  color,
  width = 360,
  radius = 10,
  duration = 6000,
  swipeDistance = 72,
  pauseOnHover = true,
  closeButton = true,
  inline = false,
  dismissible = true,
  className = "",
}: SwipeToastProps) {
  const [visible, setVisible] = useState(open);
  const [hovered, setHovered] = useState(false);
  const [focused, setFocused] = useState(false);
  const [dragging, setDragging] = useState(false);
  const [hidden, setHidden] = useState(
    () => typeof document !== "undefined" && document.hidden,
  );
  const closed = useRef(!open);
  const card = useRef<HTMLDivElement>(null);
  const remaining = useRef(duration);
  const callbacks = useRef({ onClose, onAction });
  callbacks.current = { onClose, onAction };
  const drag = useRef<{ id: number; start: number } | null>(null);
  const finish = useCallback((reason: SwipeToastCloseReason) => {
    if (closed.current) return;
    closed.current = true;
    setVisible(false);
    callbacks.current.onClose?.(reason);
  }, []);
  useEffect(() => {
    if (!open) {
      finish("programmatic");
      return;
    }
    closed.current = false;
    remaining.current = Math.max(0, duration);
    setVisible(true);
  }, [open, duration, finish]);
  useEffect(() => {
    const changed = () => setHidden(document.hidden);
    document.addEventListener("visibilitychange", changed);
    return () => document.removeEventListener("visibilitychange", changed);
  }, []);
  useEffect(() => {
    if (!open || !visible) return;
    drag.current = null;
    setDragging(false);
    setHovered(Boolean(pauseOnHover && card.current?.matches(":hover")));
    setFocused(Boolean(card.current?.contains(document.activeElement)));
    setHidden(document.hidden);
  }, [open, visible, pauseOnHover]);
  const paused = hovered || focused || dragging || hidden;
  useEffect(() => {
    if (!open || !visible || paused || duration <= 0 || closed.current) return;
    const started = Date.now();
    const timer = window.setTimeout(() => finish("timeout"), remaining.current);
    return () => {
      window.clearTimeout(timer);
      remaining.current = Math.max(
        0,
        remaining.current - (Date.now() - started),
      );
    };
  }, [open, visible, paused, duration, finish]);
  const cancelDrag = () => {
    drag.current = null;
    setDragging(false);
  };
  if (!open || !visible) return null;
  return (
    <div
      className={`swipe-toast ${inline ? "swipe-toast--inline" : ""} ${className}`}
      style={{ width, maxWidth: "100%" }}
    >
      <div
        ref={card}
        className="swipe-toast__card"
        role="status"
        aria-live="polite"
        aria-atomic="true"
        tabIndex={0}
        style={{ background, color, borderRadius: radius }}
        onMouseEnter={() => {
          if (pauseOnHover) setHovered(true);
        }}
        onMouseLeave={() => setHovered(false)}
        onFocusCapture={() => setFocused(true)}
        onBlurCapture={(event) => {
          if (!event.currentTarget.contains(event.relatedTarget as Node | null))
            setFocused(false);
        }}
        onKeyDown={(event) => {
          if (event.key === "Escape" && dismissible) {
            event.stopPropagation();
            finish("escape");
          }
        }}
        onPointerDown={(event) => {
          if (
            !dismissible ||
            drag.current !== null ||
            event.button !== 0 ||
            (event.target as Element).closest("button,a,input,select,textarea")
          )
            return;
          drag.current = {
            id: event.pointerId,
            start: event.clientY,
          };
          setDragging(true);
          if (event.currentTarget.hasPointerCapture(event.pointerId)) return;
          try {
            event.currentTarget.setPointerCapture(event.pointerId);
          } catch {
            /* Синтетический указатель не имеет захвата. */
          }
        }}
        onPointerCancel={cancelDrag}
        onLostPointerCapture={cancelDrag}
        onPointerUp={(event) => {
          const current = drag.current;
          if (!current || current.id !== event.pointerId) return;
          cancelDrag();
          if (event.currentTarget.hasPointerCapture(event.pointerId))
            event.currentTarget.releasePointerCapture(event.pointerId);
          if (
            Math.abs(event.clientY - current.start) >=
            Math.max(1, swipeDistance)
          )
            finish("swipe");
        }}
      >
        {icon && (
          <span className="swipe-toast__icon" aria-hidden="true">
            {icon}
          </span>
        )}
        <div className="swipe-toast__body">
          {title && <div className="swipe-toast__title">{title}</div>}
          {description && (
            <div className="swipe-toast__description">{description}</div>
          )}
          {actionLabel && (
            <button
              type="button"
              className="swipe-toast__action"
              onClick={() => {
                if (closed.current) return;
                finish("action");
                callbacks.current.onAction?.();
              }}
            >
              {actionLabel}
            </button>
          )}
        </div>
        {closeButton && dismissible && (
          <button
            type="button"
            className="swipe-toast__close"
            aria-label="Закрыть уведомление"
            onClick={() => finish("close")}
          >
            <X aria-hidden="true" size={18} />
          </button>
        )}
      </div>
    </div>
  );
}
