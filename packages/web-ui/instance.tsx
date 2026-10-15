import {
  createContext,
  useContext,
  useEffect,
  useState,
  type ReactNode,
} from "react";
import { api } from "@api/client";
import type { PublicInstance } from "@api/types";
import { searchRobots } from "./search-indexing";
import { instanceTerminology } from "./terminology";
export { neutralInstance, publicInstance } from "./instance-value";
import {
  neutralInstance,
  publicInstance,
  accentColors,
} from "./instance-value";
const InstanceContext = createContext<PublicInstance>(neutralInstance);
declare global {
  interface Window {
    __PUBLIC_INSTANCE__?: unknown;
    __SEARCH_INDEXING_TEST_ENVIRONMENT__?: boolean;
  }
}
export function useInstance() {
  return useContext(InstanceContext);
}
export function InstanceProvider({
  children,
  surface,
}: {
  children: ReactNode;
  surface: "landing" | "dashboard";
}) {
  const [value, setValue] = useState(() =>
    publicInstance(
      typeof window !== "undefined" ? window.__PUBLIC_INSTANCE__ : null,
    ),
  );
  useEffect(() => {
    const request = new AbortController();
    api<{ instance?: unknown }>("/public-config", {
      public: true,
      signal: request.signal,
    })
      .then((config) => {
        if (!request.signal.aborted) setValue(publicInstance(config.instance));
      })
      .catch(() => {
        if (!request.signal.aborted) setValue(neutralInstance);
      });
    return () => request.abort();
  }, []);
  useEffect(() => {
    document.documentElement.dataset.visualProfile = value.visual_profile;
    document.documentElement.style.setProperty(
      "--instance-accent",
      value.accent_color,
    );
    const accent = accentColors(value.accent_color);
    document.documentElement.style.setProperty(
      "--instance-on-accent",
      accent.onAccent,
    );
    document.documentElement.style.setProperty(
      "--instance-accent-text",
      accent.textAccent,
    );
    document.title = `${location.pathname.startsWith("/admin") ? "Управление" : surface === "landing" ? instanceTerminology(value).service_summary : "Личный кабинет"} — ${value.app_name}`;
    const description = document.querySelector<HTMLMetaElement>(
      'meta[name="description"]',
    );
    if (description) description.content = value.app_description;
    const robots =
      document.querySelector<HTMLMetaElement>('meta[name="robots"]') ??
      document.createElement("meta");
    robots.name = "robots";
    robots.content = searchRobots(
      value,
      surface === "landing",
      location.href,
      window.__SEARCH_INDEXING_TEST_ENVIRONMENT__ !== false,
    );
    if (!robots.parentNode) document.head.append(robots);
    const updateMeta = (property: string, content: string | null) => {
      const existing = document.querySelector<HTMLMetaElement>(
        `meta[property="${property}"]`,
      );
      if (content === null) {
        existing?.remove();
        return;
      }
      const element = existing ?? document.createElement("meta");
      element.setAttribute("property", property);
      element.content = content;
      if (!existing) document.head.append(element);
    };
    updateMeta("og:title", document.title);
    updateMeta("og:description", value.app_description);
    updateMeta("og:image", value.og_image_url);
    const icon = document.querySelector<HTMLLinkElement>('link[rel="icon"]');
    if (value.favicon_url) {
      const element = icon ?? document.createElement("link");
      element.rel = "icon";
      element.href = value.favicon_url;
      if (!icon) document.head.append(element);
    } else icon?.remove();
  }, [value, surface]);
  return (
    <InstanceContext.Provider value={value}>
      {children}
    </InstanceContext.Provider>
  );
}
