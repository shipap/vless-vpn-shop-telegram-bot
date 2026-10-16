import { expect, it } from "vitest";
import { Children, isValidElement, type ReactNode } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { OperationalIssues } from "../../apps/web/dashboard/operational-issues";
import type { Mutation } from "../../apps/web/dashboard/admin";

const issue = {
  key: "provider_operation:unknown-operation",
  kind: "provider_operation",
  reference: "unknown-operation",
  state: "outcome_unknown",
  age_seconds: 3600,
  reason: "Результат оплаты неизвестен",
  target: "/admin#orders",
  can_acknowledge: true,
  ack_token: "a".repeat(64),
};

function findReviewAction(node: ReactNode): (() => void) | undefined {
  for (const child of Children.toArray(node)) {
    if (!isValidElement<{ children?: ReactNode; onClick?: () => void }>(child))
      continue;
    if (child.props.children === "Разобрано") return child.props.onClick;
    const nested = findReviewAction(child.props.children);
    if (nested) return nested;
  }
}

it("offers review only with owner permission and a current actionable token", () => {
  const render = (canManage: boolean, actionable = true) =>
    renderToStaticMarkup(
      <OperationalIssues
        canManage={canManage}
        mutate={() => {}}
        value={{ items: [{ ...issue, can_acknowledge: actionable }] }}
      />,
    );
  expect(render(true)).toContain("Разобрано");
  expect(render(false)).not.toContain("Разобрано");
  expect(render(true, false)).not.toContain("Разобрано");
});

it("requests explicit confirmation with the snapshot token while retaining default fresh MFA", () => {
  const changes: Mutation[] = [];
  const tree = OperationalIssues({
    canManage: true,
    value: { items: [issue] },
    mutate: (change) => changes.push(change),
  });
  const act = findReviewAction(tree);
  expect(act).toBeTypeOf("function");
  act!();
  expect(changes).toHaveLength(1);
  expect(changes[0].path).toBe(
    "/admin/operations/provider/unknown-operation/acknowledge",
  );
  expect(changes[0].body).toEqual({ ack_token: "a".repeat(64) });
  expect(changes[0].method).toBe("POST");
  expect(changes[0].freshMfa).not.toBe(false);
  expect(changes[0].description).toContain("сверка продолжится");
});

function find(tree: any, caption: string): any {
  if (Array.isArray(tree))
    return tree.map((node) => find(node, caption)).find(Boolean);
  if (!tree || typeof tree !== "object") return null;
  if (
    tree.props?.children === caption &&
    typeof tree.props?.onClick === "function"
  )
    return tree;
  return find(tree.props?.children, caption);
}
const item = {
  key: "delivery:review-id",
  kind: "delivery",
  reference: "review-id",
  state: "review",
  age_seconds: 1200,
  reason: "Неизвестный результат доставки",
  can_acknowledge: true,
  ack_token: "a".repeat(64),
  target: "/admin#operations",
};
it("resolved delivery requests protected acknowledgement without retry or payment", () => {
  const changes: any[] = [];
  const tree = OperationalIssues({
    value: { items: [item], signal_count: 1 },
    canManage: true,
    mutate: (value) => changes.push(value),
  });
  const button = find(tree, "Решено");
  expect(button).toBeTruthy();
  expect(changes).toEqual([]);
  button.props.onClick();
  expect(changes).toEqual([
    expect.objectContaining({
      path: "/admin/operations/deliveries/review-id/acknowledge",
      method: "POST",
      body: { ack_token: "a".repeat(64) },
      confirmLabel: "Решено",
    }),
  ]);
});
it("does not offer resolution without capability or for active delivery", () => {
  for (const [canManage, can_acknowledge] of [
    [false, true],
    [true, false],
  ]) {
    const tree = OperationalIssues({
      value: { items: [{ ...item, can_acknowledge }] },
      canManage,
      mutate: () => {
        throw Error("unexpected action");
      },
    });
    expect(find(tree, "Решено")).toBeFalsy();
  }
  for (const state of ["pending", "in_flight"]) {
    const tree = OperationalIssues({
      value: { items: [{ ...item, state }] },
      canManage: true,
      mutate: () => {
        throw Error("unexpected action");
      },
    });
    expect(find(tree, "Решено")).toBeFalsy();
  }
});
it("keeps the existing unknown invoice acknowledgement separate", () => {
  const changes: any[] = [];
  const tree = OperationalIssues({
    value: {
      items: [
        { ...item, kind: "provider_operation", state: "outcome_unknown" },
      ],
    },
    canManage: true,
    mutate: (value) => changes.push(value),
  });
  expect(find(tree, "Решено")).toBeFalsy();
  find(tree, "Разобрано").props.onClick();
  expect(changes[0]).toMatchObject({
    path: "/admin/operations/provider/review-id/acknowledge",
    confirmLabel: "Разобрано",
  });
});
