import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { Customer } from "./customer";
import type { Catalog, Dashboard, Session, Subscription } from "@api/types";

const account = { id: "9007199254740993123", email_verified: true };
function render(
  due: unknown,
  extra: Record<string, unknown> = {},
  values: Record<string, unknown> = {},
  page = "home",
) {
  const resource = <T,>(data: T) => ({
    data,
    error: null,
    loading: false,
    reload: () => {},
    setData: () => {},
  });
  const sub = {
    id: "test-sub",
    name: "Подписка",
    status: "active",
    devices: [],
    segments: [],
    devices_limit: 4,
    connection_url: "https://example.test/connect",
    expires_at: "2037-01-01T00:00:00Z",
    retention_state: "none",
    connection_help_due: due,
    ...values,
  } as unknown as Subscription;
  return renderToStaticMarkup(
    <Customer
      page={page}
      session={{ account } as Session}
      refreshSession={() => {}}
      config={null}
      catalog={resource({
        products: [],
        periods: [],
        legal: { configured: false, documents: [], revision: null },
      } as unknown as Catalog)}
      dashboard={resource({
        account,
        subscriptions: [sub],
        payments: [],
        ...extra,
      } as unknown as Dashboard)}
    />,
  );
}
describe("backend connection help projection", () => {
  it("shows the instruction prompt only for the true backend indicator", () => {
    expect(render(true)).toContain("первого подключения пока нет");
    expect(render(true)).toContain("Откройте инструкцию подключения");
  });
  for (const due of [false, null, undefined]) {
    it(`does not guess no connections from ${due}`, () => {
      expect(render(due)).not.toContain("первого подключения пока нет");
    });
  }
  it("shows a proven first connection without deriving it from traffic", () => {
    const html = render(
      false,
      {},
      { ever_connected: true, first_connected_at: "2026-10-11T12:00:00Z" },
    );
    expect(html).toContain("Первое подключение подтверждено");
    expect(html).toContain("11 октября 2026");
    expect(html).not.toContain("первого подключения пока нет");
  });
  it("displays backend finite traffic progress and warning", () => {
    const html = render(
      false,
      {},
      { traffic_progress_percent: "81.5", traffic_warning: true },
    );
    expect(html).toContain("81.5%");
    expect(html).toContain("Лимит трафика почти исчерпан");
    expect(html).toContain('value="81.5"');
    expect(render(false)).not.toContain("Лимит трафика почти исчерпан");
    expect(render(false)).not.toContain("<progress");
  });
  it("keeps deletion notice and existing agreement cancellation when active cards are empty", () => {
    const html = render(false, {
      subscriptions: [],
      subscription_deletion_notices: [
        {
          subscription_id: "archived-test",
          name: "Удалённая подписка",
          deleted_at: "2026-10-11T12:00:00Z",
          reason: "manual_panel",
        },
      ],
      archived_subscription_agreements: [
        {
          subscription_id: "archived-test",
          name: "Удалённая подписка",
          recurring: {
            id: "agreement",
            state: "active",
            amount: "100.00",
            currency: "RUB",
            next_charge_at: null,
          },
        },
      ],
    });
    expect(html).toContain("Подписка была удалена");
    expect(html).toContain("Если это ошибка, обратитесь в поддержку");
    expect(html).toContain("Удалённая подписка");
    expect(html).toContain("Отменить автопродление");
    expect(html).not.toContain("connect-button");
    expect(html).not.toContain("Перевыпустить ссылку");
  });
});
