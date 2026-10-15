import { createRoot } from "react-dom/client";
import { Landing } from "./Landing";
import { InstanceProvider } from "@ui/instance";
import { useTelegramWebApp } from "@ui/telegram";
import "@fontsource-variable/manrope";
import "@ui/styles.css";
import "./landing.css";

function App() {
  useTelegramWebApp();
  return <Landing />;
}
createRoot(document.getElementById("root")!).render(
  <InstanceProvider surface="landing">
    <App />
  </InstanceProvider>,
);
