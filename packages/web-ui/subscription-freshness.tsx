import type { Subscription } from "@api/types";
import { date } from "./content";

type Observation = Pick<
  Subscription,
  "observed_at" | "device_inventory_state"
> &
  Partial<Pick<Subscription, "panel_sync_state">>;

function instant(value: string | null | undefined) {
  if (
    !value ||
    !/^\d{4}-\d{2}-\d{2}T(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d(?:\.\d+)?(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)$/.test(
      value,
    )
  )
    return NaN;
  const timestamp = Date.parse(value);
  const calendar = new Date(`${value.slice(0, 10)}T00:00:00Z`);
  return Number.isFinite(timestamp) &&
    calendar.toISOString().startsWith(value.slice(0, 10))
    ? timestamp
    : NaN;
}

export function subscriptionFreshness(
  subscription: Observation,
  now = Date.now(),
) {
  const state =
    subscription.panel_sync_state ?? subscription.device_inventory_state;
  const observed = subscription.observed_at;
  const timestamp = instant(observed);
  const valid = Number.isFinite(timestamp) && timestamp <= now;
  const recent = valid && now - timestamp < 10 * 60 * 1000;
  const updated = valid
    ? `Данные обновлены ${date(observed, true)} · МСК.`
    : "";
  if (state === "pending")
    return {
      warning: true,
      text: `Изменения ещё применяются. ${updated}`.trim(),
    };
  if (state === "denied")
    return {
      warning: true,
      text: `Состояние подключения пока не подтверждено. ${updated}`.trim(),
    };
  if (state === "unknown") return null;
  if (state === "fresh" && recent) return { warning: false, text: updated };
  if (state === "stale" || state === "unavailable" || state === "fresh")
    return {
      warning: true,
      text: `Показаны последние сохранённые данные. ${updated}`.trim(),
    };
  return null;
}

export function SubscriptionFreshness({
  subscription,
}: {
  subscription: Observation;
}) {
  const hint = subscriptionFreshness(subscription);
  return hint ? (
    <p className={hint.warning ? "notice warning" : "fine-print"}>
      {hint.text}
    </p>
  ) : null;
}

export function PaidHorizon({
  subscription,
}: {
  subscription: Pick<Subscription, "expires_at" | "paid_horizon">;
}) {
  const horizon = instant(subscription.paid_horizon);
  const expires = instant(subscription.expires_at);
  return Number.isFinite(horizon) &&
    Number.isFinite(expires) &&
    horizon > expires ? (
    <p className="fine-print">
      В истории оплачено до {date(subscription.paid_horizon)}
    </p>
  ) : null;
}
