import { describe, expect, it } from "vitest";
import { validAdminDate } from "./admin-date-field";

describe("admin calendar ISO validation", () => {
  it.each([
    ["2024-02-29", true],
    ["2026-02-29", false],
    ["2026-04-31", false],
    ["2026-10-14", true],
    ["2026-13-01", false],
    ["14.10.2026", false],
    ["", true],
  ])("accepts only a real ISO day %s", (value, accepted) => {
    expect(validAdminDate(value, "date")).toBe(accepted);
  });
  it("keeps local datetime minutes and rejects impossible clock values", () => {
    expect(validAdminDate("2026-10-14T12:30", "datetime-local")).toBe(true);
    expect(validAdminDate("2026-10-14T24:00", "datetime-local")).toBe(false);
    expect(validAdminDate("2026-10-14T12:60", "datetime-local")).toBe(false);
    expect(validAdminDate("2026-10-14T12:30Z", "datetime-local")).toBe(false);
  });
  it("enforces exact inclusive date and local datetime boundaries", () => {
    expect(
      validAdminDate("2026-10-14", "date", "2026-10-14", "2026-10-15"),
    ).toBe(true);
    expect(validAdminDate("2026-10-13", "date", "2026-10-14")).toBe(false);
    expect(validAdminDate("2026-10-16", "date", undefined, "2026-10-15")).toBe(
      false,
    );
    expect(
      validAdminDate("2026-10-14T12:29", "datetime-local", "2026-10-14T12:30"),
    ).toBe(false);
  });
});
