import { useEffect, useId, useRef, useState, type FormEvent } from "react";
import QRCode from "qrcode";
import { useInstance } from "@ui/instance";
import { ShieldCheck } from "lucide-react";
import { ApiError, api, useApi } from "@api/client";
import { Brand, Button, ErrorState, Loading } from "@ui/components";
import CodeSlots, {
  authCodeSlotsProps,
  type CodeSlotsStatus,
} from "@ui/CodeSlots";

type MfaStatus = {
  enrolled: boolean;
  enrollment_required: boolean;
  trusted: boolean;
  trust_expires_at: string | null;
};
type Enrollment = {
  secret: string;
  provisioning_uri: string;
  expires_at: string;
};

export function AdminMfa({ onVerified }: { onVerified: () => void }) {
  const instance = useInstance();
  const status = useApi<MfaStatus>("/admin/mfa/status");
  const [enrollment, setEnrollment] = useState<Enrollment | null>(null);
  const [qr, setQr] = useState("");
  const [code, setCode] = useState("");
  const [codeStatus, setCodeStatus] = useState<CodeSlotsStatus>("idle");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const submitting = useRef(false);
  const errorId = useId();
  useEffect(() => {
    if (status.data?.trusted) onVerified();
  }, [status.data?.trusted, onVerified]);
  useEffect(() => {
    if (!enrollment) return;
    let cancelled = false;
    QRCode.toDataURL(enrollment.provisioning_uri, { width: 248, margin: 2 })
      .then((value) => {
        if (!cancelled) setQr(value);
      })
      .catch(() => {
        if (!cancelled) setQr("");
      });
    const timer = setTimeout(
      () => {
        setEnrollment(null);
        setQr("");
        setCode("");
        setCodeStatus("idle");
        setError(new Error("Время настройки истекло. Запросите новый QR."));
      },
      Math.max(0, Date.parse(enrollment.expires_at) - Date.now()),
    );
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [enrollment]);
  async function begin() {
    if (submitting.current) return;
    submitting.current = true;
    setBusy(true);
    setError(null);
    try {
      setEnrollment(
        await api<Enrollment>("/admin/mfa/enroll", {
          method: "POST",
          body: {},
        }),
      );
    } catch (e) {
      setError(e);
    } finally {
      submitting.current = false;
      setBusy(false);
    }
  }
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (submitting.current || code.length !== 6 || codeStatus !== "idle")
      return;
    submitting.current = true;
    setBusy(true);
    setError(null);
    try {
      await api(
        enrollment ? "/admin/mfa/enroll/confirm" : "/admin/mfa/verify",
        { method: "POST", body: { code } },
      );
      setEnrollment(null);
      setQr("");
      setCodeStatus("success");
      onVerified();
    } catch (e) {
      setError(e);
      setCodeStatus("error");
    } finally {
      submitting.current = false;
      setBusy(false);
    }
  }
  const first = status.data?.enrollment_required;
  return (
    <main className="mfa-page">
      <Brand />
      <div className="card">
        <span className="square-icon">
          <ShieldCheck />
        </span>
        <h1>{first ? "Настройте защиту админки" : "Дополнительная защита"}</h1>
        <p>
          {first
            ? `Почта подтверждена. Добавьте ${instance.app_name} в Google Authenticator, 2FAS или другое приложение и введите первый код.`
            : "Введите текущий код из приложения-аутентификатора. На email этот код не отправляется."}
        </p>
        <p className="fine-print">
          Повторная проверка потребуется при смене IP или через 48 часов после
          успешного ввода.
        </p>
        {status.loading ? (
          <Loading />
        ) : status.error ? (
          <ErrorState error={status.error} onRetry={status.reload} />
        ) : first && !enrollment ? (
          <Button busy={busy} onClick={begin}>
            Настроить аутентификатор
          </Button>
        ) : (
          <>
            {enrollment && (
              <div className="stack">
                {qr && (
                  <img
                    src={qr}
                    width={248}
                    height={248}
                    style={{
                      maxWidth: "100%",
                      height: "auto",
                      alignSelf: "center",
                    }}
                    alt="QR для приложения-аутентификатора"
                  />
                )}
                <p className="fine-print">
                  Отсканируйте QR или добавьте этот ключ вручную. Никому его не
                  передавайте.
                </p>
                <code style={{ overflowWrap: "anywhere", userSelect: "all" }}>
                  {enrollment.secret}
                </code>
              </div>
            )}
            <form className="form" onSubmit={submit}>
              <label>
                Код аутентификатора
                <CodeSlots
                  {...authCodeSlotsProps}
                  value={code}
                  status={codeStatus}
                  disabled={busy}
                  ariaLabel="Код аутентификатора"
                  ariaDescribedBy={error ? errorId : undefined}
                  mask
                  autoFocus
                  onChange={(next) => {
                    setCode(next);
                    if (next) {
                      setCodeStatus("idle");
                      setError(null);
                    }
                  }}
                />
              </label>
              <Button type="submit" busy={busy} disabled={code.length !== 6}>
                {enrollment ? "Включить защиту" : "Подтвердить вход"}
              </Button>
            </form>
          </>
        )}
        {Boolean(error) && (
          <div id={errorId} className="mfa-error">
            <ErrorState error={error} />
          </div>
        )}
        {error instanceof ApiError && error.code === "fresh_login_required" && (
          <Button
            tone="secondary"
            onClick={async () => {
              try {
                await api("/auth/logout", { method: "POST" });
                location.assign("/?login=email");
              } catch (error) {
                setError(error);
              }
            }}
          >
            Подтвердить почту повторно
          </Button>
        )}
        <a className="text-link" href="/">
          Вернуться в кабинет
        </a>
      </div>
    </main>
  );
}
