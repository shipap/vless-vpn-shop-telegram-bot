import { useEffect, useRef, useState } from "react";
import { api, useApi } from "@api/client";
import { Button, ErrorState } from "./components";

type Preferences = {
  browser_enabled: boolean;
  push_subscriptions: { id: string; created_at: string | null }[];
};
const supported = () =>
  typeof Notification !== "undefined" &&
  "serviceWorker" in navigator &&
  "PushManager" in window &&
  window.isSecureContext;
const rememberedKey = (accountId: string) =>
  `sunrise.push.device.v1.${accountId}`;
function rememberedId(accountId: string): string | null {
  try {
    const id = localStorage.getItem(rememberedKey(accountId));
    return id && /^[A-Za-z0-9_.:-]{1,128}$/.test(id) ? id : null;
  } catch {
    return null;
  }
}
function rememberId(accountId: string, id: string | null) {
  try {
    if (id) localStorage.setItem(rememberedKey(accountId), id);
    else localStorage.removeItem(rememberedKey(accountId));
  } catch {
    /* Серверные записи остаются единственным источником разрешения. */
  }
}

export function BrowserNotifications({
  publicKey,
  accountId,
}: {
  publicKey?: string | null;
  accountId: string;
}) {
  const preferences = useApi<Preferences>("/notifications/preferences");
  const [permission, setPermission] = useState<
    NotificationPermission | "unsupported"
  >(() => (supported() ? Notification.permission : "unsupported"));
  const [subscriptionId, setSubscriptionId] = useState<string | null>(null);
  const subscription = useRef<PushSubscription | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const alive = useRef(true);
  const abort = useRef(new AbortController());
  const working = useRef(false);
  async function save(native: PushSubscription) {
    const payload = native.toJSON();
    if (!payload.endpoint || !payload.keys?.p256dh || !payload.keys?.auth)
      throw new Error(
        "Браузер не передал данные подписки. Попробуйте ещё раз.",
      );
    const result = await api<{ id: string; browser_enabled: boolean }>(
      "/notifications/push",
      {
        method: "POST",
        body: {
          endpoint: payload.endpoint,
          keys: { p256dh: payload.keys.p256dh, auth: payload.keys.auth },
        },
        signal: abort.current.signal,
      },
    );
    if (!result.id)
      throw new Error(
        "Сервис не подтвердил подписку браузера. Попробуйте ещё раз.",
      );
    const enabled = await api<{ browser_enabled: boolean }>(
      "/notifications/preferences",
      {
        method: "PATCH",
        body: { browser_enabled: true },
        signal: abort.current.signal,
      },
    );
    if (!enabled.browser_enabled)
      throw new Error(
        "Сервис не подтвердил включение уведомлений. Попробуйте ещё раз.",
      );
    if (alive.current && !abort.current.signal.aborted) {
      subscription.current = native;
      setSubscriptionId(result.id);
      rememberId(accountId, result.id);
      preferences.setData({
        browser_enabled: true,
        push_subscriptions: [
          ...(preferences.data?.push_subscriptions ?? []).filter(
            (item) => item.id !== result.id,
          ),
          { id: result.id, created_at: null },
        ],
      });
    }
  }
  useEffect(() => {
    alive.current = true;
    abort.current = new AbortController();
    return () => {
      alive.current = false;
      abort.current.abort();
    };
  }, []);
  useEffect(() => {
    let cancelled = false;
    const ownPreferences = preferences.data;
    if (ownPreferences && !ownPreferences.browser_enabled) {
      setSubscriptionId(null);
      subscription.current = null;
      rememberId(accountId, null);
    }
    if (
      supported() &&
      Notification.permission === "granted" &&
      ownPreferences?.browser_enabled &&
      ownPreferences.push_subscriptions?.length &&
      !subscription.current
    ) {
      const id = rememberedId(accountId);
      if (
        !id ||
        !ownPreferences.push_subscriptions.some((item) => item.id === id)
      )
        return;
      void navigator.serviceWorker
        .getRegistration("/")
        .then((registration) => registration?.pushManager.getSubscription())
        .then((native) => {
          if (native && alive.current && !cancelled) {
            subscription.current = native;
            setSubscriptionId(id);
          }
        })
        .catch((failure) => {
          if (alive.current && !cancelled) setError(failure);
        });
    }
    return () => {
      cancelled = true;
    };
  }, [preferences.data, accountId]);
  async function enable() {
    if (working.current || !publicKey || !supported()) return;
    working.current = true;
    setBusy(true);
    setError(null);
    try {
      const result =
        Notification.permission === "default"
          ? await Notification.requestPermission()
          : Notification.permission;
      if (!alive.current) return;
      setPermission(result);
      if (result !== "granted") return;
      await navigator.serviceWorker.register("/sw.js", { scope: "/" });
      let timer: ReturnType<typeof setTimeout> | undefined;
      const registration = await Promise.race([
        navigator.serviceWorker.ready,
        new Promise<never>((_, reject) => {
          timer = setTimeout(
            () =>
              reject(
                new Error(
                  "Браузер не смог подготовить уведомления. Попробуйте ещё раз.",
                ),
              ),
            10000,
          );
        }),
      ]).finally(() => clearTimeout(timer));
      if (!alive.current) return;
      const native =
        (await registration.pushManager.getSubscription()) ??
        (await registration.pushManager.subscribe({
          userVisibleOnly: true,
          applicationServerKey: Uint8Array.from(
            atob(publicKey.replace(/-/g, "+").replace(/_/g, "/")),
            (character) => character.charCodeAt(0),
          ),
        }));
      if (alive.current) await save(native);
      else await native.unsubscribe();
    } catch (failure) {
      if (alive.current) setError(failure);
    } finally {
      working.current = false;
      if (alive.current) setBusy(false);
    }
  }
  async function disable() {
    if (!subscriptionId || working.current) return;
    working.current = true;
    setBusy(true);
    setError(null);
    try {
      await api(`/notifications/push/${encodeURIComponent(subscriptionId)}`, {
        method: "DELETE",
        signal: abort.current.signal,
      });
      if (alive.current) setSubscriptionId(null);
      rememberId(accountId, null);
      if (alive.current) preferences.reload();
      const removed = await subscription.current?.unsubscribe();
      subscription.current = null;
      if (removed === false)
        throw new Error(
          "Доставка отключена. Браузер не подтвердил удаление подписки; проверьте его настройки.",
        );
    } catch (failure) {
      if (alive.current) setError(failure);
    } finally {
      working.current = false;
      if (alive.current) setBusy(false);
    }
  }
  const notice =
    permission === "unsupported"
      ? "Этот браузер не поддерживает уведомления. Все сообщения доступны в кабинете."
      : permission === "denied"
        ? "Уведомления запрещены в настройках браузера. Измените разрешение для этого сайта, чтобы включить доставку."
        : subscriptionId
          ? "Уведомления включены на этом устройстве."
          : !publicKey
            ? "Уведомления в браузере не настроены. Все сообщения доступны в этом кабинете."
            : permission === "granted"
              ? "Разрешение получено. Включите доставку на этом устройстве."
              : "Разрешите браузеру показывать новые сообщения. Разрешение можно отозвать в его настройках.";
  return (
    <section className="browser-notifications">
      <h3>Уведомления в браузере</h3>
      <p className="fine-print" role="status">
        {notice}
      </p>
      {!subscriptionId && preferences.data?.browser_enabled && (
        <p className="fine-print">Доставка настроена на другом устройстве.</p>
      )}
      {Boolean(error || preferences.error) && (
        <ErrorState error={error || preferences.error} />
      )}
      {subscriptionId ? (
        <Button
          tone="secondary"
          busy={busy}
          onClick={() => {
            void disable();
          }}
        >
          Отключить на этом устройстве
        </Button>
      ) : (
        <Button
          tone="secondary"
          busy={busy}
          disabled={
            !publicKey ||
            permission === "unsupported" ||
            permission === "denied"
          }
          onClick={() => {
            void enable();
          }}
        >
          {permission === "granted"
            ? "Включить на этом устройстве"
            : "Разрешить уведомления в браузере"}
        </Button>
      )}
    </section>
  );
}
