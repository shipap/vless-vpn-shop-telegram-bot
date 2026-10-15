import { readFile } from "node:fs/promises";

export default async function cleanup() {
  const runId = process.env.SUNRISE_E2E_AUTH_RUN_ID;
  if (!runId) return;
  let status: Response;
  try {
    status = await fetch("http://127.0.0.1:8150/__e2e__/status");
  } catch {
    const lifecycle = JSON.parse(
      await readFile(".artifacts/e2e-auth-lifecycle.json", "utf8"),
    );
    if (lifecycle.run_id === runId && lifecycle.schema_dropped === true) return;
    throw new Error(
      "Synthetic auth temporary schema cleanup was not confirmed",
    );
  }
  const identity = await status.json();
  if (identity.environment !== "isolated-e2e-auth" || identity.run_id !== runId)
    throw new Error("Synthetic auth server ownership did not match this run");
  const stopped = await fetch("http://127.0.0.1:8150/__e2e__/shutdown", {
    method: "POST",
    headers: { "X-E2E-Run-ID": runId },
  });
  if (!stopped.ok) throw new Error("Synthetic auth shutdown failed");
  const deadline = Date.now() + 10000;
  while (Date.now() < deadline) {
    const lifecycle = JSON.parse(
      await readFile(".artifacts/e2e-auth-lifecycle.json", "utf8"),
    );
    if (lifecycle.run_id === runId && lifecycle.schema_dropped === true) return;
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error("Synthetic auth temporary schema cleanup was not confirmed");
}
