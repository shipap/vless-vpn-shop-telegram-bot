import { Infinity as InfinityIcon, Trash2 } from "lucide-react";
import { useState } from "react";
import { Button, Dialog, ErrorState, Loading } from "@ui/components";
import type { Catalog } from "@api/types";
import type { components } from "@api/schema";
import { messages } from "@ui/messages";
import { promoCopy as copy } from "./promos-copy";
import { moscowInput, moscowTimestamp } from "@ui/moscow-time";
import { AdminDateField } from "./admin-date-field";

export type PromoDraft = {
  code: string;
  enabled: boolean;
  reward_type?: "percent" | "fixed_rub" | "free_days";
  percent: string;
  amount_rub?: string | null;
  reward_days?: number | null;
  product_id?: string | null;
  audience?: "all" | "first_purchase" | "renewal";
  total_limit?: number | null;
  per_account_limit?: number;
  starts_at?: string | null;
  ends_at?: string | null;
  activated_users?: number;
  redeemed_count?: number;
  remaining_uses?: number | null;
  historical_usage_unknown?: boolean;
};

export function promoPayload(
  promo: PromoDraft,
): components["schemas"]["PromoInput"] {
  const amount = String(promo.amount_rub ?? "").replace(",", ".");
  const amountRub = /^\d+$/.test(amount)
    ? `${amount}.00`
    : /^\d+\.\d$/.test(amount)
      ? `${amount}0`
      : amount;
  return {
    enabled: promo.enabled,
    starts_at: promo.starts_at,
    ends_at: promo.ends_at,
    code: promo.code.trim().toUpperCase(),
    reward_type: promo.reward_type || "percent",
    percent:
      !promo.reward_type || promo.reward_type === "percent"
        ? promo.percent
        : "0",
    amount_rub: promo.reward_type === "fixed_rub" ? amountRub : null,
    reward_days: promo.reward_type === "free_days" ? promo.reward_days : null,
    product_id: promo.reward_type === "free_days" ? promo.product_id : null,
    audience: promo.audience || "all",
    per_account_limit: 1,
    total_limit: promo.total_limit ?? null,
  };
}

