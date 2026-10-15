import { webConfig } from "../../apps/web/vite.shared.ts";

export default webConfig(true, {
  backend: "http://127.0.0.1:8150",
  devPort: 3110,
  previewPort: 3111,
});
