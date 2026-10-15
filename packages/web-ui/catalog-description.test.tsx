import { describe, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import type { Product } from "@api/types";
import { TariffCard } from "../../apps/web/landing/TariffCard";
import { CatalogComparison } from "./catalog-comparison";
import { CatalogAdmin } from "../../apps/web/dashboard/admin-v6-catalog";
import { CatalogDescription, SemanticContent } from "./content";

let adminProduct: Product;
vi.mock("@api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@api/client")>()),
  useApi: () => ({ data: { items: [adminProduct] } }),
}));

const product: Product = {
  id: "synthetic-product",
  code: "synthetic",
  name: "Тестовый тариф",
  description: "Первая строка\nВторая строка",
  price: "123.45",
  currency: "RUB",
  tier_level: 1,
  version: 1,
  included_devices: 2,
  available: true,
  extra_device_price: null,
  semantic_role: "synthetic",
  visible_landing: true,
  visible_bot: true,
  sort_order: 1,
  highlighted: false,
  icon: null,
  base_period_days: 30,
  periods: [],
  eligibility: { allow_upgrade: true, allow_downgrade: true },
};

const renderers = {
  landing: (value: Product) =>
    renderToStaticMarkup(<TariffCard product={value} />),
  comparison: (value: Product) =>
    renderToStaticMarkup(<CatalogComparison products={[value]} />),
  admin: (value: Product) => {
    adminProduct = value;
    return renderToStaticMarkup(
      <CatalogAdmin mutate={() => undefined} refreshVersion={0} />,
    );
  },
};

describe.each(Object.entries(renderers))(
  "%s catalog description",
  (_, render) => {
    it("falls back when semantic content exceeds the renderer limit", () => {
      const html = render({
        ...product,
        content: {
          description: Array.from({ length: 501 }, () => ({
            type: "text",
            text: "Скрытый текст",
          })),
        },
      });
      expect(html).toContain("Первая строка\nВторая строка");
      expect(html).not.toContain("Скрытый текст");
    });
    it.each(
      [
        [],
        [null],
        [{ type: "text", text: { invalid: true } }],
        [{ type: "unknown" }],
        [{ type: "text", text: "" }],
        [{ type: "text", text: " \n\t " }],
        [{ type: "paragraph", children: [] }],
        [{ type: "br" }],
        [{ type: "list", items: [[]] }],
        [{ type: "table", rows: [[[]]] }],
      ].map((description) => ({ description })),
    )(
      "preserves plain fallback when semantic content is empty or invalid: %j",
      ({ description }) => {
        const html = render({ ...product, content: { description } });
        expect(html).toContain("Первая строка\nВторая строка");
      },
    );

    it.each([
      { description: [{ type: "emoji", fallback: "🇳🇱" }], expected: "🇳🇱" },
      {
        description: [
          { type: "list", items: [[{ type: "text", text: "Видимый пункт" }]] },
        ],
        expected: "<li>Видимый пункт</li>",
      },
      {
        description: [
          {
            type: "table",
            rows: [[[{ type: "text", text: "Видимая ячейка" }]]],
          },
        ],
        expected: "<td>Видимая ячейка</td>",
      },
      {
        description: [{ type: "details", title: "Подробнее", children: [] }],
        expected: "<summary>Подробнее</summary>",
      },
      {
        description: [
          {
            type: "details",
            children: [{ type: "text", text: "Видимый текст" }],
          },
        ],
        expected: "Видимый текст",
      },
    ])(
      "prefers meaningful semantic content without a plain duplicate: $expected",
      ({ description, expected }) => {
        const html = render({ ...product, content: { description } });
        expect(html).toContain(expected);
        expect(html).not.toContain("Первая строка");
      },
    );

    it("renders semantic lists and line breaks without decorative emoji or a plain duplicate", () => {
      const html = render({
        ...product,
        content: {
          description: [
            { type: "emoji", fallback: "🌍" },
            { type: "br" },
            {
              type: "list",
              ordered: true,
              items: [[{ type: "text", text: "Структурированная строка" }]],
            },
          ],
        },
      });
      expect(html).not.toContain("🌍");
      expect(html).toContain("<br/>");
      expect(html).toContain("<ol><li>Структурированная строка</li></ol>");
      expect(html).not.toContain("Первая строка");
      expect(html.match(/Структурированная строка/g)).toHaveLength(1);
    });

    it("escapes literal legacy HTML and keeps unsafe semantic URLs noninteractive", () => {
      const plain = render({
        ...product,
        description:
          '<tg-emoji emoji-id="123">🌍</tg-emoji>\n<script>alert(1)</script>',
        content: { description: [] },
      });
      expect(plain).not.toContain("<script>");
      expect(plain).not.toContain("<tg-emoji");
      expect(plain).toContain("&lt;script&gt;");
      const semantic = render({
        ...product,
        content: {
          description: [
            {
              type: "link",
              href: "javascript:alert(1)",
              children: [{ type: "text", text: "Ссылка" }],
            },
          ],
        },
      });
      expect(semantic).toContain("Ссылка");
      expect(semantic).not.toContain("javascript:");
    });
  },
);

