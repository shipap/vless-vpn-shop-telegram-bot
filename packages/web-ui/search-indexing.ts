import type { PublicInstance } from "../api-client/types.ts";

export const restrictedRobots = "noindex,nofollow,noarchive";

export function searchRobots(
  instance: PublicInstance,
  landing: boolean,
  currentUrl: string | null = instance.public_site_url,
  testEnvironment = true,
): string {
  if (
    testEnvironment ||
    !landing ||
    instance.visual_profile === "private" ||
    instance.search_indexing_enabled !== true
  )
    return restrictedRobots;
  try {
    const configured = new URL(instance.public_site_url ?? "");
    const current = new URL(currentUrl ?? "");
    if (
      configured.protocol !== "https:" ||
      configured.origin !== current.origin ||
      configured.username ||
      configured.password ||
      current.username ||
      current.password ||
      /(?:^|\.)(?:localhost|test|invalid|example|internal|local)$/.test(
        current.hostname,
      ) ||
      current.hostname.includes(":") ||
      /^[\d.]+$/.test(current.hostname) ||
      /^\/(?:admin|account|dashboard|auth|login|api)(?:\/|$)/i.test(
        decodeURIComponent(current.pathname),
      )
    )
      return restrictedRobots;
    return "index,follow";
  } catch {
    return restrictedRobots;
  }
}
