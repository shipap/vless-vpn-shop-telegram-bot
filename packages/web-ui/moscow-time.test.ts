import { expect, it } from "vitest";
import { moscowInput, moscowTimestamp } from "./moscow-time";
it("edits promo timestamps in Moscow independently of browser timezone and preserves cross-midnight UTC", () => {
  expect(moscowInput("2026-10-07T21:10:00Z")).toBe("2026-10-08T00:10");
  expect(moscowTimestamp("2026-10-08T00:10")).toBe("2026-10-07T21:10:00.000Z");
  expect(moscowTimestamp("2026-02-30T00:10")).toBeNull();
  expect(moscowInput("bogus")).toBe("");
  expect(moscowTimestamp("")).toBeNull();
});