export function PromoEditor({
  promo,
  index,
  products,
  loading,
  error,
  onRetry,
  onChange,
  onRemove,
}: {
  promo: PromoDraft;
  index: number;
  products: Catalog["products"];
  loading: boolean;
  error: unknown;
  onRetry: () => void;
  onChange: (value: PromoDraft) => void;
  onRemove?: () => void;
}) {
  const [removing, setRemoving] = useState(false);
  const reward = promo.reward_type || "percent";
  const unlimited = promo.total_limit == null;
  const set = (value: Partial<PromoDraft>) => onChange({ ...promo, ...value });
  return (
    <fieldset className="promo-editor">
      <legend>Промокод {index + 1}</legend>
      <div className="field-grid">
        <label>
          {messages.m1b3a63b288}
          <input
            required
            maxLength={40}
            pattern="[A-Za-z0-9_-]+"
            value={promo.code}
            onChange={(event) => set({ code: event.target.value })}
          />
        </label>
        <label>
          {copy.reward}
          <select
            value={reward}
            onChange={(event) =>
              set({
                reward_type: event.target.value as PromoDraft["reward_type"],
              })
            }
          >
            <option value="percent">{copy.percent}</option>
            <option value="fixed_rub">{copy.fixed}</option>
            <option value="free_days">{copy.free}</option>
          </select>
        </label>
        {reward === "percent" && (
          <label>
            {messages.m7153e4db89}
            <input
              required
              type="number"
              min="0"
              max="100"
              step="0.01"
              value={promo.percent}
              onChange={(event) => set({ percent: event.target.value })}
            />
          </label>
        )}
        {reward === "fixed_rub" && (
          <label>
            {copy.amount}
            <input
              required
              inputMode="decimal"
              pattern="\d+(\.\d{1,2})?"
              value={promo.amount_rub ?? ""}
              onChange={(event) =>
                set({ amount_rub: event.target.value.replace(",", ".") })
              }
            />
          </label>
        )}
        {reward === "free_days" && (
          <>
            <label>
              {copy.days}
              <input
                required
                type="number"
                min="1"
                max="3650"
                step="1"
                value={promo.reward_days ?? ""}
                onChange={(event) =>
                  set({
                    reward_days: event.target.value
                      ? Number(event.target.value)
                      : null,
                  })
                }
              />
            </label>
            <label>
              {copy.tariff}
              <select
                required
                disabled={loading || Boolean(error)}
                value={promo.product_id || ""}
                onChange={(event) => set({ product_id: event.target.value })}
              >
                <option value="">{copy.tariffPlaceholder}</option>
                {products
                  .filter(
                    (product) =>
                      product.available || product.id === promo.product_id,
                  )
                  .map((product) => (
                    <option
                      value={product.id}
                      key={product.id}
                      disabled={!product.available}
                    >
                      {product.name}
                      {!product.available ? " · недоступен" : ""}
                    </option>
                  ))}
              </select>
            </label>
          </>
        )}
        <label>
          {copy.audience}
          <select
            value={promo.audience || "all"}
            onChange={(event) =>
              set({ audience: event.target.value as PromoDraft["audience"] })
            }
          >
            <option value="all">{copy.all}</option>
            <option value="first_purchase">{copy.first}</option>
            <option value="renewal">{copy.renewal}</option>
          </select>
        </label>
        <div className="promo-limit-field">
          <label htmlFor={`promo-limit-${index}`}>{copy.total}</label>
          <div className="promo-limit-control">
            <input
              id={`promo-limit-${index}`}
              type={unlimited ? "text" : "number"}
              min="1"
              step="1"
              required={!unlimited}
              disabled={unlimited}
              value={
                unlimited ? copy.unlimitedValue : (promo.total_limit ?? "")
              }
              onChange={(event) =>
                set({
                  total_limit: event.target.value
                    ? Number(event.target.value)
                    : 0,
                })
              }
            />
            <button
              type="button"
              className="icon-button"
              aria-label={copy.unlimited}
              aria-pressed={unlimited}
              title={unlimited ? copy.finite : copy.unlimited}
              onClick={() => set({ total_limit: unlimited ? 1 : null })}
            >
              <InfinityIcon size={20} aria-hidden="true" />
            </button>
          </div>
        </div>
        {(["starts_at", "ends_at"] as const).map((key) => (
          <div className="admin-date-label" key={key}>
            {key === "starts_at" ? copy.starts : copy.ends} · МСК
            <AdminDateField
              mode="datetime-local"
              label={`${key === "starts_at" ? copy.starts : copy.ends} · МСК`}
              value={moscowInput(promo[key])}
              onChange={(value) =>
                set({
                  [key]: value ? moscowTimestamp(value) : null,
                })
              }
            />
          </div>
        ))}
      </div>
      {reward === "free_days" && (
        <>
          <p className="small-copy">{copy.tariffNote}</p>
          {loading && <Loading />}
          {Boolean(error) && <ErrorState error={error} onRetry={onRetry} />}
        </>
      )}
      <div className="promo-editor-footer">
        <label className="checkbox-label">
          <input
            type="checkbox"
            checked={promo.enabled}
            onChange={(event) => set({ enabled: event.target.checked })}
          />
          {messages.m0b81629905}
        </label>
        <span className="small-copy">{copy.once}</span>
        {onRemove && (
          <button
            type="button"
            className="icon-button"
            aria-label={`Удалить промокод ${promo.code || index + 1}`}
            onClick={() => setRemoving(true)}
          >
            <Trash2 size={16} aria-hidden="true" />
          </button>
        )}
      </div>
      {removing && onRemove && (
        <Dialog
          title={`Удалить промокод ${promo.code || index + 1}?`}
          onClose={() => setRemoving(false)}
        >
          <p>
            Промокод будет удалён из редактируемого списка после подтверждения.
            Изменения вступят в силу после сохранения настроек.
          </p>
          <Button
            type="button"
            tone="secondary"
            onClick={() => setRemoving(false)}
          >
            Отмена
          </Button>
          <Button
            type="button"
            tone="danger"
            onClick={() => {
              setRemoving(false);
              onRemove();
            }}
          >
            Подтвердить удаление
          </Button>
        </Dialog>
      )}
    </fieldset>
  );
}
