import {
  useEffect,
  useId,
  useRef,
  useState,
  type FormEvent,
  type ReactNode,
} from "react";
import { ApiError, api, useApi } from "@api/client";
import type { Session } from "@api/types";
import { Button, Dialog, Empty, ErrorState, Loading } from "@ui/components";
import { date } from "@ui/content";
import CodeSlots, {
  authCodeSlotsProps,
  type CodeSlotsStatus,
} from "@ui/CodeSlots";

type Operation = {
  operation_id: string;
  stage:
    | "current_email"
    | "new_email"
    | "awaiting_telegram"
    | "owner_review"
    | "completed";
  expires_at: string | null;
  challenge_id?: string;
  retry_after?: number;
  proof?: string;
};
type AccountStatus = { account: Session["account"]; operations: Operation[] };
type OwnSession = {
  id: string;
  current: boolean;
  auth_method: string;
  created_at: string;
  last_seen_at: string | null;
  expires_at: string;
  ip_address: string | null;
  browser_label: string | null;
};
type Props = {
  session: Session;
  refreshSession: () => void;
  onAuthenticationRequired: () => void;
  freshProof: (onConfirmed: () => Promise<void>) => ReactNode;
  onEmailMergeLogin?: (email: string) => void;
};

export function AccountFeatures({
  session,
  refreshSession,
  onAuthenticationRequired,
  freshProof,
  onEmailMergeLogin,
}: Props) {
  const [dialog, setDialog] = useState<"email" | "unlink" | null>(null);
  const identity = useApi<AccountStatus>("/account/identity/status");
  useEffect(() => {
    window.addEventListener("focus", identity.reload);
    return () => window.removeEventListener("focus", identity.reload);
  }, [identity.reload]);
  const pending = (identity.data?.operations ?? []).filter(
    (operation) =>
      operation.stage !== "completed" &&
      (!operation.expires_at || Date.parse(operation.expires_at) > Date.now()),
  );
  return (
    <>
      <section className="card">
        <h2>Способы входа</h2>
        <p className="fine-print">
          Подтверждённая почта и связанный Telegram относятся к одному аккаунту.
          Изменение привязки сохраняет подписки и историю.
        </p>
        <div className="account-feature-actions">
          <Button tone="secondary" onClick={() => setDialog("email")}>
            {session.account.email ? "Изменить почту" : "Добавить почту"}
          </Button>
          {session.account.telegram_id && (
            <Button
              tone="secondary"
              disabled={!session.account.email_verified}
              onClick={() => setDialog("unlink")}
            >
              Отвязать Telegram
            </Button>
          )}
        </div>
        {session.account.telegram_id && !session.account.email_verified && (
          <p className="fine-print">
            Для отвязки Telegram сначала добавьте и подтвердите почту, чтобы
            сохранить способ входа.
          </p>
        )}
        {Boolean(identity.error) && (
          <ErrorState error={identity.error} onRetry={identity.reload} />
        )}
        {pending.map((operation) => (
          <div
            className="notice account-pending-operation"
            key={operation.operation_id}
          >
            <p>
              {operation.stage === "owner_review"
                ? "Ожидает решения владельца"
                : operation.stage === "awaiting_telegram"
                  ? "Ожидает подтверждения в Telegram"
                  : "Ожидает подтверждения почты"}
            </p>
            <p className="fine-print">
              До {date(operation.expires_at, true)} · МСК. Привязка не меняется
              до завершения подтверждения.
            </p>
            <Button
              tone="secondary"
              onClick={() => {
                identity.reload();
                refreshSession();
              }}
            >
              Проверить результат
            </Button>
          </div>
        ))}
      </section>
      <AccountSessions
        refreshSession={refreshSession}
        onAuthenticationRequired={onAuthenticationRequired}
      />
      {dialog && (
        <Dialog
          title={
            dialog === "email" ? "Подтверждение почты" : "Отвязка Telegram"
          }
          onClose={() => {
            setDialog(null);
            identity.reload();
          }}
        >
          <IdentityForm
            key={dialog}
            kind={dialog}
            session={session}
            freshProof={freshProof}
            onEmailMergeLogin={onEmailMergeLogin}
            onComplete={() => {
              setDialog(null);
              refreshSession();
            }}
          />
        </Dialog>
      )}
    </>
  );
}

