import { messages } from "@ui/messages";
import { InstanceProvider, useInstance } from "@ui/instance";
import { instanceTerminology } from "@ui/terminology";
import { createRoot } from "react-dom/client";
import { useCallback, useEffect, useRef, useState } from "react";
import {
  Home,
  Smartphone,
  CreditCard,
  Gift,
  UserRound,
  CircleHelp,
  LogOut,
  Menu,
  X,
  ArrowUpRight,
  ShieldCheck,
  Newspaper,
} from "lucide-react";
import { ApiError, api, useApi, setCsrf } from "@api/client";
import type { Session, Catalog, Dashboard } from "@api/types";
import { Brand, Button, ErrorState, Loading } from "@ui/components";
import { safeUrl } from "@ui/content";
import { LegalLinks, LegalNotice } from "@ui/legal";
import { ru } from "@ui/ru";
import { useTelegramWebApp } from "@ui/telegram";
import { Login } from "./auth";
import { TelegramMiniAppLogin } from "./telegram-auth";
import type { LoginConfig } from "./auth";
import { Customer } from "./customer";
import { Admin } from "./admin";
import {
  NotificationCenter,
  validNotificationAction,
  type NotificationAction,
} from "@ui/notification-center";
import { hasCapability, staffRole } from "@ui/staff-permissions";
import { useAttachCampaign } from "@ui/campaign-hooks";
import "@fontsource-variable/manrope";
import "@ui/styles.css";
import "./dashboard.css";

