import { describe, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { NotificationCenter } from "./notification-center";

const fixture = vi.hoisted(() => ({ cursor: 0, actions: [] as unknown[] }));
vi.mock("react", async (original) => ({
  ...(await original<typeof import("react")>()),
  useState: (initial: unknown) => {
    const slot = fixture.cursor++;
    const value =
      slot === 0
        ? true
        : slot === 1
          ? {
              unread_count: 1,
              items: [
                {
                  id: "owned-local",
                  kind: "subscription_expiry",
                  title: "Срок подписки",
                  body: "Проверьте расчёт",
                  severity: "info",
                  created_at: "2026-10-11T12:00:00Z",
                  read_at: null,
                  target: "/#subscriptions",
                  actions: fixture.actions,
                },
              ],
            }
          : typeof initial === "function"
            ? initial()
            : initial;
    return [value, () => {}];
  },
}));
function render(actions: unknown[]) {
  fixture.cursor = 0;
  fixture.actions = actions;
  return renderToStaticMarkup(
    <NotificationCenter accountId="9007199254740993123" onAction={() => {}} />,
  );
}
describe("bounded notification actions", () => {
  it("shows the server-labelled specific renewal action", () => {
    const html = render([
      {
        label: "Продлить выбранную подписку",
        target: "/#subscriptions",
        operation: "renew",
        metadata: { subscription_id: "own-local", quote_id: "own-quote" },
      },
    ]);
    expect(html).toContain("Продлить выбранную подписку");
  });
  for (const action of [
    {
      label: "Недопустимое действие",
      target: "https://example.test/private",
      operation: "connect",
      metadata: { subscription_id: "own-local" },
    },
    {
      label: "Недопустимое действие",
      target: "/#subscriptions",
      operation: "delete",
      metadata: { subscription_id: "own-local" },
    },
    {
      label: "Недопустимое действие",
      target: "/#subscriptions",
      operation: "renew",
      metadata: {},
    },
    {
      label: "Недопустимое действие",
      target: "/#subscriptions",
      operation: ["renew"],
      metadata: { subscription_id: "own-local" },
    },
  ]) {
    it("does not render an arbitrary operation/link or missing local identity", () => {
      expect(render([action])).not.toContain("Недопустимое действие");
    });
  }
});
