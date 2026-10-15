import { useEffect, useState, useCallback } from "react";
let csrf: string | null = null;
export const setCsrf = (value: string | null) => {
  csrf = value;
};
export class ApiError extends Error {
  constructor(
    message: string,
    public status: number,
    public code: string,
    public correlationId?: string,
  ) {
    super(message);
  }
}
export async function api<T = unknown>(
  path: string,
  options: {
    method?: string;
    body?: unknown;
    rawBody?: Blob;
    signal?: AbortSignal;
    public?: boolean;
  } = {},
): Promise<T> {
  if (
    !/^\/[a-z0-9_/?=&%.,:+-]*$/i.test(path) ||
    path.includes("..") ||
    path.startsWith("//")
  )
    throw new Error("Недопустимый адрес запроса");
  if (
    options.rawBody !== undefined &&
    (!(options.rawBody instanceof Blob) ||
      options.body !== undefined ||
      options.method !== "POST" ||
      path !== "/admin/broadcasts/media" ||
      options.public ||
      !["image/jpeg", "image/png", "video/mp4"].includes(options.rawBody.type))
  )
    throw new Error("Недопустимый файл рассылки");
  const headers: Record<string, string> = { Accept: "application/json" };
  if (options.body !== undefined) headers["Content-Type"] = "application/json";
  if (options.rawBody) headers["Content-Type"] = options.rawBody.type;
  if (options.method && options.method !== "GET" && csrf)
    headers["X-CSRF-Token"] = csrf;
  let response: Response;
  try {
    response = await fetch(`/api/v1${path}`, {
      method: options.method ?? "GET",
      body:
        options.rawBody ??
        (options.body === undefined ? undefined : JSON.stringify(options.body)),
      credentials: options.public ? "omit" : "same-origin",
      cache: "no-store",
      headers,
      signal: options.signal,
      redirect: "error",
    });
  } catch (error) {
    if (error instanceof Error && error.name === "AbortError") throw error;
    throw new ApiError(
      "Не удалось связаться с сервисом. Попробуйте ещё раз.",
      0,
      "NETWORK",
    );
  }
  let data: unknown;
  try {
    data = await response.json();
  } catch {
    data = null;
  }
  if (!response.ok) {
    const error = (
      data as {
        error?: { message?: string; code?: string; correlation_id?: string };
      } | null
    )?.error;
    if (error?.code === "mfa_required" && typeof window !== "undefined") {
      window.dispatchEvent(new Event("sunrise:mfa-required"));
    }
    throw new ApiError(
      error?.message ||
        (response.status === 404
          ? "Страница не найдена."
          : "Не удалось выполнить запрос. Попробуйте позже."),
      response.status,
      error?.code ?? "UNEXPECTED",
      error?.correlation_id,
    );
  }
  return data as T;
}
export function useApi<T>(
  path: string | null,
  isPublic = false,
  refreshKey?: number | string,
) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(!!path);
  const [version, setVersion] = useState(0);
  const reload = useCallback(() => setVersion((v) => v + 1), []);
  useEffect(() => {
    if (!path) return;
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    api<T>(path, { signal: controller.signal, public: isPublic })
      .then((value) => {
        if (!controller.signal.aborted) setData(value);
      })
      .catch((error) => {
        if (!controller.signal.aborted) setError(error);
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [path, isPublic, version, refreshKey]);
  return { data, error, loading, reload, setData };
}
