import { useEffect, useState } from "react";
import "./telegram.css";

type Insets = { top?: number; right?: number; bottom?: number; left?: number };
export type TelegramWebApp = {
  initData: string;
  safeAreaInset?: Insets;
  contentSafeAreaInset?: Insets;
  viewportStableHeight?: number;
  ready: () => void;
  expand: () => void;
  onEvent: (event: string, callback: () => void) => void;
  offEvent: (event: string, callback: () => void) => void;
};
declare global {
  interface Window {
    Telegram?: { WebApp?: TelegramWebApp };
  }
}
const initialized = new WeakSet<TelegramWebApp>();

export function useTelegramWebApp() {
  const [webApp] = useState(() => {
    const candidate = window.Telegram?.WebApp;
    return candidate?.initData ? candidate : null;
  });
  const [activation, setActivation] = useState(0);
  useEffect(() => {
    if (!webApp) return;
    const root = document.documentElement;
    document.body.dataset.telegramMiniApp = "true";
    const update = () => {
      for (const side of ["top", "right", "bottom", "left"] as const) {
        for (const [kind, insets] of [
          ["device", webApp.safeAreaInset],
          ["content", webApp.contentSafeAreaInset],
        ] as const) {
          const value = insets?.[side];
          root.style.setProperty(
            `--telegram-${kind}-safe-${side}`,
            `${typeof value === "number" && Number.isFinite(value) ? Math.max(0, value) : 0}px`,
          );
        }
      }
      if (
        webApp.viewportStableHeight &&
        Number.isFinite(webApp.viewportStableHeight)
      )
        root.style.setProperty(
          "--telegram-viewport-height",
          `${webApp.viewportStableHeight}px`,
        );
    };
    const activate = () => setActivation((value) => value + 1);
    update();
    if (!initialized.has(webApp)) {
      webApp.ready();
      webApp.expand();
      initialized.add(webApp);
    }
    for (const event of [
      "safeAreaChanged",
      "contentSafeAreaChanged",
      "viewportChanged",
    ])
      webApp.onEvent(event, update);
    webApp.onEvent("activated", activate);
    return () => {
      for (const event of [
        "safeAreaChanged",
        "contentSafeAreaChanged",
        "viewportChanged",
      ])
        webApp.offEvent(event, update);
      webApp.offEvent("activated", activate);
      delete document.body.dataset.telegramMiniApp;
    };
  }, [webApp]);
  return {
    initData: webApp?.initData ?? "",
    activation,
    isMiniApp: Boolean(webApp),
  };
}
