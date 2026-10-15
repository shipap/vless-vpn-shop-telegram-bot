import { expect, it } from "vitest";
import { neutralInstance, publicInstance } from "./instance-value";
it("fails closed to neutral identity and refuses private asset paths and active URL schemes", () => {
  for (const value of [
    null,
    {},
    [],
    { app_name: "private", visual_profile: "unexpected" },
  ])
    expect(publicInstance(value)).toEqual(neutralInstance);
  const value = publicInstance({
    ...neutralInstance,
    app_name: "Другой VPN",
    visual_profile: "private",
    logo_url: "/brand/shop-32.png",
    hero_url: "https://external.test/image.png",
    support_url: "javascript:alert(1)",
    dashboard_url: "https://user:secret@example.test/",
  });
  expect(value.app_name).toBe("Другой VPN");
  expect(value.logo_url).toBeNull();
  expect(value.hero_url).toBeNull();
  expect(value.support_url).toBeNull();
  expect(value.dashboard_url).toBeNull();
  expect(
    publicInstance({ ...value, hero_url: "/api/v1/instance/assets/hero" })
      .hero_url,
  ).toBe("/api/v1/instance/assets/hero");
});
