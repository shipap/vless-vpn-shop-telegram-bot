import { useCallback, useEffect, useId, useRef, useState } from "react";
import { api } from "@api/client";
import { X } from "lucide-react";
import { Button, Empty, ErrorState, Loading } from "./components";
import { date, ContentOrText } from "./content";
import BellToggle from "./reactbits/BellToggle";
import SwipeToast from "./reactbits/SwipeToast";
import { notificationTarget } from "./notification-target";
import { BrowserNotifications } from "./browser-notifications";
import "./notifications.css";

export type NotificationAction = {
  label: string;
  target: "/#subscriptions";
  operation: "connect" | "devices" | "renew";
  metadata: { subscription_id: string; quote_id?: string };
};
export function validNotificationAction(
  value: unknown,
): value is NotificationAction {
  if (!value || typeof value !== "object") return false;
  const item = value as Record<string, unknown>;
  const metadata = item.metadata as Record<string, unknown> | undefined;
  return (
    typeof item.label === "string" &&
    item.label.length > 0 &&
    item.label.length <= 200 &&
    item.target === "/#subscriptions" &&
    typeof item.operation === "string" &&
    ["connect", "devices", "renew"].includes(item.operation) &&
    !!metadata &&
    typeof metadata === "object" &&
    !Array.isArray(metadata) &&
    typeof metadata.subscription_id === "string" &&
    metadata.subscription_id.length > 0 &&
    metadata.subscription_id.length <= 128 &&
    (metadata.quote_id === undefined ||
      (typeof metadata.quote_id === "string" &&
        metadata.quote_id.length > 0 &&
        metadata.quote_id.length <= 128))
  );
}

export type NotificationItem = {
  id: string;
  kind: string;
  title: string;
  body: string;
  severity: string;
  created_at: string;
  read_at: string | null;
  target: string | null;
  nodes?: unknown;
  actions?: NotificationAction[];
};
type Inbox = { items: NotificationItem[]; unread_count: number };

