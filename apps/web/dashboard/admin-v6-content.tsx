import { useState } from "react";
import { useApi } from "@api/client";
import { Button, ErrorState, Loading } from "@ui/components";
import {
  SemanticContent,
  validatedContentNodes,
  type ContentNode,
} from "@ui/content";
import type { Mutation } from "./admin";
import { ReviewChange } from "./admin-v6";
import { adminSaveCopy } from "./admin-save-copy";
import { addPracticalFaq } from "@ui/practical-faq";

export function StructuredEditor({
  label,
  nodes,
  onChange,
  depth = 0,
}: {
  label: string;
  nodes: ContentNode[];
  onChange: (nodes: ContentNode[]) => void;
  depth?: number;
}) {
  const set = (index: number, field: string, value: unknown) =>
    onChange(
      nodes.map((node, i) =>
        i === index ? { ...node, [field]: value } : node,
      ),
    );
  return (
    <fieldset className="v6-structured">
      <legend>{label}</legend>
      {nodes.map((node, index) => (
        <div className="v6-content-node" key={index}>
          <label>
            Элемент {index + 1}
            <select
              value={node.type}
              onChange={(e) => {
                const type = e.target.value;
                onChange(
                  nodes.map((n, i) =>
                    i === index
                      ? type === "text"
                        ? { type, text: "" }
                        : type === "emoji"
                          ? { type, id: "", fallback: "☀", label: "Значок" }
                          : { type, children: [{ type: "text", text: "" }] }
                      : n,
                  ),
                );
              }}
            >
              <option value="text">Текст</option>
              <option value="paragraph">Абзац</option>
              <option value="bold">Жирный</option>
              <option value="italic">Курсив</option>
              <option value="link">Ссылка</option>
              <option value="emoji">Эмодзи</option>
              {![
                "text",
                "paragraph",
                "bold",
                "italic",
                "link",
                "emoji",
              ].includes(node.type) && (
                <option value={node.type}>Существующее оформление</option>
              )}
            </select>
          </label>
          {node.type === "text" && (
            <label>
              Текст элемента {index + 1}
              <textarea
                required
                value={node.text || ""}
                onChange={(e) => set(index, "text", e.target.value)}
              />
            </label>
          )}
          {node.type === "link" && (
            <label>
              Адрес ссылки
              <input
                type="url"
                required
                pattern="https://.*"
                value={node.href || ""}
                onChange={(e) => set(index, "href", e.target.value)}
              />
            </label>
          )}
          {node.type === "emoji" && (
            <div className="field-grid">
              <label>
                ID эмодзи
                <input
                  required
                  pattern="[0-9]{1,32}"
                  inputMode="numeric"
                  value={node.id || ""}
                  onChange={(e) => set(index, "id", e.target.value)}
                />
              </label>
              <label>
                Запасной символ
                <input
                  required
                  maxLength={16}
                  value={node.fallback || ""}
                  onChange={(e) => set(index, "fallback", e.target.value)}
                />
              </label>
              <label>
                Значение значка
                <input
                  required
                  value={node.label || ""}
                  onChange={(e) => set(index, "label", e.target.value)}
                />
              </label>
            </div>
          )}
          {node.children && depth < 8 && (
            <StructuredEditor
              label="Содержимое элемента"
              nodes={node.children}
              depth={depth + 1}
              onChange={(value) => set(index, "children", value)}
            />
          )}
          {node.items?.map((item, i) => (
            <StructuredEditor
              key={i}
              label={`Пункт ${i + 1}`}
              nodes={item}
              depth={depth + 1}
              onChange={(value) =>
                set(
                  index,
                  "items",
                  node.items!.map((v, j) => (j === i ? value : v)),
                )
              }
            />
          ))}
          <Button
            type="button"
            tone="ghost"
            onClick={() => onChange(nodes.filter((_, i) => i !== index))}
          >
            Удалить элемент {index + 1}
          </Button>
        </div>
      ))}
      <Button
        tone="secondary"
        type="button"
        onClick={() => onChange([...nodes, { type: "text", text: "" }])}
      >
        Добавить текст
      </Button>
      <div className="content-preview">
        <SemanticContent nodes={nodes} />
      </div>
    </fieldset>
  );
}
type Faq = { id: string; question: string; answer: ContentNode[] };
type Step = { id: string; title: string; body: ContentNode[] };
type Content = { faq: Faq[]; how_to_connect: Step[] };

function editablePublicContent(value: unknown): Content | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const data = value as Record<string, unknown>;
  const valid = (value: unknown, title: string, body: string) =>
    Array.isArray(value) &&
    value.every(
      (item) =>
        item &&
        typeof item === "object" &&
        typeof item.id === "string" &&
        typeof item[title] === "string" &&
        Array.isArray(item[body]) &&
        (item[body].length === 0 || validatedContentNodes(item[body]) !== null),
    );
  const faq = data.faq === undefined ? [] : data.faq;
  const steps = data.how_to_connect === undefined ? [] : data.how_to_connect;
  return valid(faq, "question", "answer") && valid(steps, "title", "body")
    ? { faq: faq as Faq[], how_to_connect: steps as Step[] }
    : null;
}

