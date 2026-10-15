import { describe, it, expect } from "vitest";
import { analyticsView, exportRows, factRows } from "./analytics";
describe("Financial presentation from backend facts", () => {
  it("keeps backend net settlements intact without subtracting their confirmed fee twice", () => {
    const view = analyticsView(
      {
        currencies: { RUB: "99.00" },
        net_after_refunds: { RUB: "89.00" },
        previous: { RUB: "0.00" },
        net_usdt: "0.92",
        confirmed_fees: { USDT: "0.03" },
      },
      "RUB",
    );
    expect(view.totals.net_after_refunds).toBe("89.00");
    expect(view.totals.net_usdt).toBe("0.92");
    expect(view.totals.previous).toBe("0.00");
  });
  it("distinguishes absent aggregates from an observed empty result", () => {
    expect(factRows({}, "sales_composition")).toBeNull();
    expect(factRows({ sales_composition: [] }, "sales_composition")).toEqual(
      [],
    );
    expect(
      factRows(
        {
          sales_composition: [
            { currency: "RUB", count: 2 },
            { currency: "EUR", count: 4 },
          ],
        },
        "sales_composition",
        "RUB",
      ),
    ).toEqual([{ currency: "RUB", count: 2 }]);
  });
  it("keeps currencies separate and unknown commission null", () => {
    const view = analyticsView(
      {
        currencies: { RUB: "99.00", EUR: "4.00" },
        refunds: { RUB: "10.00" },
        series: [
          { date: "2026-10-07", currency: "RUB", amount: "99.00" },
          { date: "2026-10-07", currency: "EUR", amount: "4.00" },
        ],
        rows: [{ currency: "RUB", amount: "99.00", fee: null }],
        paid_count: 2,
      },
      "RUB",
    );
    expect(view.totals.external_payments).toBe("99.00");
    expect(view.totals.known_fees).toBeNull();
    expect(view.series).toHaveLength(1);
    expect(view.series[0].payments).toBe("99.00");
    expect(view.series[0].refunds).toBeNull();
  });
  it("neutralizes spreadsheet formulas without altering valid decimals", () => {
    const csv = exportRows([
      {
        id: "=CMD()",
        date: "2026-10-07",
        amount: "9007199254740993.99",
        currency: "RUB",
        fee: null,
      },
    ]);
    expect(csv).toContain("'=CMD()");
    expect(csv).toContain("9007199254740993.99");
    expect(csv).toContain("Неизвестно");
  });
});
