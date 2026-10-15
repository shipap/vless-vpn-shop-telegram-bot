import { expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { BroadcastReport } from "../../apps/web/dashboard/broadcast-report";

it("separates waiting, refused and unknown outcomes from delivered recipients", () => {
  const html = renderToStaticMarkup(
    <BroadcastReport
      value={{
        state: "done",
        recipient_count: 8,
        delivered_count: 2,
        blocked_count: 1,
        provider_rejected_count: 1,
        unknown_count: 1,
        waiting_count: 2,
        cancelled_count: 1,
      }}
    />,
  );
  expect(html).toContain("Доставлено");
  expect(html).toContain("Неизвестный результат");
  expect(html).toContain("Ожидают доставки");
  expect(html).toContain("Отклонено Telegram или почтой");
  expect(html).toContain("не означает доставку");
});

it("does not claim preparation finished for a pending campaign", () => {
  const html = renderToStaticMarkup(
    <BroadcastReport
      value={{ state: "pending", recipient_count: 8, waiting_count: 8 }}
    />,
  );
  expect(html).not.toContain("Подготовка очереди завершена");
  expect(html).toContain("Ожидают доставки");
});
