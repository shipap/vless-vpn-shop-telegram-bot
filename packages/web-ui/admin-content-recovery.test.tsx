import { expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { PublicContentAdmin } from "../../apps/web/dashboard/admin-v6-content";
import { Editor } from "../../apps/web/dashboard/admin-editor";

const fixture = vi.hoisted(() => ({ data: {} as unknown }));
vi.mock("@api/client", () => ({
  useApi: () => ({ data: fixture.data, error: null, loading: false }),
  api: vi.fn(),
}));

it("opens missing FAQ collections without changing or submitting stored content", () => {
  fixture.data = { catalog_description: "Существующее вступление" };
  const mutate = vi.fn();
  const html = renderToStaticMarkup(
    <PublicContentAdmin mutate={mutate} refreshVersion={0} />,
  );
  expect(html).toContain("Добавить вопрос");
  expect(html).toContain("Добавить шаг подключения");
  expect(fixture.data).toEqual({
    catalog_description: "Существующее вступление",
  });
  expect(mutate).not.toHaveBeenCalled();
});

it.each([
  [[]],
  "legacy content",
  42,
  null,
  { faq: null },
  { how_to_connect: {} },
  { faq: [null] },
  {
    faq: [
      { id: "q", question: "Вопрос", answer: [{ type: "text", text: {} }] },
    ],
  },
  { how_to_connect: [{ id: "s", title: "Шаг", body: [null] }] },
])(
  "keeps invalid stored collections visible as a local error without offering replacement save: %j",
  (data) => {
    fixture.data = data;
    const before = JSON.stringify(data);
    const html = renderToStaticMarkup(
      <PublicContentAdmin mutate={() => {}} refreshVersion={0} />,
    );
    expect(html).toContain('role="alert"');
    expect(html).not.toContain('type="submit"');
    expect(JSON.stringify(fixture.data)).toBe(before);
  },
);

it("retains valid saved questions and formatted instructions in editable fields", () => {
  fixture.data = {
    faq: [
      {
        id: "q",
        question: "Мой вопрос",
        answer: [{ type: "text", text: "Мой ответ" }],
      },
    ],
    how_to_connect: [
      {
        id: "s",
        title: "Мой шаг",
        body: [
          {
            type: "bold",
            children: [{ type: "text", text: "Моя инструкция" }],
          },
        ],
      },
    ],
    catalog_description: "Отдельное вступление",
  };
  const before = JSON.stringify(fixture.data);
  const html = renderToStaticMarkup(
    <PublicContentAdmin mutate={() => {}} refreshVersion={0} />,
  );
  expect(html).toContain('value="Мой вопрос"');
  expect(html).toContain("Мой ответ");
  expect(html).toContain('value="Мой шаг"');
  expect(html).toContain("<strong>Моя инструкция</strong>");
  expect(JSON.stringify(fixture.data)).toBe(before);
});

it.each([[null], [{ type: "text", text: { invalid: true } }]])(
  "keeps malformed raw content editable without crashing its preview: %j",
  (node) => {
    const nodes = [node];
    const html = renderToStaticMarkup(
      <Editor
        section="content"
        row={{ key: "intro", nodes }}
        onClose={() => {}}
        onSave={() => {}}
      />,
    );
    expect(html).toContain('class="code-editor"');
    expect(html).toContain('role="alert"');
    expect(html).toContain("textarea");
    expect(html).toContain(node === null ? "null" : "&quot;invalid&quot;");
  },
);
