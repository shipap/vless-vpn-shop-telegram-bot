import { expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import {
  SubscriptionFreshness,
  subscriptionFreshness,
  PaidHorizon,
} from "./subscription-freshness";

const now = Date.parse("2026-10-09T08:05:00Z");
const observed_at = "2026-10-09T08:00:00Z";

it("formats a recent confirmed observation in Moscow time", () => {
  expect(
    subscriptionFreshness(
      { observed_at, device_inventory_state: "fresh" },
      now,
    ),
  ).toEqual({
    warning: false,
    text: "Данные обновлены 9 октября 2026 г. в 11:00 · МСК.",
  });
});
it.each(["stale", "unavailable"] as const)(
  "identifies %s observations as cached",
  (device_inventory_state) => {
    expect(
      subscriptionFreshness({ observed_at, device_inventory_state }, now)?.text,
    ).toContain("Показаны последние сохранённые данные");
  },
);
it.each(["not-a-date", "2026-10-09T09:00:00Z", "2026-10-09T07:00:00Z", null])(
  "never promises fresh data for an invalid, future, old or missing date (%s)",
  (observed_at) => {
    const value = subscriptionFreshness(
      { observed_at, device_inventory_state: "fresh" },
      now,
    );
    expect(value?.warning).toBe(true);
    expect(value?.text).not.toContain("Invalid Date");
  },
);
it.each(["pending", "denied"] as const)(
  "prioritizes %s panel synchronization over a fresh device inventory",
  (panel_sync_state) => {
    const value = subscriptionFreshness(
      { observed_at, device_inventory_state: "fresh", panel_sync_state },
      now,
    );
    expect(value?.warning).toBe(true);
  },
);
it("renders no alarming hint for an unknown demo observation", () => {
  expect(
    renderToStaticMarkup(
      <SubscriptionFreshness
        subscription={{ observed_at: null, device_inventory_state: "unknown" }}
      />,
    ),
  ).toBe("");
});

it.each(["0", "2026-02-30T08:00:00Z", "2026-10-09T24:00:00Z"])(
  "rejects malformed calendar observations (%s)",
  (observed_at) => {
    expect(
      subscriptionFreshness(
        { observed_at, device_inventory_state: "fresh" },
        now,
      )?.text,
    ).not.toContain("Данные обновлены");
  },
);

it("only displays valid paid rights beyond the current expiry", () => {
  const subscription = {
    expires_at: "2027-01-01T00:00:00Z",
    paid_horizon: "2027-02-01T00:00:00Z",
  };
  expect(
    renderToStaticMarkup(<PaidHorizon subscription={subscription} />),
  ).toContain("В истории оплачено до 1 февраля 2027 г.");
  for (const paid_horizon of [
    null,
    "not-a-date",
    "2026-12-01T00:00:00Z",
    subscription.expires_at,
  ]) {
    expect(
      renderToStaticMarkup(
        <PaidHorizon subscription={{ ...subscription, paid_horizon }} />,
      ),
    ).toBe("");
  }
});
