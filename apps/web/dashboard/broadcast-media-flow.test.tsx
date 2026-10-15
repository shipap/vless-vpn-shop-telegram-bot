import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  BroadcastAttachment,
  Editor,
  type BroadcastMediaRef,
} from "./admin-editor";
import { OperationalControls } from "./admin-operations";

const harness = vi.hoisted(() => ({
  slots: [] as unknown[],
  cursor: 0,
  refs: [] as { current: unknown }[],
  refCursor: 0,
  api: vi.fn(),
}));
vi.mock("react", async (original) => ({
  ...(await original<typeof import("react")>()),
  useState: (initial: unknown) => {
    const slot = harness.cursor++;
    if (!(slot in harness.slots))
      harness.slots[slot] = typeof initial === "function" ? initial() : initial;
    return [
      harness.slots[slot],
      (value: unknown) => {
        harness.slots[slot] =
          typeof value === "function" ? value(harness.slots[slot]) : value;
      },
    ];
  },
  useRef: (initial: unknown) =>
    harness.refs[harness.refCursor++] ??
    (harness.refs[harness.refCursor - 1] = { current: initial }),
  useEffect: () => {},
  useId: () => "synthetic-file-input",
}));
vi.mock("@api/client", () => ({
  api: harness.api,
  useApi: () => ({ data: null, error: null, loading: false }),
}));
type Element = {
  type: string | ((props: Record<string, unknown>) => unknown);
  props: Record<string, any>;
};
function find(
  value: unknown,
  match: (value: Element) => boolean,
): Element | null {
  if (Array.isArray(value)) {
    for (const item of value) {
      const result = find(item, match);
      if (result) return result;
    }
    return null;
  }
  if (!value || typeof value !== "object" || !("props" in value)) return null;
  const element = value as Element;
  return match(element) ? element : find(element.props.children, match);
}
const ref: BroadcastMediaRef = {
  asset_id: "a".repeat(32),
  sha256: "b".repeat(64),
  proof: "c".repeat(64),
  mime: "image/png",
  size_bytes: 3,
};
const file = () =>
  Object.assign(new Blob([new Uint8Array([1, 2, 3])], { type: "image/png" }), {
    name: "example.png",
  }) as File;