export function NotificationCenter({
  publicKey,
  accountId,
  onAction,
}: {
  publicKey?: string | null;
  accountId: string;
  onAction?: (action: NotificationAction) => void;
}) {
  const id = useId();
  const anchor = useRef<HTMLSpanElement>(null);
  const popover = useRef<HTMLDivElement>(null);
  const [open, setOpen] = useState(false);
  const [data, setData] = useState<Inbox | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const [filter, setFilter] = useState(false);
  const [toasts, setToasts] = useState<NotificationItem[]>([]);
  const known = useRef<Set<string> | null>(null);
  const controller = useRef<AbortController | null>(null);
  const alive = useRef(true);
  useEffect(() => {
    if (!open || !popover.current || !anchor.current) return;
    const panel = popover.current;
    const viewport = window.visualViewport;
    const position = () => {
      const box = anchor.current!.getBoundingClientRect();
      const viewportLeft = viewport?.offsetLeft ?? 0;
      const viewportTop = viewport?.offsetTop ?? 0;
      const width = viewport?.width ?? innerWidth;
      const height = viewport?.height ?? innerHeight;
      const panelWidth = Math.min(420, Math.max(0, width - 24));
      const left = Math.max(
        viewportLeft + 12,
        Math.min(
          box.right - panelWidth,
          viewportLeft + width - panelWidth - 12,
        ),
      );
      const minimumHeight = Math.min(160, Math.max(0, height - 24));
      const top = Math.max(
        viewportTop + 12,
        Math.min(box.bottom + 8, viewportTop + height - minimumHeight - 12),
      );
      panel.style.width = `${panelWidth}px`;
      panel.style.left = `${left}px`;
      panel.style.right = "auto";
      panel.style.top = `${top}px`;
      panel.style.maxHeight = `${Math.max(0, Math.min(560, viewportTop + height - top - 12))}px`;
    };
    position();
    panel.showPopover();
    window.addEventListener("resize", position);
    window.addEventListener("scroll", position, true);
    viewport?.addEventListener("resize", position);
    viewport?.addEventListener("scroll", position);
    const observer = new ResizeObserver(position);
    observer.observe(anchor.current);
    return () => {
      panel.hidePopover();
      window.removeEventListener("resize", position);
      window.removeEventListener("scroll", position, true);
      viewport?.removeEventListener("resize", position);
      viewport?.removeEventListener("scroll", position);
      observer.disconnect();
    };
  }, [open]);
  const load = useCallback(async () => {
    controller.current?.abort();
    const request = new AbortController();
    controller.current = request;
    try {
      const inbox = await api<Inbox>("/notifications?limit=50", {
        signal: request.signal,
      });
      if (request.signal.aborted || !alive.current) return;
      const items = inbox.items ?? [];
      if (known.current) {
        const previous = known.current;
        const arrivals = items.filter(
          (item) => !item.read_at && !previous.has(item.id),
        );
        if (arrivals.length)
          setToasts((current) => [...current, ...arrivals].slice(-3));
      } else known.current = new Set();
      for (const item of items) known.current.add(item.id);
      while (known.current.size > 1000)
        known.current.delete(known.current.values().next().value!);
      setData({ items, unread_count: inbox.unread_count ?? 0 });
      setError(null);
    } catch (failure) {
      if (!request.signal.aborted && alive.current) setError(failure);
    }
  }, []);
  useEffect(() => {
    const openInbox = () => {
      if (location.pathname === "/" && location.hash === "#notifications")
        setOpen(true);
    };
    openInbox();
    window.addEventListener("hashchange", openInbox);
    return () => window.removeEventListener("hashchange", openInbox);
  }, []);
  useEffect(() => {
    alive.current = true;
    let stream: EventSource | null = null;
    const connect = () => {
      stream?.close();
      stream = null;
      if (document.visibilityState !== "visible" || !navigator.onLine) return;
      void load();
      stream = new EventSource("/api/v1/notifications/stream", {
        withCredentials: true,
      });
      stream.addEventListener("notification", () => {
        void load();
      });
    };
    connect();
    const timer = window.setInterval(() => {
      if (document.visibilityState === "visible" && navigator.onLine)
        void load();
    }, 60000);
    document.addEventListener("visibilitychange", connect);
    window.addEventListener("online", connect);
    window.addEventListener("offline", connect);
    return () => {
      alive.current = false;
      stream?.close();
      controller.current?.abort();
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", connect);
      window.removeEventListener("online", connect);
      window.removeEventListener("offline", connect);
    };
  }, [load]);
  async function markRead(item?: NotificationItem) {
    if (busy) return;
    setBusy(true);
    try {
      await api(
        item
          ? `/notifications/${encodeURIComponent(item.id)}/read`
          : "/notifications/read-all",
        { method: "POST", body: {} },
      );
      if (alive.current) await load();
    } catch (failure) {
      if (alive.current) setError(failure);
    } finally {
      if (alive.current) setBusy(false);
    }
  }
  const count = data?.unread_count ?? 0;
  return (
    <>
      <span ref={anchor} className="notification-anchor">
        <BellToggle
          offLabel="Уведомления"
          onLabel="Уведомления"
          label={`Уведомления, ${count} ${count % 10 === 1 && count % 100 !== 11 ? "непрочитанное" : "непрочитанных"}`}
          count={count}
          pressed={count > 0}
          expanded={open}
          controls={id}
          popoverTarget={id}
          waves={false}
          clapper={false}
          ringAmplitude={6}
          ringPasses={2}
          ringDuration={220}
          color="var(--ink)"
          background="transparent"
          onColor="var(--ink)"
          onBackground="var(--surface)"
        />
      </span>
      <div
        ref={popover}
        popover="auto"
        role="dialog"
        aria-modal="false"
        aria-labelledby={`${id}-title`}
        className="notification-popover"
        id={id}
        onToggle={(event) => {
          setOpen((event.nativeEvent as ToggleEvent).newState === "open");
        }}
      >
        <div className="notification-popover-head">
          <h2 id={`${id}-title`}>Уведомления</h2>
          <button
            type="button"
            className="icon-button"
            aria-label="Закрыть уведомления"
            autoFocus
            popoverTarget={id}
            popoverTargetAction="hide"
          >
            <X size={18} />
          </button>
        </div>
        {open && (
          <div
            className="notification-inbox stack"
            role="region"
            aria-label="Список уведомлений"
            tabIndex={0}
          >
            <BrowserNotifications publicKey={publicKey} accountId={accountId} />
            <div className="notification-inbox-tools">
              <label className="checkbox-label">
                <input
                  type="checkbox"
                  checked={filter}
                  onChange={(event) => setFilter(event.target.checked)}
                />
                Только непрочитанные
              </label>
              <Button
                tone="secondary"
                busy={busy}
                disabled={!count}
                onClick={() => {
                  void markRead();
                }}
              >
                Прочитать все
              </Button>
            </div>
            {Boolean(error) && (
              <ErrorState
                error={error}
                onRetry={() => {
                  void load();
                }}
              />
            )}
            {!data && !error && <Loading />}
            {data &&
              !(
                filter ? data.items.filter((item) => !item.read_at) : data.items
              ).length && <Empty title="Новых уведомлений нет" />}
            {(filter
              ? data?.items.filter((item) => !item.read_at)
              : data?.items
            )?.map((item) => (
              <article
                className={`notification-item ${!item.read_at ? "unread" : ""}`}
                key={item.id}
              >
                <h3>{item.title}</h3>
                <ContentOrText nodes={item.nodes} text={item.body} />
                <p className="fine-print">
                  {date(item.created_at, true)} · МСК
                </p>
                <div className="notification-item-actions">
                  {onAction &&
                    (Array.isArray(item.actions) ? item.actions : [])
                      .filter(validNotificationAction)
                      .map((action, index) => (
                        <Button
                          key={index}
                          tone="secondary"
                          onClick={() => {
                            setOpen(false);
                            onAction(action);
                          }}
                        >
                          {action.label}
                        </Button>
                      ))}
                  {notificationTarget(item.target) && (
                    <a
                      href={notificationTarget(item.target)}
                      className="button secondary"
                      onClick={(event) => {
                        if (
                          notificationTarget(item.target) === "/#notifications"
                        ) {
                          event.preventDefault();
                          setOpen(true);
                        } else setOpen(false);
                      }}
                    >
                      {item.kind === "identity_linked" &&
                      notificationTarget(item.target) === "/#account"
                        ? "Отвязать Telegram"
                        : "Открыть"}
                    </a>
                  )}
                  {!item.read_at && (
                    <Button
                      tone="ghost"
                      busy={busy}
                      onClick={() => {
                        void markRead(item);
                      }}
                    >
                      Отметить прочитанным
                    </Button>
                  )}
                </div>
              </article>
            ))}
          </div>
        )}
      </div>
      <div className="notification-toast-stack">
        {toasts.map((item) => (
          <SwipeToast
            key={item.id}
            inline
            closeButton
            title={item.title}
            description={item.body}
            background="var(--surface)"
            color="var(--ink)"
            duration={6000}
            slideMs={220}
            settleBounce={0}
            actionLabel="Уведомления"
            onAction={() => setOpen(true)}
            onClose={() =>
              setToasts((current) =>
                current.filter((toast) => toast.id !== item.id),
              )
            }
          />
        ))}
      </div>
    </>
  );
}