describe("website catalog emoji policy", () => {
  it("strips decorative Telegram emoji from comparison headings without losing names or flags", () => {
    const html = renderers.comparison({
      ...product,
      name: '<tg-emoji emoji-id="123">✨</tg-emoji> Тариф 🇪🇸 ✅',
      description: "🇪🇸 Испания",
    });
    expect(html).toContain('<th scope="col"> Тариф 🇪🇸 </th>');
    expect(html).not.toMatch(/✨|✅|tg-emoji|emoji-asset/u);
    expect(html).toContain("🇪🇸 Испания");
  });

  it("keeps missing legacy descriptions empty instead of breaking the checkout view", () => {
    const html = renderToStaticMarkup(
      <CatalogDescription nodes={[]} text={undefined as unknown as string} />,
    );
    expect(html).toContain('<div class="catalog-description"><p></p></div>');
  });

  it("removes legacy Telegram tags embedded in semantic text while retaining ordinary copyright and registered marks", () => {
    const html = renderToStaticMarkup(
      <CatalogDescription
        nodes={[
          {
            type: "text",
            text: '<tg-emoji emoji-id="123">✨</tg-emoji> 🇪🇸 Испания · © 2026 · WireGuard® · знак™',
          },
        ]}
        text="Запасной текст"
      />,
    );
    expect(html).toContain(" 🇪🇸 Испания · © 2026 · WireGuard® · знак™");
    expect(html).not.toMatch(/tg-emoji|✨|Запасной текст/u);
  });

  it("removes plain emoji sequences and custom emoji while preserving flags and exact text structure", () => {
    const html = renderToStaticMarkup(
      <CatalogDescription
        nodes={[]}
        text={
          '<tg-emoji emoji-id="123">✨</tg-emoji> Страны 🇳🇱 🇺🇸\n\n✅ Доступ 👩🏽‍💻 🏳️‍🌈 🏴\u{e0067}\u{e0062}\u{e007f} 1️⃣ ☀️ ❤ ★ ✦\n123 #2 * текст'
        }
      />,
    );
    expect(html).toContain(" Страны 🇳🇱 🇺🇸\n\n Доступ        \n123 #2 * текст");
    expect(html).not.toMatch(/tg-emoji|emoji-asset|✨|✅|👩|🏳|🏴|1️⃣|☀|❤|★|✦/u);
    expect(html).not.toMatch(/[\u200d\u{e0067}\u{e0062}\u{e007f}]/u);
  });

  it("keeps semantic paragraphs, lists, links and flags while removing custom assets and decorative summaries", () => {
    const html = renderToStaticMarkup(
      <CatalogDescription
        text="Не показывать дубль"
        nodes={[
          {
            type: "paragraph",
            children: [
              { type: "emoji", id: "123", fallback: "✨" },
              { type: "text", text: "Текст ✅ 🇳🇱" },
            ],
          },
          {
            type: "list",
            ordered: true,
            items: [
              [
                { type: "emoji", id: "456", fallback: "🇺🇸" },
                {
                  type: "bold",
                  children: [{ type: "text", text: " Сервер 🚀" }],
                },
              ],
            ],
          },
          {
            type: "details",
            title: "⚡ Подробнее",
            children: [
              {
                type: "link",
                href: "https://example.com",
                children: [{ type: "text", text: "Связь 🔥" }],
              },
            ],
          },
        ]}
      />,
    );
    expect(html).toContain("<p>Текст  🇳🇱</p>");
    expect(html).toContain("<ol><li>🇺🇸<strong> Сервер </strong></li></ol>");
    expect(html).toContain("<summary> Подробнее</summary>");
    expect(html).toContain('href="https://example.com/"');
    expect(html).not.toContain("emoji-asset");
    expect(html).not.toMatch(/✨|✅|🚀|⚡|🔥|Не показывать дубль/u);
  });

  it("falls back to meaningful plain text when semantic content contains only removed emoji", () => {
    const html = renderToStaticMarkup(
      <CatalogDescription
        nodes={[{ type: "emoji", id: "123", fallback: "✨" }]}
        text="Описание ✅ 🇩🇪"
      />,
    );
    expect(html).toContain("<p>Описание  🇩🇪</p>");
    expect(html).not.toContain("emoji-asset");
  });

  it("retains rich emoji for the general semantic renderer", () => {
    const html = renderToStaticMarkup(
      <SemanticContent
        nodes={[
          { type: "emoji", id: "123", fallback: "✨" },
          { type: "text", text: "✅" },
        ]}
      />,
    );
    expect(html).toContain("emoji-asset");
    expect(html).toContain("✨");
    expect(html).toContain("✅");
  });
});