function IdentityForm({
  kind,
  session,
  freshProof,
  onEmailMergeLogin,
  onComplete,
}: {
  kind: "email" | "unlink";
  session: Session;
  freshProof: Props["freshProof"];
  onEmailMergeLogin?: Props["onEmailMergeLogin"];
  onComplete: () => void;
}) {
  const [email, setEmail] = useState("");
  const [operation, setOperation] = useState<Operation | null>(null);
  const [code, setCode] = useState("");
  const [codeStatus, setCodeStatus] = useState<CodeSlotsStatus>("idle");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [freshRequired, setFreshRequired] = useState(false);
  const [mergeEmail, setMergeEmail] = useState<string | null>(null);
  const [review, setReview] = useState(false);
  const [reason, setReason] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const [copied, setCopied] = useState(false);
  const [now, setNow] = useState(Date.now());
  const [deadline, setDeadline] = useState(0);
  const errorId = useId();
  const working = useRef(false);
  const alive = useRef(true);
  const controller = useRef<AbortController | null>(null);
  const pendingFreshRequest = useRef<{ path: string; body: unknown } | null>(
    null,
  );
  const left = Math.max(0, Math.ceil((deadline - now) / 1000));
  const expires = operation?.expires_at
    ? new Date(operation.expires_at).getTime()
    : 0;
  const expired = Boolean(
    operation && operation.stage !== "completed" && expires && now >= expires,
  );
  useEffect(() => {
    alive.current = true;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => {
      alive.current = false;
      controller.current?.abort();
      window.clearInterval(timer);
    };
  }, []);
  useEffect(() => {
    if (operation?.stage !== "awaiting_telegram" || expired) return;
    let pending = false;
    const abort = new AbortController();
    const check = async () => {
      if (pending || document.visibilityState !== "visible") return;
      pending = true;
      try {
        const status = await api<AccountStatus>("/account/identity/status", {
          signal: abort.signal,
        });
        if (
          !abort.signal.aborted &&
          alive.current &&
          status.account.telegram_id === null
        )
          onComplete();
      } catch (failure) {
        if (!abort.signal.aborted && alive.current) setError(failure);
      } finally {
        pending = false;
      }
    };
    const timer = window.setInterval(() => {
      void check();
    }, 5000);
    window.addEventListener("focus", check);
    return () => {
      abort.abort();
      window.clearInterval(timer);
      window.removeEventListener("focus", check);
    };
  }, [operation?.stage, expired, onComplete]);
  function apply(result: Operation) {
    if (result.stage === "completed") {
      onComplete();
      return;
    }
    const sentAt = Date.now();
    setOperation(result);
    setCode("");
    setCodeStatus("idle");
    setNow(sentAt);
    setDeadline(sentAt + (result.retry_after ?? 0) * 1000);
    setFreshRequired(false);
  }
  async function mutate(path: string, body: unknown) {
    if (working.current) return;
    working.current = true;
    setBusy(true);
    setError(null);
    setMergeEmail(null);
    const request = new AbortController();
    controller.current = request;
    try {
      const result = await api<Operation>(path, {
        method: "POST",
        body,
        signal: request.signal,
      });
      if (alive.current && !request.signal.aborted) apply(result);
    } catch (failure) {
      if (!alive.current || request.signal.aborted) return;
      setCode("");
      setCodeStatus("error");
      if (
        failure instanceof ApiError &&
        failure.code === "fresh_login_required" &&
        session.account.email_verified
      ) {
        pendingFreshRequest.current = { path, body };
        setFreshRequired(true);
      } else if (
        failure instanceof ApiError &&
        failure.code === "email_identity_conflict" &&
        path === "/account/email/request" &&
        kind === "email" &&
        !session.account.email &&
        session.account.telegram_id &&
        onEmailMergeLogin
      ) {
        setMergeEmail(email.trim());
      } else setError(failure);
    } finally {
      working.current = false;
      if (alive.current) setBusy(false);
    }
  }
  const start = async () => {
    await mutate(
      kind === "email" ? "/account/email/request" : "/account/telegram/unlink",
      kind === "email" ? { email: email.trim() } : {},
    );
  };
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!operation) {
      if (kind === "email" || confirmed) await start();
      return;
    }
    if (!operation.challenge_id || code.length !== 6 || expired) return;
    await mutate("/account/email/verify", {
      operation_id: operation.operation_id,
      challenge_id: operation.challenge_id,
      code,
    });
  }
  const codeLabel =
    operation?.stage === "current_email"
      ? "Код из текущей почты"
      : "Код из новой почты";
  return (
    <div className="stack account-identity-operation">
      {freshRequired ? (
        freshProof(async () => {
          const original = pendingFreshRequest.current;
          pendingFreshRequest.current = null;
          setFreshRequired(false);
          if (original) await mutate(original.path, original.body);
        })
      ) : operation?.stage === "owner_review" ? (
        <div className="notice" role="status">
          Обращение передано владельцу. Привязка остаётся действующей до
          отдельного решения. Проверить результат можно в этом кабинете.
        </div>
      ) : operation?.stage === "awaiting_telegram" ? (
        <>
          <p>
            Подтвердите отвязку в связанном Telegram-аккаунте. Это сохраняет
            оплаченный доступ, но отключает вход и сообщения через эту привязку.
          </p>
          {operation.proof && (
            <Button
              disabled={expired}
              onClick={async () => {
                try {
                  await navigator.clipboard.writeText(
                    `/unlink ${operation.proof}`,
                  );
                  if (alive.current) setCopied(true);
                } catch (failure) {
                  if (alive.current) setError(failure);
                }
              }}
            >
              {copied ? "Команда скопирована" : "Скопировать команду отвязки"}
            </Button>
          )}
          <p className="fine-print">
            Команду нельзя передавать другим людям. Вернитесь в кабинет после
            подтверждения в боте.
          </p>
          <Button tone="secondary" onClick={() => setReview(true)}>
            Не могу подтвердить в Telegram
          </Button>
        </>
      ) : (
        <form className="form" onSubmit={submit}>
          {kind === "email" && (
            <label>
              Новая почта
              <input
                type="email"
                required
                autoComplete="email"
                disabled={busy || Boolean(operation)}
                value={email}
                onChange={(event) => {
                  setEmail(event.target.value);
                  setMergeEmail(null);
                }}
              />
            </label>
          )}
          {kind === "unlink" && !operation && (
            <>
              <p>
                Отвязка сохраняет подписки и историю. Продолжить можно при
                наличии подтверждённой почты, с подтверждением в Telegram.
              </p>
              <label className="checkbox-label">
                <input
                  type="checkbox"
                  checked={confirmed}
                  required
                  onChange={(event) => setConfirmed(event.target.checked)}
                />
                Понимаю последствия отвязки Telegram
              </label>
            </>
          )}
          {operation?.challenge_id && (
            <>
              <p>
                Код отправлен{" "}
                {operation.stage === "current_email"
                  ? `на текущую почту ${session.account.email}`
                  : `на новую почту ${email}`}
                .
              </p>
              <p className="fine-print">
                Письмо не пришло? Проверьте папку «Спам». Время окончания:{" "}
                {date(operation.expires_at, true)} · МСК.
              </p>
              <label>
                {codeLabel}
                <CodeSlots
                  {...authCodeSlotsProps}
                  key={operation.challenge_id}
                  value={code}
                  status={codeStatus}
                  disabled={busy || expired}
                  ariaLabel={codeLabel}
                  ariaDescribedBy={error ? errorId : undefined}
                  autoFocus
                  onChange={(next) => {
                    setCode(next);
                    setCodeStatus("idle");
                    setError(null);
                  }}
                />
              </label>
              <Button
                tone="secondary"
                type="button"
                disabled={busy || left > 0}
                onClick={() => {
                  void start();
                }}
              >
                {left > 0
                  ? `Повторить через ${left} сек.`
                  : "Отправить код ещё раз"}
              </Button>
            </>
          )}
          <Button
            type="submit"
            busy={busy}
            disabled={
              operation
                ? code.length !== 6 || expired
                : kind === "unlink" && !confirmed
            }
          >
            {operation
              ? "Подтвердить код"
              : kind === "email"
                ? "Отправить код"
                : "Начать отвязку"}
          </Button>
        </form>
      )}
      {kind === "unlink" &&
        !freshRequired &&
        (review || !operation) &&
        operation?.stage !== "owner_review" && (
          <details open={review}>
            <summary>Нет доступа к связанному Telegram</summary>
            <form
              className="form"
              onSubmit={(event) => {
                event.preventDefault();
                void mutate("/account/telegram/unlink-request", {
                  reason: reason.trim(),
                });
              }}
            >
              <p className="fine-print">
                Владелец рассмотрит обращение отдельно. Отправка запроса не
                удаляет привязку автоматически.
              </p>
              <label>
                Причина обращения
                <textarea
                  required
                  minLength={5}
                  maxLength={500}
                  value={reason}
                  onChange={(event) => setReason(event.target.value)}
                />
              </label>
              <Button type="submit" busy={busy}>
                Отправить обращение владельцу
              </Button>
            </form>
          </details>
        )}
      {expired && (
        <p className="notice warning" role="status">
          Срок подтверждения истёк. Закройте окно и начните действие заново.
        </p>
      )}
      {mergeEmail && onEmailMergeLogin && (
        <div className="notice stack" role="status">
          <p>
            Почта {mergeEmail} уже связана с другим аккаунтом. Чтобы объединить
            способы входа, подтвердите вход по этой почте, затем привязку из
            исходного Telegram-аккаунта в боте.
          </p>
          <p>
            Сам вход по почте не объединяет аккаунты. Подписки и история
            сохраняются при окончательном подтверждении привязки в Telegram.
          </p>
          <Button onClick={() => onEmailMergeLogin(mergeEmail)}>
            Войти по этой почте и связать аккаунты
          </Button>
        </div>
      )}
      {Boolean(error) && (
        <div id={errorId}>
          <ErrorState error={error} />
        </div>
      )}
    </div>
  );
}

