import { useEffect, useRef, useState } from "react";
import { ArrowRight, Send } from "lucide-react";
import { ApiError, api, setCsrf, useApi } from "@api/client";
import type { Legal, Session } from "@api/types";
import { Brand, Button, ErrorState, Loading } from "@ui/components";
import { LegalCheckbox, LegalLinks } from "@ui/legal";
import { referralPayload } from "@ui/referral";

export function TelegramMiniAppLogin({
  initData,
  autoResume,
  onLogin,
  onEmail,
}: {
  initData: string;
  autoResume: boolean;
  onLogin: (session: Session) => void;
  onEmail: () => void;
}) {
  const legal = useApi<Legal>("/legal");
  const [accepted, setAccepted] = useState(false);
  const [needsConsent, setNeedsConsent] = useState(false);
  const [busy, setBusy] = useState(autoResume);
  const [error, setError] = useState<unknown>(null);
  const inFlight = useRef(false);
  const attempted = useRef(false);
  const mounted = useRef(true);
  const controller = useRef<AbortController | null>(null);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      controller.current?.abort();
    };
  }, []);
  async function authenticate(consented: boolean) {
    if (
      inFlight.current ||
      (consented && (!accepted || !legal.data?.configured || legal.loading))
    )
      return;
    inFlight.current = true;
    setBusy(true);
    setError(null);
    const request = new AbortController();
    controller.current = request;
    try {
      const value = await api<Session>("/auth/telegram/miniapp", {
        method: "POST",
        signal: request.signal,
        body: {
          init_data: initData,
          ...referralPayload(),
          ...(consented
            ? { legal_accepted: true, documents: legal.data!.documents }
            : {}),
        },
      });
      if (!mounted.current) return;
      setCsrf(value.csrf_token);
      onLogin(value);
    } catch (error) {
      if (!mounted.current || request.signal.aborted) return;
      setError(error);
      if (error instanceof ApiError && error.code.startsWith("legal_")) {
        setNeedsConsent(true);
        setAccepted(false);
        legal.reload();
      }
    } finally {
      inFlight.current = false;
      controller.current = null;
      if (mounted.current) setBusy(false);
    }
  }
  useEffect(() => {
    if (autoResume && !attempted.current) {
      attempted.current = true;
      void authenticate(false);
    }
  }, [autoResume, initData]);
  return (
    <main className="standalone telegram-auth">
      <Brand />
      <section className="card">
        <span className="square-icon">
          <Send />
        </span>
        <h1>Вход через Telegram</h1>
        <p>Подписки, устройства и платежи доступны в вашем кабинете.</p>
        {needsConsent && (
          <LegalCheckbox
            legal={legal.data}
            checked={accepted}
            onChange={setAccepted}
            context="login"
          />
        )}
        {busy ? (
          <Loading />
        ) : (
          <Button
            onClick={() => authenticate(needsConsent)}
            disabled={
              needsConsent &&
              (!accepted || !legal.data?.configured || legal.loading)
            }
          >
            Продолжить в Telegram
            <ArrowRight size={17} />
          </Button>
        )}
        {Boolean(error) &&
          !(error instanceof ApiError && error.code.startsWith("legal_")) && (
            <ErrorState error={error} />
          )}
        {Boolean(legal.error) && (
          <ErrorState error={legal.error} onRetry={legal.reload} />
        )}
        <Button tone="secondary" onClick={onEmail}>
          Войти по email
        </Button>
      </section>
      <LegalLinks legal={legal.data} />
    </main>
  );
}

export function TelegramWebsiteLogin({
  enabled,
  accepted,
  legal,
  onStale,
}: {
  enabled: boolean;
  accepted: boolean;
  legal: Legal | null;
  onStale: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(() => {
    const url = new URL(location.href);
    if (!url.searchParams.has("telegram_login_error")) return null;
    url.searchParams.delete("telegram_login_error");
    history.replaceState(null, "", url.pathname + url.search + url.hash);
    return new Error(
      "Не удалось войти через Telegram. Попробуйте снова или войдите по email.",
    );
  });
  const inFlight = useRef(false);
  async function start() {
    if (inFlight.current || !enabled || !accepted || !legal?.configured) return;
    inFlight.current = true;
    setBusy(true);
    setError(null);
    try {
      const result = await api<{ authorization_url: string }>(
        "/auth/telegram/start",
        {
          method: "POST",
          body: {
            legal_accepted: true,
            documents: legal.documents,
            ...referralPayload(),
          },
        },
      );
      const url = new URL(result.authorization_url);
      if (
        url.protocol !== "https:" ||
        url.hostname !== "oauth.telegram.org" ||
        url.pathname !== "/auth" ||
        url.username ||
        url.password ||
        url.port
      )
        throw new Error(
          "Не удалось открыть вход через Telegram. Попробуйте позже.",
        );
      location.assign(url.href);
    } catch (error) {
      setError(error);
      if (error instanceof ApiError && error.code.startsWith("legal_"))
        onStale();
      inFlight.current = false;
      setBusy(false);
    }
  }
  return (
    <div className="telegram-website-login">
      <Button
        className="telegram-login-button"
        type="button"
        busy={busy}
        disabled={!enabled || !accepted || !legal?.configured}
        onClick={start}
      >
        <svg width="20" height="20" viewBox="0 0 24 24" aria-hidden="true">
          <path
            fill="currentColor"
            d="M21.4 3.3 18.1 20c-.2 1.2-.9 1.5-1.9.9l-5-3.7-2.4 2.3c-.3.3-.5.5-1 .5l.4-5.1 9.3-8.4c.4-.4-.1-.6-.6-.3L5.4 13.5.5 12c-1.1-.3-1.1-1.1.2-1.6L20 3c.9-.3 1.6.2 1.4 1.3Z"
          />
        </svg>
        Войти через Telegram
      </Button>
      {!enabled && (
        <p className="fine-print">Вход через Telegram временно недоступен.</p>
      )}
      {enabled && !accepted && (
        <p className="fine-print">
          Для входа подтвердите согласие с документами ниже.
        </p>
      )}
      {Boolean(error) && <ErrorState error={error} />}
    </div>
  );
}