export function PublicContentAdmin({
  mutate,
  refreshVersion,
}: {
  mutate: (value: Mutation) => void;
  refreshVersion: number;
}) {
  const resource = useApi<unknown>(
    "/admin/public-content",
    false,
    refreshVersion,
  );
  if (resource.error)
    return <ErrorState error={resource.error} onRetry={resource.reload} />;
  if (resource.loading) return <Loading />;
  const editable = editablePublicContent(resource.data);
  if (!editable)
    return (
      <p className="notice warning" role="alert">
        Сохранённые FAQ или инструкции имеют неподдерживаемую структуру.
        Исходные данные сохранены; редактирование этого раздела недоступно до
        проверки данных.
      </p>
    );
  return (
    <ContentForm
      key={JSON.stringify(resource.data)}
      data={editable}
      mutate={mutate}
    />
  );
}
function ContentForm({
  data,
  mutate,
}: {
  data: Content;
  mutate: (value: Mutation) => void;
}) {
  const editableContent: Content = {
    faq: data.faq,
    how_to_connect: data.how_to_connect,
  };
  const [content, setContent] = useState(editableContent);
  return (
    <ReviewChange
      requireReason={false}
      title={adminSaveCopy.publicContent}
      path="/admin/public-content"
      body={content}
      before={editableContent}
      mutate={mutate}
    >
      <h2>FAQ и подключение</h2>
      <p>
        Эти материалы используются общим каталогом сайта и бота. Структура и
        строковые ID эмодзи сохраняются.
      </p>
      <Button
        type="button"
        tone="secondary"
        disabled={content.faq.length >= 30}
        onClick={() =>
          setContent((current) => ({
            ...current,
            faq: addPracticalFaq(current.faq),
          }))
        }
      >
        Добавить практические ответы
      </Button>
      <p className="fine-print">
        Добавляются только отсутствующие вопросы. Проверьте ответы и сохраните
        изменения; опубликованные ответы не заменяются.
      </p>
      {content.faq.map((item, index) => (
        <fieldset key={item.id}>
          <legend>Вопрос {index + 1}</legend>
          <label>
            Вопрос {index + 1}
            <input
              required
              value={item.question}
              onChange={(e) =>
                setContent((c) => ({
                  ...c,
                  faq: c.faq.map((v, i) =>
                    i === index ? { ...v, question: e.target.value } : v,
                  ),
                }))
              }
            />
          </label>
          <StructuredEditor
            label={`Ответ ${index + 1}`}
            nodes={item.answer}
            onChange={(answer) =>
              setContent((c) => ({
                ...c,
                faq: c.faq.map((v, i) => (i === index ? { ...v, answer } : v)),
              }))
            }
          />
          <Button
            type="button"
            tone="ghost"
            onClick={() =>
              setContent((c) => ({
                ...c,
                faq: c.faq.filter((_, i) => i !== index),
              }))
            }
          >
            Удалить вопрос {index + 1}
          </Button>
        </fieldset>
      ))}
      <Button
        tone="secondary"
        type="button"
        onClick={() =>
          setContent((c) => ({
            ...c,
            faq: [
              ...c.faq,
              { id: crypto.randomUUID(), question: "", answer: [] },
            ],
          }))
        }
      >
        Добавить вопрос
      </Button>
      {content.how_to_connect.map((item, index) => (
        <fieldset key={item.id}>
          <legend>Шаг подключения {index + 1}</legend>
          <label>
            Заголовок шага {index + 1}
            <input
              required
              value={item.title}
              onChange={(e) =>
                setContent((c) => ({
                  ...c,
                  how_to_connect: c.how_to_connect.map((v, i) =>
                    i === index ? { ...v, title: e.target.value } : v,
                  ),
                }))
              }
            />
          </label>
          <StructuredEditor
            label={`Инструкция ${index + 1}`}
            nodes={item.body}
            onChange={(body) =>
              setContent((c) => ({
                ...c,
                how_to_connect: c.how_to_connect.map((v, i) =>
                  i === index ? { ...v, body } : v,
                ),
              }))
            }
          />
          <Button
            type="button"
            tone="ghost"
            onClick={() =>
              setContent((c) => ({
                ...c,
                how_to_connect: c.how_to_connect.filter((_, i) => i !== index),
              }))
            }
          >
            Удалить шаг {index + 1}
          </Button>
        </fieldset>
      ))}
      <Button
        tone="secondary"
        type="button"
        onClick={() =>
          setContent((c) => ({
            ...c,
            how_to_connect: [
              ...c.how_to_connect,
              { id: crypto.randomUUID(), title: "", body: [] },
            ],
          }))
        }
      >
        Добавить шаг подключения
      </Button>
    </ReviewChange>
  );
}
