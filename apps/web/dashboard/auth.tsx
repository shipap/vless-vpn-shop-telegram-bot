import { messages } from "@ui/messages";
import { useInstance } from "@ui/instance";
import { instanceTerminology } from "@ui/terminology";
import { useEffect, useRef, useState, type FormEvent } from "react";
import { ArrowLeft, ArrowRight, Mail, ShieldCheck, Sun } from "lucide-react";
import { ApiError, api, setCsrf, useApi } from "@api/client";
import type { PublicConfig, Session, Legal } from "@api/types";
import { LegalLinks, LegalCheckbox } from "@ui/legal";
import { Brand, Button, ErrorState } from "@ui/components";
import { safeUrl } from "@ui/content";
import CodeSlots, {
  authCodeSlotsProps,
  type CodeSlotsStatus,
} from "@ui/CodeSlots";
import { TelegramWebsiteLogin } from "./telegram-auth";
import { browserReferral, referralPayload } from "@ui/referral";

declare global {
  interface Window {
    turnstile?: {
      render: (
        element: HTMLElement,
        options: Record<string, unknown>,
      ) => string;
      remove: (id: string) => void;
      reset: (id: string) => void;
    };
  }
}
function Turnstile({
  siteKey,
  onToken,
  resetKey,
}: {
  siteKey: string;
  onToken: (value: string) => void;
  resetKey: number;
}) {
  const target = useRef<HTMLDivElement>(null);
  const widget = useRef<string | null>(null);
  const [size, setSize] = useState<"normal" | "compact" | null>(null);
  const [failed, setFailed] = useState(false);
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    const element = target.current;
    if (!element) return;
    const measure = () =>
      setSize(element.clientWidth < 300 ? "compact" : "normal");
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => observer.disconnect();
  }, []);
  useEffect(() => {
    if (size === null) return;
    onToken("");
    let id: string | undefined;
    let disposed = false;
    const start = () => {
      if (!disposed && target.current && window.turnstile && id === undefined) {
        try {
          id = window.turnstile.render(target.current, {
            sitekey: siteKey,
            action: "login",
            theme: "dark",
            language: "ru",
            size,
            callback: (value: string) => {
              if (disposed) return;
              setFailed(false);
              onToken(value);
            },
            "expired-callback": () => {
              if (disposed) return;
              onToken("");
              if (id) window.turnstile?.reset(id);
            },
            "error-callback": () => {
              if (disposed) return true;
              onToken("");
              setFailed(true);
              return true;
            },
          });
          widget.current = id;
        } catch {
          failure();
        }
      }
    };
    const failure = () => {
      if (!disposed) {
        setFailed(true);
        onToken("");
      }
    };
    const existing = document.querySelector<HTMLScriptElement>(
      "script[data-turnstile]",
    );
    if (window.turnstile) start();
    else if (existing && existing.dataset.failed !== "true") {
      existing.addEventListener("load", start);
      existing.addEventListener("error", failure);
    } else {
      existing?.remove();
      const script = document.createElement("script");
      script.src =
        "https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit";
      script.async = true;
      script.dataset.turnstile = "true";
      script.addEventListener("load", start);
      script.addEventListener("error", () => {
        script.dataset.failed = "true";
        failure();
      });
      document.head.append(script);
    }
    return () => {
      disposed = true;
      if (id) window.turnstile?.remove(id);
      widget.current = null;
      existing?.removeEventListener("load", start);
      existing?.removeEventListener("error", failure);
    };
  }, [siteKey, onToken, retry, size]);
  useEffect(() => {
    if (resetKey > 0 && widget.current) {
      onToken("");
      window.turnstile?.reset(widget.current);
    }
  }, [resetKey, onToken]);
  return (
    <div className="turnstile-field">
      <div ref={target} className="turnstile-widget" />
      {failed && (
        <div className="notice warning" role="status">
          <p>Не удалось загрузить проверку безопасности. Повторите попытку.</p>
          <Button
            tone="secondary"
            type="button"
            onClick={() => {
              setFailed(false);
              setRetry((value) => value + 1);
            }}
          >
            Повторить проверку
          </Button>
        </div>
      )}
    </div>
  );
}
export type LoginConfig = PublicConfig & {
  turnstile_test_bypass?: boolean;
  telegram_miniapp_enabled?: boolean;
  telegram_login_enabled?: boolean;
};
export function Login({
  config,
  onLogin,
  initialEmail = "",
  emailOnly = false,
  onCancel,
}: {
  config: LoginConfig | null;
  onLogin: (session: Session) => void;
  initialEmail?: string;
  emailOnly?: boolean;
  onCancel?: () => void;
}) {
  const instance = useInstance();
  const terms = instanceTerminology(instance);
  browserReferral();
  const [email, setEmail] = useState(initialEmail);
  const [code, setCode] = useState("");
  const [codeStatus, setCodeStatus] = useState<CodeSlotsStatus>("idle");
  const submitting = useRef(false);
  const [challenge, setChallenge] = useState<string | null>(null);
  const [token, setToken] = useState("");
  const [turnstileReset, setTurnstileReset] = useState(0);
  const [deadline, setDeadline] = useState(0);
  const [now, setNow] = useState(Date.now());
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const legal = useApi<Legal>("/legal");
  const [legalAccepted, setLegalAccepted] = useState(false);
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, []);
  const left = Math.max(0, Math.ceil((deadline - now) / 1000));
  const bypass =
    config?.local_test === true && config.turnstile_test_bypass === true;
  async function requestCode() {
    if (
      submitting.current ||
      !legalAccepted ||
      !legal.data?.configured ||
      (!bypass && !token)
    )
      return;
    submitting.current = true;
    setBusy(true);
    setError(null);
    try {
      const result = await api<{ challenge_id: string; resend_after: number }>(
        "/auth/email/request",
        {
          method: "POST",
          body: {
            email,
            turnstile_token: bypass ? "local-test" : token,
          },
        },
      );
      setChallenge(result.challenge_id);
      setCode("");
      setCodeStatus("idle");
      const sentAt = Date.now();
      setNow(sentAt);
      setDeadline(sentAt + result.resend_after * 1000);
    } catch (error) {
      setError(error);
    } finally {
      if (!bypass) {
        setToken("");
        setTurnstileReset((value) => value + 1);
      }
      submitting.current = false;
      setBusy(false);
    }
  }
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!challenge) return requestCode();
    if (
      submitting.current ||
      code.length !== 6 ||
      codeStatus !== "idle" ||
      !legalAccepted ||
      !legal.data?.configured
    )
      return;
    submitting.current = true;
    setBusy(true);
    setError(null);
    try {
      const session = await api<Session>("/auth/email/verify", {
        method: "POST",
        body: {
          challenge_id: challenge,
          code,
          ...referralPayload(),
          legal_accepted: true,
          documents: legal.data.documents,
        },
      });
      setCsrf(session.csrf_token);
      setCodeStatus("success");
      onLogin(session);
    } catch (error) {
      setError(error);
      if (error instanceof ApiError && error.code.startsWith("legal_")) {
        setLegalAccepted(false);
        legal.reload();
        setCodeStatus("idle");
      } else setCodeStatus("error");
    } finally {
      submitting.current = false;
      setBusy(false);
    }
  }
  return (
    <div className="login-layout">
      <div className="login-art">
        <Brand href={safeUrl(config?.dashboard_url) || "/"} />
        <div>
          <p className="eyebrow">{messages.m35aafcbcb7}</p>
          <h1>
            {messages.m8e903e599a}
            <br />
            {messages.mbac6321d73}
            <br />
            <span>{messages.m59a4d44cab}</span>
          </h1>
          {instance.visual_profile === "private" && (
            <div className="login-sun" aria-hidden="true">
              <Sun />
            </div>
          )}
        </div>
        <p>
          {instance.app_name} · {terms.service_summary}
        </p>
      </div>
      <main className="login-main">
        <div className="mobile-brand">
          <Brand />
        </div>
        <div className="login-form">
          <span className="square-icon">
            <Mail size={24} />
          </span>
          <p className="eyebrow">{messages.mf1c1c2644d}</p>
          <h2>{challenge ? messages.mce3e6aa2df : messages.ma4d4f34d1b}</h2>
          <p>
            {challenge
              ? `Введите код из письма для ${email}.`
              : `Войдите по email, чтобы ${terms.connect_label.charAt(0).toLowerCase() + terms.connect_label.slice(1)} и управлять подпиской.`}
          </p>
          {challenge && (
            <p className="fine-print">
              Письмо не пришло? Проверьте папку «Спам» и правильность адреса.
              Повторную отправку можно запросить через 120 секунд.
            </p>
          )}
          {!challenge && !emailOnly && (
            <TelegramWebsiteLogin
              enabled={config?.telegram_login_enabled === true}
              accepted={legalAccepted && !legal.loading}
              legal={legal.data}
              onStale={() => {
                setLegalAccepted(false);
                legal.reload();
              }}
            />
          )}
          <form onSubmit={submit} className="form">
            {!challenge ? (
              <label>
                Email
                <input
                  type="email"
                  autoComplete="email"
                  required
                  readOnly={emailOnly}
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  placeholder="you@example.com"
                />
              </label>
            ) : (
              <label className="login-code-field">
                <span className="login-code-label">{messages.md137bee310}</span>
                <CodeSlots
                  {...authCodeSlotsProps}
                  key={challenge}
                  value={code}
                  status={codeStatus}
                  disabled={busy}
                  ariaLabel={messages.md137bee310}
                  ariaDescribedBy={error ? "login-code-error" : undefined}
                  onChange={(next) => {
                    setCode(next);
                    if (next) {
                      setCodeStatus("idle");
                      setError(null);
                    }
                  }}
                  autoFocus
                />
              </label>
            )}
            {config?.turnstile_site_key && !bypass && (
              <Turnstile
                siteKey={config.turnstile_site_key}
                onToken={setToken}
                resetKey={turnstileReset}
              />
            )}
            {config && !config.turnstile_site_key && !bypass && (
              <p className="notice warning" role="status">
                Проверка безопасности временно недоступна. Попробуйте позже.
              </p>
            )}
            {Boolean(error) && (
              <div id="login-code-error">
                <ErrorState error={error} />
              </div>
            )}
            <LegalCheckbox
              legal={legal.data}
              checked={legalAccepted}
              onChange={setLegalAccepted}
              context="login"
            />
            <Button
              type="submit"
              busy={busy}
              disabled={
                !legalAccepted ||
                !legal.data?.configured ||
                (!challenge && !bypass && !token)
              }
            >
              {challenge ? messages.m94f437afd7 : messages.m94b683c81c}
              <ArrowRight size={17} />
            </Button>
            {challenge && (
              <div className="login-resend">
                <Button
                  tone="ghost"
                  type="button"
                  disabled={left > 0 || busy || (!bypass && !token)}
                  onClick={requestCode}
                >
                  {left > 0
                    ? `Повторить через ${left} сек.`
                    : messages.mf6ef2bb5d5}
                </Button>
                <button
                  className="text-link"
                  type="button"
                  disabled={busy}
                  onClick={() => {
                    setChallenge(null);
                    setCode("");
                    setCodeStatus("idle");
                    setError(null);
                  }}
                >
                  <ArrowLeft size={15} />
                  {messages.m84741592ab}
                </button>
              </div>
            )}
            {onCancel && (
              <Button
                tone="secondary"
                type="button"
                disabled={busy}
                onClick={onCancel}
              >
                Отмена привязки
              </Button>
            )}
          </form>
          <div className="login-security">
            <ShieldCheck size={18} />
            <p>{messages.ma1e5084cbb}</p>
          </div>
          {bypass && (
            <div className="notice">
              {config.smtp_email_enabled &&
              !email.trim().toLowerCase().endsWith(".test")
                ? "Локальный тестовый стенд. Коды отправляются на разрешённые тестовые адреса электронной почты."
                : messages.m3fac10115d}
            </div>
          )}
        </div>
        <div className="login-bottom">
          <LegalLinks legal={legal.data} />
        </div>
      </main>
    </div>
  );
}