const icons = {
  home: Home,
  devices: Smartphone,
  payments: CreditCard,
  bonuses: Gift,
  account: UserRound,
  help: CircleHelp,
  news: Newspaper,
};
function useDashboard(accountId: string | undefined, page: string) {
  const [data, setData] = useState<Dashboard | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(false);
  const [version, setVersion] = useState(0);
  const previous = useRef<{
    accountId: string | undefined;
    page: string;
  } | null>(null);
  const reload = useCallback(() => setVersion((value) => value + 1), []);
  useEffect(() => {
    const entry =
      (page === "home" || page === "account") &&
      (previous.current?.accountId !== accountId ||
        previous.current?.page !== page);
    previous.current = { accountId, page };
    if (!accountId) {
      setData(null);
      return;
    }
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    api<Dashboard>(entry ? "/dashboard?refresh_panel=true" : "/dashboard", {
      signal: controller.signal,
    })
      .then((value) => {
        if (!controller.signal.aborted) setData(value);
      })
      .catch((failure) => {
        if (!controller.signal.aborted) setError(failure);
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [accountId, page, version]);
  return { data, setData, error, loading, reload };
}
function App() {
  const instance = useInstance();
  const nav = {
    ...ru.nav,
    home: instanceTerminology(instance).connections_label,
    news: "Новости серверов",
  };
  const telegram = useTelegramWebApp();
  const session = useApi<Session>("/auth/me");
  const config = useApi<LoginConfig>("/public-config");
  const catalog = useApi<Catalog>("/catalog");
  const [page, setPage] = useState(() => {
    const initial = location.hash.slice(1);
    return initial in nav ? initial : "home";
  });
  const [notificationAction, setNotificationAction] =
    useState<NotificationAction | null>(null);
  const [emailMergeIntent, setEmailMergeIntent] = useState<{
    email: string;
    telegramId: string;
    sourceAccountId: string;
  } | null>(null);
  const emailMergeIntentRef = useRef(emailMergeIntent);
  const [telegramLinkHint, setTelegramLinkHint] = useState<{
    email: string;
    telegramId: string;
    accountId: string;
    expiresAt: number;
  } | null>(null);
  const [emailMergeError, setEmailMergeError] = useState<unknown>(null);
  const clearEmailMergeIntent = () => {
    emailMergeIntentRef.current = null;
    setEmailMergeIntent(null);
  };
  useEffect(() => {
    if (
      emailMergeIntent &&
      (!session.data ||
        session.data.account.id !== emailMergeIntent.sourceAccountId ||
        session.data.account.telegram_id !== emailMergeIntent.telegramId)
    )
      clearEmailMergeIntent();
  }, [emailMergeIntent, session.data]);
  useEffect(() => {
    if (!telegramLinkHint) return;
    const account = session.data?.account;
    const remaining = telegramLinkHint.expiresAt - Date.now();
    if (
      !account ||
      remaining <= 0 ||
      account.id !== telegramLinkHint.accountId ||
      account.email?.trim().toLowerCase() !== telegramLinkHint.email ||
      !account.email_verified ||
      account.telegram_id
    ) {
      setTelegramLinkHint(null);
      return;
    }
    const timer = window.setTimeout(() => setTelegramLinkHint(null), remaining);
    return () => window.clearTimeout(timer);
  }, [telegramLinkHint, session.data]);
  const accountId = session.data?.account.id;
  const dashboard = useDashboard(accountId, page);
  useEffect(() => {
    dashboard.setData(null);
    setNotificationAction(null);
  }, [accountId, dashboard.setData]);
  const refreshAccount = useCallback(() => {
    session.reload();
    dashboard.reload();
  }, [session.reload, dashboard.reload]);
  const authenticated = Boolean(session.data);
  useEffect(() => {
    if (!authenticated) return;
    let lastFocusRefresh = 0;
    const refreshVisible = () => {
      if (document.visibilityState !== "visible") return;
      const now = Date.now();
      if (now - lastFocusRefresh < 500) return;
      lastFocusRefresh = now;
      refreshAccount();
    };
    window.addEventListener("focus", refreshVisible);
    document.addEventListener("visibilitychange", refreshVisible);
    return () => {
      window.removeEventListener("focus", refreshVisible);
      document.removeEventListener("visibilitychange", refreshVisible);
    };
  }, [authenticated, refreshAccount]);
  const [mobile, setMobile] = useState(false);
  const [logoutError, setLogoutError] = useState<unknown>(null);
  const [telegramEmail, setTelegramEmail] = useState(() => {
    const url = new URL(location.href);
    if (url.searchParams.get("login") !== "email") return false;
    url.searchParams.delete("login");
    history.replaceState(null, "", url.pathname + url.search + url.hash);
    return true;
  });
  const [telegramAutoResume, setTelegramAutoResume] = useState(true);
  useEffect(() => {
    if (telegram.activation > 0) refreshAccount();
  }, [telegram.activation, refreshAccount]);
  useEffect(() => {
    const handler = () => {
      const next = location.hash.slice(1);
      if (next === "main-content") return;
      setPage(next in nav ? next : "home");
      setMobile(false);
      requestAnimationFrame(() =>
        document.getElementById("main-content")?.focus(),
      );
    };
    window.addEventListener("hashchange", handler);
    return () => window.removeEventListener("hashchange", handler);
  }, []);
  useEffect(() => {
    if (session.data) setCsrf(session.data.csrf_token);
  }, [session.data]);
  useAttachCampaign(session.data?.account.id);
  useEffect(() => {
    if (
      session.error instanceof ApiError &&
      session.error.status === 401 &&
      session.data
    ) {
      setCsrf(null);
      session.setData(null);
    }
  }, [session.error, session.data, session.setData]);
  if (session.loading && !session.data) return <Loading />;
  if (
    session.error &&
    (!(session.error instanceof ApiError) || session.error.status !== 401)
  )
    return (
      <main className="standalone">
        <Brand />
        <ErrorState error={session.error} onRetry={session.reload} />
      </main>
    );
  if (!session.data && config.loading) return <Loading />;
  if (emailMergeIntent)
    return (
      <Login
        key={emailMergeIntent.email}
        config={config.data}
        initialEmail={emailMergeIntent.email}
        emailOnly
        onCancel={() => {
          clearEmailMergeIntent();
          setTelegramLinkHint(null);
        }}
        onLogin={(value) => {
          const intent = emailMergeIntentRef.current;
          clearEmailMergeIntent();
          dashboard.setData(null);
          session.setData(value);
          session.reload();
          setPage("account");
          location.hash = "#account";
          if (
            intent &&
            intent === emailMergeIntent &&
            value.account.email_verified &&
            value.account.email?.trim().toLowerCase() === intent.email &&
            !value.account.telegram_id
          ) {
            setTelegramLinkHint({
              email: intent.email,
              telegramId: intent.telegramId,
              accountId: value.account.id,
              expiresAt: Date.now() + 300000,
            });
          } else {
            setTelegramLinkHint(null);
            setEmailMergeError(
              new Error(
                "Привязка не начата: вход выполнен в другой аккаунт или Telegram уже связан. Проверьте способы входа в этом кабинете.",
              ),
            );
          }
        }}
      />
    );
  if (
    !session.data &&
    !telegramEmail &&
    telegram.initData &&
    config.data?.telegram_miniapp_enabled
  )
    return (
      <TelegramMiniAppLogin
        initData={telegram.initData}
        autoResume={telegramAutoResume}
        onEmail={() => setTelegramEmail(true)}
        onLogin={(value) => {
          session.setData(value);
          session.reload();
        }}
      />
    );
  if (!session.data)
    return (
      <Login
        config={config.data}
        onLogin={(value) => {
          session.setData(value);
          session.reload();
        }}
      />
    );
  const current = session.data;
  if (location.pathname.startsWith("/admin"))
    return (
      <Admin
        key={current.account.id}
        session={current}
        refreshSession={refreshAccount}
        webPushPublicKey={
          (
            config.data as
              (LoginConfig & { web_push_public_key?: string | null }) | null
          )?.web_push_public_key
        }
      />
    );
  async function logout() {
    try {
      await api("/auth/logout", { method: "POST" });
      clearEmailMergeIntent();
      setTelegramLinkHint(null);
      setEmailMergeError(null);
      setTelegramAutoResume(false);
      setCsrf(null);
      session.setData(null);
      dashboard.setData(null);
      session.reload();
    } catch (error) {
      setLogoutError(error);
    }
  }
  return (
    <div className="dashboard-layout">
      <a
        href="#main-content"
        className="skip-link"
        onClick={(event) => {
          event.preventDefault();
          document.getElementById("main-content")?.focus();
        }}
      >
        {messages.m0ebff3642a}
      </a>
      <header className="mobile-header">
        <Brand />
        <button
          id="mobile-menu-toggle"
          className="icon-button"
          onClick={() => setMobile(!mobile)}
          onKeyDown={(event) => {
            if (event.key === "Escape") setMobile(false);
          }}
          aria-label={mobile ? messages.m980d22e392 : messages.mb2bbe98857}
          aria-expanded={mobile}
        >
          {mobile ? <X /> : <Menu />}
        </button>
      </header>
      <aside
        className={`sidebar ${mobile ? "open" : ""}`}
        onKeyDown={(event) => {
          if (event.key === "Escape") {
            setMobile(false);
            document.getElementById("mobile-menu-toggle")?.focus();
          }
        }}
      >
        <Brand />
        <div className="sidebar-caption">{messages.mf1c1c2644d}</div>
        <nav aria-label={messages.ma9715af198}>
          {Object.entries(nav).map(([key, label]) => {
            const Icon = icons[key as keyof typeof icons];
            return (
              <a
                key={key}
                href={`#${key}`}
                className={page === key ? "active" : ""}
                aria-current={page === key ? "page" : undefined}
              >
                <Icon size={19} />
                {label}
                {page === key && <span className="nav-dot" />}
              </a>
            );
          })}
        </nav>
        <div className="sidebar-bottom">
          {hasCapability(current, "statistics_read") && (
            <a href="/admin" className="admin-link">
              <ShieldCheck size={17} />
              {messages.m8f5b8d860e}
              <ArrowUpRight size={14} />
            </a>
          )}
          {safeUrl(config.data?.support_url) && (
            <a
              href={safeUrl(config.data?.support_url)}
              className="support-link"
            >
              <CircleHelp size={18} />
              <div>
                <strong>{messages.m3811b676de}</strong>
                <span>{messages.m1f8466168b}</span>
              </div>
              <ArrowUpRight size={15} />
            </a>
          )}
          <div className="account-mini">
            <span className="avatar">
              {current.account.email?.slice(0, 1).toUpperCase() || "S"}
            </span>
            <div>
              <strong>{current.account.email || messages.m3230cfc033}</strong>
              {staffRole(current) && (
                <span>
                  {staffRole(current) === "owner"
                    ? "Владелец"
                    : "Администратор"}
                </span>
              )}
              <span>
                {messages.md05b6f3e91}
                {current.account.id}
              </span>
            </div>
            <button
              onClick={logout}
              className="icon-button"
              aria-label={messages.m5690c2e63b}
            >
              <LogOut size={17} />
            </button>
          </div>
        </div>
      </aside>
      <div className="workspace">
        <div
          className="workspace-top"
          role="region"
          aria-label={messages.currentSection}
        >
          <span>
            {instance.app_name} <span className="slash">/</span>{" "}
            {nav[page as keyof typeof nav] || nav.home}
          </span>
          <div className="workspace-actions">
            <span className="workspace-date">
              {new Intl.DateTimeFormat("ru-RU", {
                day: "numeric",
                month: "long",
              }).format(new Date())}
            </span>
            <NotificationCenter
              key={current.account.id}
              accountId={current.account.id}
              onAction={(action) => {
                if (!validNotificationAction(action)) return;
                setNotificationAction(action);
                const next =
                  action.operation === "devices" ? "devices" : "home";
                setPage(next);
                location.hash = `#${next}`;
              }}
              publicKey={
                (
                  config.data as
                    | (LoginConfig & { web_push_public_key?: string | null })
                    | null
                )?.web_push_public_key
              }
            />
          </div>
        </div>
        <main id="main-content" className="workspace-main" tabIndex={-1}>
          <LegalNotice />
          {Boolean(logoutError) && <ErrorState error={logoutError} />}
          {Boolean(emailMergeError) && <ErrorState error={emailMergeError} />}
          <Customer
            key={current.account.id}
            page={page}
            session={current}
            dashboard={dashboard}
            catalog={catalog}
            config={config.data}
            notificationAction={notificationAction}
            refreshSession={refreshAccount}
            onEmailMergeLogin={(email) => {
              if (current.account.email || !current.account.telegram_id) return;
              const intent = {
                email: email.trim().toLowerCase(),
                telegramId: current.account.telegram_id,
                sourceAccountId: current.account.id,
              };
              emailMergeIntentRef.current = intent;
              setEmailMergeIntent(intent);
              setTelegramLinkHint(null);
              setEmailMergeError(null);
              setTelegramAutoResume(false);
              setTelegramEmail(true);
            }}
            telegramLinkHint={
              telegramLinkHint &&
              telegramLinkHint.accountId === current.account.id &&
              telegramLinkHint.email ===
                current.account.email?.trim().toLowerCase() &&
              current.account.email_verified &&
              !current.account.telegram_id &&
              Date.now() < telegramLinkHint.expiresAt
                ? telegramLinkHint
                : null
            }
            onTelegramLinkHintConsumed={() => setTelegramLinkHint(null)}
            onAuthenticationRequired={() => {
              clearEmailMergeIntent();
              setTelegramLinkHint(null);
              setTelegramAutoResume(false);
              setCsrf(null);
              session.setData(null);
              session.reload();
            }}
          />
        </main>
        <footer className="workspace-footer">
          <span>{instance.app_name}</span>
          <span>{messages.m3fd04974c0}</span>
          <LegalLinks legal={catalog.data?.legal ?? null} />
        </footer>
      </div>
    </div>
  );
}
createRoot(document.getElementById("root")!).render(
  <InstanceProvider surface="dashboard">
    <App />
  </InstanceProvider>,
);
