import { expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import BellToggle from "./reactbits/BellToggle";

it("associates the notification bell with its native popover without a scripted toggle", () => {
  const html = renderToStaticMarkup(
    <BellToggle
      label="Уведомления"
      popoverTarget="owned-inbox"
      controls="owned-inbox"
      expanded={false}
    />,
  );
  expect(html).toMatch(/<button[^>]*popovertarget="owned-inbox"/i);
  expect(html).toMatch(/<button[^>]*aria-expanded="false"/);
});
