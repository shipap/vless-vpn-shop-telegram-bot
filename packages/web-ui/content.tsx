import { messages } from "./messages";
import { Fragment, type ReactNode } from "react";
import { CustomEmoji } from "./emoji";

export function safeUrl(value: string | null | undefined): string | undefined {
  if (!value || /[\u0000-\u0020\\]/.test(value) || value.startsWith("//"))
    return undefined;
  if (value.startsWith("/")) return value;
  try {
    const url = new URL(value);
    if (url.username || url.password) return undefined;
    if (
      url.protocol === "https:" ||
      (url.protocol === "http:" &&
        ["localhost", "127.0.0.1", "[::1]"].includes(url.hostname))
    )
      return url.href;
  } catch {
    /* Непроверенная ссылка не становится интерактивной. */
  }
  return undefined;
}

export function money(
  amount: string | null | undefined,
  currency = "RUB",
): string {
  if (amount == null || !/^-?\d+(\.\d+)?$/.test(amount))
    return messages.m6d8e839d9f;
  const [whole, fraction] = amount.split(".");
  const grouped = BigInt(whole).toLocaleString("ru-RU");
  const fractionalMinus =
    whole === "-0" && fraction && !/^0+$/.test(fraction) ? "-" : "";
  return `${fractionalMinus}${grouped}${fraction && !/^0+$/.test(fraction) ? "," + fraction : ""} ${currency === "RUB" ? "₽" : currency}`;
}

export function date(
  value: string | null | undefined,
  time = false,
  timeZone = "Europe/Moscow",
) {
  if (!value) return messages.mf16cbd3270;
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return messages.mf16cbd3270;
  return new Intl.DateTimeFormat("ru-RU", {
    day: "numeric",
    month: "long",
    year: "numeric",
    timeZone,
    ...(time
      ? ({
          hour: "2-digit",
          minute: "2-digit",
        } as const)
      : {}),
  }).format(parsed);
}

export type ContentNode = {
  type: string;
  text?: string;
  children?: ContentNode[];
  href?: string;
  id?: string;
  fallback?: string;
  label?: string;
  items?: ContentNode[][];
  rows?: ContentNode[][][];
  title?: string;
  level?: number;
  ordered?: boolean;
};

export function validatedContentNodes(value: unknown): ContentNode[] | null {
  let remaining = 1500;
  const valid = (nodes: unknown, depth: number): boolean =>
    depth <= 12 &&
    Array.isArray(nodes) &&
    nodes.length <= 500 &&
    nodes.every((node) => {
      if (
        --remaining < 0 ||
        !node ||
        typeof node !== "object" ||
        Array.isArray(node)
      )
        return false;
      const item = node as Record<string, unknown>;
      if (
        typeof item.type !== "string" ||
        ![
          "text",
          "bold",
          "strong",
          "italic",
          "emphasis",
          "underline",
          "code",
          "br",
          "paragraph",
          "heading",
          "link",
          "emoji",
          "quote",
          "details",
          "list",
          "table",
        ].includes(item.type)
      )
        return false;
      for (const key of ["text", "href", "id", "fallback", "label", "title"])
        if (item[key] !== undefined && typeof item[key] !== "string")
          return false;
      if (item.level !== undefined && typeof item.level !== "number")
        return false;
      if (item.ordered !== undefined && typeof item.ordered !== "boolean")
        return false;
      if (item.children !== undefined && !valid(item.children, depth + 1))
        return false;
      if (
        item.items !== undefined &&
        (!Array.isArray(item.items) ||
          item.items.length > 500 ||
          !item.items.every((row) => valid(row, depth + 1)))
      )
        return false;
      if (
        item.rows !== undefined &&
        (!Array.isArray(item.rows) ||
          item.rows.length > 500 ||
          !item.rows.every(
            (row) =>
              Array.isArray(row) &&
              row.length <= 500 &&
              row.every((cell) => valid(cell, depth + 1)),
          ))
      )
        return false;
      return true;
    });
  return valid(value, 0) && (value as ContentNode[]).length
    ? (value as ContentNode[])
    : null;
}

export function ContentOrText({
  nodes,
  text,
}: {
  nodes: unknown;
  text: string;
}) {
  const validated = validatedContentNodes(nodes);
  return validated ? (
    <SemanticContent nodes={validated} />
  ) : (
    <p>{plainTelegramText(text)}</p>
  );
}

export function plainTelegramText(text: string | null | undefined): string {
  return (text ?? "").replace(
    /<\/?(?:tg-emoji|tg-spoiler|b|strong|i|em|u|ins|s|strike|del|code|pre|a|blockquote)(?:\s[^>]*)?>/gi,
    "",
  );
}

const catalogGraphemes = new Intl.Segmenter("ru", { granularity: "grapheme" });

