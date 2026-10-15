import { describe, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { Customer } from "./customer";
import { NodeNews } from "./node-news";
import type { Catalog, Dashboard, Session } from "@api/types";

const data = vi.hoisted(() => ({
  paths: [] as string[],
  feed: {
    items: [
      {
        id: "local-recovery",
        title: "Тестовый узел",
        state: "recovered",
        occurred_at: "2026-10-11T12:00:00Z",
        internal_uuid: "hidden-private",
        ip: "hidden-ip",
      },
      {
        id: "local-outage",
        title: "Тестовый узел",
        state: "outage",
        occurred_at: "2026-10-11T11:00:00Z",
      },
    ],
    next_cursor: "opaque-more",
  },
}));
vi.mock("@api/client", async (original) => ({
  ...(await original<typeof import("@api/client")>()),
  useApi: (path: string) => {
    data.paths.push(path);
    return {
      data: data.feed,
      loading: false,
      error: null,
      reload: () => {},
      setData: () => {},
    };
  },
}));
describe("dedicated node news", () => {
  it("renders only safe feed fields and its chronological order", () => {
    const account = { id: "9007199254740993123" };
    const resource = <T,>(value: T) => ({
      data: value,
      loading: false,
      error: null,
      reload: () => {},
      setData: () => {},
    });
    const html = renderToStaticMarkup(
      <Customer
        page="news"
        session={{ account } as Session}
        refreshSession={() => {}}
        config={null}
        catalog={resource({
          products: [],
          legal: { documents: [] },
        } as unknown as Catalog)}
        dashboard={resource({
          account,
          subscriptions: [],
          payments: [],
        } as unknown as Dashboard)}
      />,
    );
    expect(html).toContain("Новости серверов");
    expect(html).toContain("Работа восстановлена");
    expect(html).toContain("Перебои в работе");
    expect(html.indexOf("Работа восстановлена")).toBeLessThan(
      html.indexOf("Перебои в работе"),
    );
    expect(html).not.toContain("hidden-");
    expect(html).not.toContain("notification-toast");
    expect(html).not.toContain("Обновить");
    expect(html).toContain("Более ранние события");
    expect(data.paths).toEqual(["/node-news?limit=20"]);
  });

  it("compacts repeated server events into one accessible group with latest state and retained chronology", () => {
    const html = renderToStaticMarkup(<NodeNews />);
    expect(html.match(/<article/g)).toHaveLength(1);
    expect(html.match(/<h3>Тестовый узел<\/h3>/g)).toHaveLength(1);
    expect(html).toContain("История событий");
    expect(html).toContain("<details");
    expect(html).toContain("<ol");
    expect(html).toContain('dateTime="2026-10-11T12:00:00Z"');
    expect(html).toContain('dateTime="2026-10-11T11:00:00Z"');
    expect(html.indexOf("2026-10-11T12:00:00Z")).toBeLessThan(
      html.indexOf("2026-10-11T11:00:00Z"),
    );
    expect(html).not.toContain("hidden-");
  });

  it("keeps all outage history for several servers without repeating expanded event cards", () => {
    const original = data.feed.items;
    data.feed.items = Array.from({ length: 20 }, (_, index) => ({
      id: `synthetic-event-${index}`,
      title: `🇳🇱 Сервер #${(index % 4) + 1}`,
      state: index < 4 ? "recovered" : "outage",
      occurred_at: `2026-10-11T${String(20 - index).padStart(2, "0")}:00:00Z`,
    }));
    try {
      const html = renderToStaticMarkup(<NodeNews />);
      expect(html.match(/<article/g)).toHaveLength(4);
      expect(html.match(/<li>/g)).toHaveLength(20);
      expect(html.match(/<details/g)).toHaveLength(4);
      expect(html).not.toContain("<details open");
      expect(html.match(/История событий · 5/g)).toHaveLength(4);
      expect(html).toContain("🇳🇱 Сервер #4");
      expect(html).toContain("Более ранние события");
    } finally {
      data.feed.items = original;
    }
  });

  it("explains an empty feed without a subscription", () => {
    const original = data.feed.items;
    data.feed.items = [];
    try {
      const html = renderToStaticMarkup(<NodeNews hasSubscription={false} />);
      expect(html).toContain(
        "Для получения информации о серверах нужна подписка",
      );
      expect(html).not.toContain("Новостей пока нет");
    } finally {
      data.feed.items = original;
    }
  });

  it("does not suggest buying a subscription when no recent events exist", () => {
    const original = data.feed.items;
    data.feed.items = [];
    try {
      const html = renderToStaticMarkup(<NodeNews hasSubscription />);
      expect(html).toContain("За последние 24 часа событий нет");
      expect(html).not.toContain("нужна подписка");
    } finally {
      data.feed.items = original;
    }
  });
});
