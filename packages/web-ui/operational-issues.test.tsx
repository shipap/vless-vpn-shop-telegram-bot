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
