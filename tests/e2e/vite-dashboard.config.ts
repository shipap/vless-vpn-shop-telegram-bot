import { webConfig } from "../../apps/web/vite.shared.ts";

export default webConfig(false, {
  backend: "http://127.0.0.1:8150",
  devPort: 3210,
  previewPort: 3211,
});
