import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import {
  safeUrl,
  money,
  date,
  SemanticContent,
  ContentOrText,
  validatedContentNodes,
} from "./content";

describe("Safe customer content", () => {
  it("validates notification and FAQ node shapes before rendering with plain-text fallback", () => {
    for (const invalid of [
      [null],
      [{ type: "text", text: { unsafe: "object" } }],
      [{ type: "table", rows: [{}] }],
      [{ type: "paragraph", children: "bad" }],
      [{ type: "unknown", text: "ignored" }],
    ]) {
      expect(validatedContentNodes(invalid)).toBeNull();
      expect(
        renderToStaticMarkup(
          <ContentOrText nodes={invalid} text="Запасной текст" />,
        ),
      ).toBe("<p>Запасной текст</p>");
    }
    const html = renderToStaticMarkup(
      <ContentOrText
        nodes={[
          {
            type: "paragraph",
            children: [
              {
                type: "link",
                href: "javascript:alert(1)",
                children: [{ type: "text", text: "Безопасный текст" }],
              },
            ],
          },
        ]}
        text="Fallback"
      />,
    );
    expect(html).toContain("Безопасный текст");
    expect(html).not.toContain("javascript:");
    expect(html).not.toContain("href=");
  });
  it("keeps date-only access deadlines in Moscow across UTC midnight", () => {
    const value = "2026-10-07T22:30:00Z";
    expect(date(value)).toBe("8 октября 2026 г.");
    expect(date(value, false, "UTC")).toBe("7 октября 2026 г.");
    expect(date(value, true)).toContain("8 октября");
    expect(date(value, true)).toContain("01:30");
    for (const invalid of [null, undefined, "invalid"]) {
      expect(date(invalid)).toBe("Не указана");
      expect(date(invalid, true)).toBe("Не указана");
    }
  });
  it("formats analytics timestamps in the applied timezone", () => {
    const value = "2026-10-07T22:30:00Z";
    expect(date(value, true, "UTC")).toContain("7 октября");
    expect(date(value, true, "UTC")).toContain("22:30");
    expect(date(value, true, "Europe/Moscow")).toContain("8 октября");
  });
  it("requests custom emoji through the local asset route without rounding its ID", () => {
    const html = renderToStaticMarkup(
      <SemanticContent
        nodes={[
          {
            type: "emoji",
            id: "90071992547409999",
            fallback: "☀️",
            label: "Солнце",
          },
        ]}
      />,
    );
    expect(html).toContain('src="/api/v1/content/emoji/90071992547409999"');
    expect(html).toContain('loading="lazy"');
    expect(html).toContain("☀️");
    expect(html).not.toContain("api.telegram.org");
  });
  it("preserves semantic heading levels and ordered lists", () => {
    const html = renderToStaticMarkup(
      <SemanticContent
        nodes={[
          {
            type: "heading",
            level: 2,
            children: [{ type: "text", text: "Инструкция" }],
          },
          {
            type: "list",
            ordered: true,
            items: [[{ type: "text", text: "Первый шаг" }]],
          },
        ]}
      />,
    );
    expect(html).toContain("<h2>Инструкция</h2>");
    expect(html).toContain("<ol>");
  });
  it("rejects executable and credential-bearing URLs", () => {
    for (const value of [
      "javascript:alert(1)",
      "data:text/html,test",
      "//evil.test",
      "https://user:pass@example.com",
    ])
      expect(safeUrl(value)).toBeUndefined();
    expect(safeUrl("https://example.com/help")).toBe(
      "https://example.com/help",
    );
  });
  it("preserves large monetary decimals without Number rounding", () => {
    expect(money("-0.01", "RUB")).toBe("-0,01 ₽");
    expect(money("9007199254740993.99", "RUB")).toContain(
      "9 007 199 254 740 993,99",
    );
    expect(money(null, "RUB")).toBe("Неизвестно");
  });
  it("escapes text, blocks executable links and preserves emoji fallback", () => {
    const html = renderToStaticMarkup(
      <SemanticContent
        nodes={[
          { type: "text", text: "<script>alert(1)</script>" },
          {
            type: "link",
            href: "javascript:alert(1)",
            children: [{ type: "text", text: "ссылка" }],
          },
          {
            type: "emoji",
            id: "90071992547409999",
            fallback: "☀️",
            label: "Солнце",
          },
        ]}
      />,
    );
    expect(html).not.toContain("<script>");
    expect(html).not.toContain("javascript:");
    expect(html).toContain("☀️");
  });
});