function draw(run: () => unknown) {
  harness.cursor = 0;
  harness.refCursor = 0;
  return run();
}
beforeEach(() => {
  harness.slots = [];
  harness.refs = [];
  harness.api.mockReset();
});
async function select(tree: unknown, value: File) {
  find(
    tree,
    (e) => e.type === "input" && e.props.type === "file",
  )!.props.onChange({ target: { files: [value], value: "selected" } });
  for (let i = 0; i < 5; i++) await Promise.resolve();
}
describe("actual broadcast file selection", () => {
  it("uploads raw bytes, stores only the signed five-field reference and removes it", async () => {
    harness.api.mockResolvedValue({
      ...ref,
      secret_url: "https://foreign.invalid",
    });
    const onChange = vi.fn(),
      onBlockedChange = vi.fn();
    const props = { value: null, onChange, onBlockedChange };
    const selected = file();
    await select(
      draw(() => BroadcastAttachment(props)),
      selected,
    );
    expect(harness.api).toHaveBeenCalledWith(
      "/admin/broadcasts/media",
      expect.objectContaining({
        method: "POST",
        rawBody: selected,
        signal: expect.any(AbortSignal),
      }),
    );
    expect(onChange.mock.calls).toEqual([[null], [ref]]);
    expect(onBlockedChange.mock.calls).toEqual([[true], [false]]);
    const tree = draw(() => BroadcastAttachment({ ...props, value: ref }));
    find(
      tree,
      (e) => typeof e.type === "function" && e.props.children === "Убрать файл",
    )!.props.onClick();
    expect(onChange).toHaveBeenLastCalledWith(null);
    expect(onBlockedChange).toHaveBeenLastCalledWith(false);
  });
  it.each([
    new Blob(["x"], { type: "text/html" }),
    new Blob([new Uint8Array(2_000_001)], { type: "image/jpeg" }),
    new Blob([new Uint8Array(8_000_001)], { type: "video/mp4" }),
    new Blob([], { type: "video/mp4" }),
  ])(
    "refuses unsupported, oversize or empty files before upload",
    async (blob) => {
      const onBlockedChange = vi.fn();
      await select(
        draw(() =>
          BroadcastAttachment({
            value: null,
            onChange: vi.fn(),
            onBlockedChange,
          }),
        ),
        Object.assign(blob, { name: "invalid" }) as File,
      );
      expect(harness.api).not.toHaveBeenCalled();
      expect(onBlockedChange).toHaveBeenLastCalledWith(true);
    },
  );
  it.each([
    { ...ref, proof: "bad" },
    { ...ref, mime: "text/html" },
    { ...ref, size_bytes: 2_000_001 },
    { ...ref, size_bytes: 0 },
    { ...ref, size_bytes: 1.5 },
    { ...ref, size_bytes: "3" },
    { ...ref, sha256: [ref.sha256] },
  ])(
    "holds malformed server attachment until removal or successful retry",
    async (result) => {
      harness.api.mockResolvedValue(result);
      const onChange = vi.fn(),
        onBlockedChange = vi.fn();
      await select(
        draw(() =>
          BroadcastAttachment({ value: null, onChange, onBlockedChange }),
        ),
        file(),
      );
      expect(onChange.mock.calls).toEqual([[null]]);
      expect(onBlockedChange).toHaveBeenLastCalledWith(true);
    },
  );
  it("aborts a removed in-flight file and ignores its delayed success", async () => {
    let resolve!: (value: unknown) => void;
    harness.api.mockImplementation(
      () =>
        new Promise((done) => {
          resolve = done;
        }),
    );
    const onChange = vi.fn(),
      onBlockedChange = vi.fn(),
      props = { value: null, onChange, onBlockedChange };
    await select(
      draw(() => BroadcastAttachment(props)),
      file(),
    );
    const signal = harness.api.mock.calls[0]![1].signal as AbortSignal;
    find(
      draw(() => BroadcastAttachment(props)),
      (e) => typeof e.type === "function" && e.props.children === "Убрать файл",
    )!.props.onClick();
    expect(signal.aborted).toBe(true);
    resolve(ref);
    for (let i = 0; i < 5; i++) await Promise.resolve();
    expect(onChange.mock.calls).toEqual([[null], [null]]);
    expect(onBlockedChange).toHaveBeenLastCalledWith(false);
  });
  it.each(["image/jpeg", "image/png", "video/mp4"])(
    "accepts the exact decimal upload cap for %s",
    async (mime) => {
      const size = mime === "video/mp4" ? 8_000_000 : 2_000_000;
      const result = { ...ref, mime, size_bytes: size };
      harness.api.mockResolvedValue(result);
      const onChange = vi.fn();
      await select(
        draw(() =>
          BroadcastAttachment({
            value: null,
            onChange,
            onBlockedChange: vi.fn(),
          }),
        ),
        Object.assign(new Blob([new Uint8Array(size)], { type: mime }), {
          name: "boundary",
        }) as File,
      );
      expect(onChange).toHaveBeenLastCalledWith(result);
    },
  );
  it.each([
    ["create", "image/png", 143, 105],
    ["preview", "image/png", 143, 105],
    ["create", "image/jpeg", 681, 645],
    ["preview", "image/jpeg", 681, 645],
  ])(
    "accepts canonical %s/%s size and submits only server Ref",
    async (mode, mime, rawSize, canonicalSize) => {
      const canonical = { ...ref, mime, size_bytes: canonicalSize };
      harness.api.mockResolvedValue(canonical);
      const accepted = vi.fn(),
        onBlockedChange = vi.fn();
      await select(
        draw(() =>
          BroadcastAttachment({
            value: null,
            onChange: accepted,
            onBlockedChange,
          }),
        ),
        Object.assign(
          new Blob([new Uint8Array(rawSize as number)], {
            type: mime as string,
          }),
          { name: "image" },
        ) as File,
      );
      expect(accepted).toHaveBeenLastCalledWith(canonical);
      expect(onBlockedChange).toHaveBeenLastCalledWith(false);
      harness.slots = [];
      harness.refs = [];
      const save = vi.fn();
      const run =
        mode === "create"
          ? () =>
              Editor({
                section: "broadcasts",
                row: {},
                onClose: () => {},
                onSave: save,
              })
          : () => {
              const e = OperationalControls({
                section: "broadcasts",
                mutate: save,
                onView: () => {},
                refreshVersion: 0,
              }) as Element;
              return (e.type as Function)(e.props);
            };
      let tree = draw(run);
      find(tree, (e) => e.type === "textarea")!.props.onChange({
        target: { value: "Серверное вложение" },
      });
      find(tree, (e) => e.type === BroadcastAttachment)!.props.onChange(
        accepted.mock.calls.at(-1)![0],
      );
      tree = draw(run);
      await find(tree, (e) => e.type === "form")!.props.onSubmit({
        preventDefault: () => {},
      });
      expect(harness.api).toHaveBeenLastCalledWith(
        "/admin/broadcasts/estimate",
        expect.objectContaining({
          body: {
            nodes: [
              {
                type: "paragraph",
                children: [{ type: "text", text: "Серверное вложение" }],
              },
            ],
            media: canonical,
          },
        }),
      );
      if (mode === "create")
        expect(save.mock.calls[0]![0].body.media).toEqual(canonical);
    },
  );
  it.each(["create", "preview"])(
    "includes the signed attachment separately from text in %s and blocks unfinished uploads",
    async (mode) => {
      harness.api.mockResolvedValue({});
      const save = vi.fn();
      const run =
        mode === "create"
          ? () =>
              Editor({
                section: "broadcasts",
                row: {},
                onClose: () => {},
                onSave: save,
              })
          : () => {
              const e = OperationalControls({
                section: "broadcasts",
                mutate: save,
                onView: () => {},
                refreshVersion: 0,
              }) as Element;
              return (e.type as Function)(e.props);
            };
      let tree = draw(run);
      find(tree, (e) => e.type === "textarea")!.props.onChange({
        target: { value: "Сообщение" },
      });
      const attachment = find(tree, (e) => e.type === BroadcastAttachment)!;
      attachment.props.onChange(ref);
      attachment.props.onBlockedChange(true);
      tree = draw(run);
      await find(tree, (e) => e.type === "form")!.props.onSubmit({
        preventDefault: () => {},
      });
      expect(harness.api).not.toHaveBeenCalled();
      expect(save).not.toHaveBeenCalled();
      attachment.props.onBlockedChange(false);
      tree = draw(run);
      await find(tree, (e) => e.type === "form")!.props.onSubmit({
        preventDefault: () => {},
      });
      expect(harness.api).toHaveBeenLastCalledWith(
        "/admin/broadcasts/estimate",
        expect.objectContaining({
          body: {
            nodes: [
              {
                type: "paragraph",
                children: [{ type: "text", text: "Сообщение" }],
              },
            ],
            media: ref,
          },
        }),
      );
      if (mode === "create")
        expect(save.mock.calls[0]![0].body.media).toEqual(ref);
      else {
        find(
          tree,
          (e) =>
            typeof e.type === "function" &&
            e.props.children === "Отправить тест себе",
        )!.props.onClick();
        expect(save.mock.calls[0]![0]).toMatchObject({
          path: "/admin/broadcasts/test-self",
          body: { media: ref },
        });
      }
    },
  );
});
