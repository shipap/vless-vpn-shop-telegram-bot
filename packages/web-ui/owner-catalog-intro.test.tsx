import { expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { Landing } from "../../apps/web/landing/Landing";

vi.mock("@api/client", () => ({
  useApi: () => ({
    data: {
      products: [
        {
          id: "synthetic",
          name: "Выбор тарифа",
          description: "Описание тарифа",
          price: "99.00",
          currency: "RUB",
          included_devices: 4,
          available: true,
        },
      ],
      legal: { documents: [] },
      content: {
        catalog_description: "Описание владельца",
        catalog_description_content: [
          {
            type: "paragraph",
            children: [
              {
                type: "bold",
                text: "",
                children: [{ type: "text", text: "Описание владельца" }],
              },
            ],
          },
        ],
      },
    },
    loading: false,
    error: null,
  }),
}));

it("uses the owner introduction alone while keeping separate selectable tariff cards", () => {
  const html = renderToStaticMarkup(<Landing />);
  const catalog = html.slice(
    html.indexOf('id="tariffs"'),
    html.indexOf('id="setup"'),
  );
  expect(catalog).toContain("<strong>Описание владельца</strong>");
  expect(catalog).not.toContain('id="tariffs-title"');
  expect(catalog).not.toContain("Два тарифа");
  expect(catalog).toContain('data-product-id="synthetic"');
  expect(catalog.match(/Описание владельца/g)).toHaveLength(1);
});
