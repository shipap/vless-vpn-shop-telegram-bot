import { describe, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { Editor } from "./admin-editor";
import { OperationalControls } from "./admin-operations";

vi.mock("@api/client", () => ({
  useApi: () => ({ data: null, error: null, loading: false }),
  api: vi.fn(),
}));
describe("broadcast attachment entry", () => {
  it.each(["create", "preview"])(
    "offers one actual photo/video file in the %s composer",
    (mode) => {
      const html = renderToStaticMarkup(
        mode === "create" ? (
          <Editor
            section="broadcasts"
            row={{}}
            onClose={() => {}}
            onSave={() => {}}
          />
        ) : (
          <OperationalControls
            section="broadcasts"
            mutate={() => {}}
            onView={() => {}}
            refreshVersion={0}
          />
        ),
      );
      expect(html).toContain('type="file"');
      expect(html).toContain('accept="image/jpeg,image/png,video/mp4"');
      expect(html).not.toContain("multiple=");
      expect(html).toContain("Фото JPEG/PNG — до 2 МБ");
      expect(html).toContain("видео MP4 — до 8 МБ");
      expect(html).toContain("В email файл придёт вложением");
    },
  );
});
