import { beforeEach, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { ApiError } from "@api/client";
import { NodeNews } from "../../apps/web/dashboard/node-news";

const resource = vi.hoisted(() => ({
  data: { items: [], next_cursor: "older-event" },
  error: null as unknown,
  loading: false,
  reload: () => {},
}));
vi.mock("@api/client", async (original) => ({
  ...(await original<typeof import("@api/client")>()),
  useApi: () => resource,
}));

beforeEach(() => {
  resource.error = null;
  resource.loading = false;
});

it("does not offer a stale next page after a failed reload", () => {
  resource.error = new Error("Сервис недоступен");
  const html = renderToStaticMarkup(<NodeNews />);
  expect(html).toContain('role="alert"');
  expect(html).toContain("Сервис недоступен");
  expect(html).toContain("Попробовать снова");
  expect(html).not.toContain("Более ранние события");
});

it("disables paging while an existing page is reloading", () => {
  resource.loading = true;
  const html = renderToStaticMarkup(<NodeNews />);
  expect(html).toMatch(
    /<button[^>]*disabled=""[^>]*>Более ранние события<\/button>/,
  );
});

it.each([0, 404, 503])(
  "keeps first-page failures actionable (status %i)",
  (status) => {
    resource.error = new ApiError("Сервис недоступен", status, "UNAVAILABLE");
    const html = renderToStaticMarkup(<NodeNews />);
    expect(html).toContain('role="alert"');
    expect(html).toContain("Сервис недоступен");
    expect(html).toContain("Попробовать снова");
    expect(html).not.toContain("История обновилась");
  },
);
