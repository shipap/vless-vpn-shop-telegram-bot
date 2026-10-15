import { useState } from "react";
import { api, useApi } from "@api/client";
import type { Legal, LegalStatus } from "@api/types";
import { safeUrl } from "./content";
import { Button, ErrorState } from "./components";

export function LegalLinks({ legal }: { legal: Legal | null }) {
  return (
    <span className="legal-links">
      {legal?.documents.map((document) => {
        const href = safeUrl(document.url);
        if (!href) return null;
        return (
          <a
            key={document.document_type}
            href={href}
            target="_blank"
            rel="noopener noreferrer"
          >
            {document.document_type === "offer"
              ? "Публичная оферта"
              : "Политика конфиденциальности"}
          </a>
        );
      })}
    </span>
  );
}

export function LegalCheckbox({
  legal,
  checked,
  onChange,
  context = "operation",
}: {
  legal: Legal | null;
  checked: boolean;
  onChange: (value: boolean) => void;
  context?: "login" | "operation";
}) {
  return (
    <div className="legal-consent">
      <LegalLinks legal={legal} />
      <label className="checkbox-label">
        <input
          type="checkbox"
          checked={checked}
          disabled={!legal?.configured}
          onChange={(event) => onChange(event.target.checked)}
        />
        {context === "login"
          ? "Принимаю оферту и политику конфиденциальности для входа в аккаунт"
          : "Принимаю оферту и политику конфиденциальности для новых операций"}
      </label>
      <p className="fine-print">
        Это согласие не включает автоматические списания.
      </p>
    </div>
  );
}

export function LegalNotice() {
  const status = useApi<LegalStatus>("/legal/status");
  const [checked, setChecked] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  if (status.data?.accepted) return null;
  if (!status.data)
    return status.error ? (
      <ErrorState error={status.error} onRetry={status.reload} />
    ) : null;
  async function accept() {
    if (!checked || !status.data?.configured) return;
    setBusy(true);
    setError(null);
    try {
      const result = await api<LegalStatus>("/legal/accept", {
        method: "POST",
        body: {
          documents: status.data.documents,
          context: "first_use",
        },
      });
      status.setData(result);
      setChecked(false);
    } catch (error) {
      setError(error);
    } finally {
      setBusy(false);
    }
  }
  return (
    <section className="notice legal-notice" aria-label="Условия сервиса">
      <strong>Подтвердите актуальные условия сервиса</strong>
      <p>
        Для новых покупок и изменений подписки нужно явное согласие. Просмотр
        оплаченного доступа и отмена автопродления остаются доступны.
      </p>
      <LegalCheckbox
        legal={status.data}
        checked={checked}
        onChange={setChecked}
      />
      {!status.data.configured && (
        <p>Документы ещё не настроены. Обратитесь в поддержку.</p>
      )}
      {Boolean(error) && <ErrorState error={error} />}
      <Button
        disabled={!checked || !status.data.configured}
        busy={busy}
        onClick={accept}
      >
        Принять условия
      </Button>
    </section>
  );
}