function AccountSessions({
  refreshSession,
  onAuthenticationRequired,
}: Pick<Props, "refreshSession" | "onAuthenticationRequired">) {
  const sessions = useApi<{ items: OwnSession[] }>("/account/sessions");
  const [selected, setSelected] = useState<OwnSession | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const working = useRef(false);
  const controller = useRef<AbortController | null>(null);
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
      controller.current?.abort();
    };
  }, []);
  async function revoke() {
    if (!selected || working.current) return;
    working.current = true;
    setBusy(true);
    setError(null);
    const request = new AbortController();
    controller.current = request;
    try {
      const result = await api<{
        revoked: boolean;
        current_session_revoked: boolean;
      }>(`/account/sessions/${encodeURIComponent(selected.id)}/revoke`, {
        method: "POST",
        body: {},
        signal: request.signal,
      });
      if (!alive.current || request.signal.aborted) return;
      if (!result.revoked)
        throw new Error(
          "Не удалось завершить сеанс. Обновите список и повторите.",
        );
      setSelected(null);
      if (result.current_session_revoked) onAuthenticationRequired();
      else {
        sessions.reload();
        refreshSession();
      }
    } catch (failure) {
      if (alive.current && !request.signal.aborted) setError(failure);
    } finally {
      working.current = false;
      if (alive.current) setBusy(false);
    }
  }
  return (
    <section className="card stack account-sessions">
      <div className="card-head">
        <h2>Сеансы входа</h2>
        <Button tone="ghost" onClick={sessions.reload}>
          Обновить
        </Button>
      </div>
      <p className="fine-print">
        Завершите неизвестный сеанс. Подписки, устройства и история сохранятся.
      </p>
      {sessions.error ? (
        <ErrorState error={sessions.error} onRetry={sessions.reload} />
      ) : !sessions.data ? (
        <Loading />
      ) : !(sessions.data.items ?? []).length ? (
        <Empty title="Других сеансов нет" />
      ) : (
        sessions.data.items.map((item) => (
          <article className="account-session" key={item.id}>
            <div>
              <h3>{item.browser_label || "Браузер не указан"}</h3>
              <p>
                {item.current ? "Это устройство" : "Другое устройство"} ·{" "}
                {item.auth_method === "email" ||
                item.auth_method === "email_otp"
                  ? "Вход по почте"
                  : item.auth_method === "telegram_miniapp"
                    ? "Telegram Mini App"
                    : item.auth_method === "telegram_oidc" ||
                        item.auth_method === "telegram"
                      ? "Telegram Login"
                      : "Способ входа не указан"}
              </p>
              <p className="fine-print">
                Последняя активность:{" "}
                {date(item.last_seen_at || item.created_at, true)} · МСК
                {item.ip_address ? ` · IP ${item.ip_address}` : ""}
              </p>
            </div>
            <Button
              tone="secondary"
              onClick={() => {
                setError(null);
                setSelected(item);
              }}
            >
              Завершить сеанс
            </Button>
          </article>
        ))
      )}
      {selected && (
        <Dialog title="Завершить сеанс" onClose={() => setSelected(null)}>
          <div className="stack">
            <p>
              {selected.browser_label || "Выбранный браузер"}
              {selected.current
                ? ": этот вход завершится, потребуется войти снова."
                : ": доступ к кабинету с этого сеанса будет закрыт."}
            </p>
            {Boolean(error) && <ErrorState error={error} />}
            <Button
              tone="danger"
              busy={busy}
              onClick={() => {
                void revoke();
              }}
            >
              Завершить сеанс
            </Button>
          </div>
        </Dialog>
      )}
    </section>
  );
}
