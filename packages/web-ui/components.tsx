import { messages } from "./messages";
import {
  useEffect,
  useState,
  useRef,
  type ReactNode,
  type ButtonHTMLAttributes,
} from "react";
import {
  ArrowUpRight,
  LoaderCircle,
  X,
  AlertCircle,
  Inbox,
} from "lucide-react";
import { ru, statusLabel } from "./ru";
import { useInstance } from "./instance";

export function Brand({ href = "/" }: { href?: string }) {
  const instance = useInstance();
  const [failed, setFailed] = useState<string | null>(null);
  return (
    <a
      className="brand"
      href={href}
      aria-label={`${instance.app_name} — главная`}
    >
      {instance.logo_url && failed !== instance.logo_url && (
        <img
          src={instance.logo_url}
          alt=""
          onError={() => setFailed(instance.logo_url)}
        />
      )}
      <span>{instance.app_name}</span>
    </a>
  );
}
export function Button({
  children,
  tone = "primary",
  busy = false,
  className = "",
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & {
  tone?: "primary" | "secondary" | "ghost" | "danger";
  busy?: boolean;
}) {
  return (
    <button
      {...props}
      disabled={props.disabled || busy}
      className={`button ${tone} ${className}`}
      aria-busy={busy}
    >
      {busy && <LoaderCircle className="spin" size={17} aria-hidden="true" />}
      {children}
    </button>
  );
}
export function ArrowLink({
  href,
  children,
  className = "",
}: {
  href: string;
  children: ReactNode;
  className?: string;
}) {
  return (
    <a href={href} className={`button ${className}`}>
      {children}
      <ArrowUpRight size={18} aria-hidden="true" />
    </a>
  );
}
export function Loading() {
  return (
    <div className="loading" role="status">
      <LoaderCircle className="spin" aria-hidden="true" />
      <span>{ru.loading}</span>
    </div>
  );
}
export function ErrorState({
  error,
  onRetry,
}: {
  error: unknown;
  onRetry?: () => void;
}) {
  const message = error instanceof Error ? error.message : ru.network;
  return (
    <div className="error-state" role="alert">
      <AlertCircle size={22} />
      <div>
        <strong>{messages.md7cebf02f0}</strong>
        <p>{message}</p>
        {onRetry && (
          <Button tone="secondary" onClick={onRetry}>
            {ru.retry}
          </Button>
        )}
      </div>
    </div>
  );
}
export function Empty({
  title = ru.empty,
  children,
}: {
  title?: string;
  children?: ReactNode;
}) {
  return (
    <div className="empty">
      <Inbox aria-hidden="true" />
      <h3>{title}</h3>
      {children && <div>{children}</div>}
    </div>
  );
}
export function Badge({ value }: { value: string }) {
  return (
    <span
      className={`badge ${["active", "paid", "fulfilled", "completed"].includes(value) ? "good" : ["failed", "expired"].includes(value) ? "bad" : ""}`}
    >
      {statusLabel(value)}
    </span>
  );
}
export function SectionTitle({
  eyebrow,
  title,
  children,
}: {
  eyebrow?: string;
  title: string;
  children?: ReactNode;
}) {
  return (
    <div className="section-title">
      <div>
        {eyebrow && <p className="eyebrow">{eyebrow}</p>}
        <h1>{title}</h1>
      </div>
      {children}
    </div>
  );
}
export function Dialog({
  title,
  children,
  onClose,
}: {
  title: string;
  children: ReactNode;
  onClose: () => void;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const current = ref.current;
    const before = document.activeElement as HTMLElement;
    current?.showModal();
    return () => {
      current?.close();
      before?.focus();
    };
  }, []);
  return (
    <dialog
      ref={ref}
      className="dialog"
      onCancel={(event) => {
        event.preventDefault();
        onClose();
      }}
      aria-labelledby="dialog-title"
    >
      <div className="dialog-head">
        <h2 id="dialog-title">{title}</h2>
        <button
          type="button"
          className="icon-button"
          onClick={onClose}
          aria-label={ru.close}
        >
          <X />
        </button>
      </div>
      {children}
    </dialog>
  );
}