export function catalogText(text: string): string {
  return Array.from(
    catalogGraphemes.segment(plainTelegramText(text)),
    ({ segment }) =>
      /^\p{Regional_Indicator}{2}$/u.test(segment) ||
      ["©", "®", "™"].includes(segment) ||
      !/[\p{Extended_Pictographic}\p{Emoji_Presentation}\p{Emoji_Modifier}\u20e3★☆✦✧◆◇◈]/u.test(
        segment,
      )
        ? segment
        : "",
  ).join("");
}

function catalogNodes(nodes: ContentNode[]): ContentNode[] {
  return nodes.map((node) =>
    node.type === "emoji"
      ? {
          type: "text",
          text: (
            node.fallback?.match(/\p{Regional_Indicator}{2}/gu) ?? []
          ).join(""),
        }
      : {
          ...node,
          text: node.text === undefined ? undefined : catalogText(node.text),
          title: node.title === undefined ? undefined : catalogText(node.title),
          children: node.children && catalogNodes(node.children),
          items: node.items?.map(catalogNodes),
          rows: node.rows?.map((row) => row.map(catalogNodes)),
        },
  );
}

export function CatalogDescription({
  nodes,
  text,
  className = "",
}: {
  nodes: unknown;
  text: string;
  className?: string;
}) {
  const raw = validatedContentNodes(nodes);
  const validated = raw && catalogNodes(raw);
  const visible = (node: ContentNode): boolean => {
    switch (node.type) {
      case "text":
        return Boolean(node.text?.trim());
      case "emoji":
        return Boolean(
          node.fallback?.trim() || (node.id && /^[0-9]{1,32}$/.test(node.id)),
        );
      case "br":
        return false;
      case "list":
        return Boolean(node.items?.some((item) => item.some(visible)));
      case "table":
        return Boolean(
          node.rows?.some((row) => row.some((cell) => cell.some(visible))),
        );
      case "details":
        return Boolean(node.title?.trim() || node.children?.some(visible));
      default:
        return Boolean(node.children?.some(visible));
    }
  };
  return (
    <div className={`catalog-description ${className}`.trim()}>
      {validated?.some(visible) ? (
        <SemanticContent nodes={validated} />
      ) : (
        <p>{catalogText(plainTelegramText(text))}</p>
      )}
    </div>
  );
}

export function SemanticContent({
  nodes,
  depth = 0,
}: {
  nodes: ContentNode[];
  depth?: number;
}) {
  if (depth > 12 || !Array.isArray(nodes)) return null;
  return (
    <>
      {nodes.slice(0, 500).map((node, index) => {
        const child = (
          <SemanticContent nodes={node.children ?? []} depth={depth + 1} />
        );
        let rendered: ReactNode;
        switch (node.type) {
          case "text":
            rendered = node.text;
            break;
          case "bold":
          case "strong":
            rendered = <strong>{child}</strong>;
            break;
          case "italic":
          case "emphasis":
            rendered = <em>{child}</em>;
            break;
          case "underline":
            rendered = <u>{child}</u>;
            break;
          case "code":
            rendered = <code>{child}</code>;
            break;
          case "br":
            rendered = <br />;
            break;
          case "paragraph":
            rendered = <p>{child}</p>;
            break;
          case "heading":
            {
              const Heading =
                `h${[1, 2, 3, 4, 5, 6].includes(node.level || 0) ? node.level : 3}` as
                  "h1" | "h2" | "h3" | "h4" | "h5" | "h6";
              rendered = <Heading>{child}</Heading>;
            }
            break;
          case "link":
            rendered = safeUrl(node.href) ? (
              <a href={safeUrl(node.href)} rel="noopener noreferrer">
                {child}
              </a>
            ) : (
              child
            );
            break;
          case "emoji":
            rendered = (
              <CustomEmoji
                id={node.id}
                fallback={node.fallback}
                label={node.label}
              />
            );
            break;
          case "quote":
            rendered = <blockquote>{child}</blockquote>;
            break;
          case "details":
            rendered = (
              <details>
                <summary>{node.title || messages.m59589fe2a5}</summary>
                {child}
              </details>
            );
            break;
          case "list":
            {
              const List = node.ordered ? "ol" : "ul";
              rendered = (
                <List>
                  {node.items?.map((item, i) => (
                    <li key={i}>
                      <SemanticContent nodes={item} depth={depth + 1} />
                    </li>
                  ))}
                </List>
              );
            }
            break;
          case "table":
            rendered = (
              <div className="table-scroll">
                <table>
                  <tbody>
                    {node.rows?.map((row, i) => (
                      <tr key={i}>
                        {row.map((cell, j) => (
                          <td key={j}>
                            <SemanticContent nodes={cell} depth={depth + 1} />
                          </td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            );
            break;
          default:
            rendered = node.text ?? child;
        }
        return <Fragment key={index}>{rendered}</Fragment>;
      })}
    </>
  );
}
