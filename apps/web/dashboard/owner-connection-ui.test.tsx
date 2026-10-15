import { expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { ConnectionGuide } from "./connection-guide";
import { AdminV11 } from "./admin-v11";
import { CustomerDevices } from "./customer-devices";
import { StaffPermissions } from "@ui/staff-permissions";
import type { Session } from "@api/types";

const calls = vi.hoisted(() => [] as string[]);
vi.mock("@api/client", async (original) => ({
  ...(await original<typeof import("@api/client")>()),
  useApi: (path: string) => {
    calls.push(path);
    if (path.endsWith("/inventory?refresh=true"))
      return {
        data: {
          source: "remnawave",
          devices: [],
          status: "ACTIVE",
          observed_at: "2026-10-13T12:00:00Z",
          traffic_bytes: "0",
          traffic_limit_bytes: null,
          error: null,
        },
        loading: false,
        error: null,
        reload: () => {},
        setData: () => {},
      };
    return {
      data: {
        source: "remnawave",
        notice:
          "Можно делиться подпиской с друзьями и семьёй. Публичная оферта.",
        platforms: [
          {
            id: "linux",
            name: "Linux",
            apps: [
              {
                name: "Клиент панели",
                steps: [
                  {
                    title: "Импорт",
                    description: "Добавьте подписку",
                    links: [
                      {
                        text: "Скачать",
                        url: "https://client.example.test/download",
                      },
                    ],
                  },
                ],
              },
            ],
          },
        ],
      },
      loading: false,
      error: null,
      reload: () => {},
    };
  },
}));

it("uses the shared authorized instruction DTO without the removed share notice", () => {
  calls.length = 0;
  const html = renderToStaticMarkup(
    <ConnectionGuide subscriptionId="own-sub" />,
  );
  expect(calls).toEqual(["/subscriptions/own-sub/connection-instructions"]);
  expect(html).toContain("Клиент панели");
  expect(html).toContain("Добавьте подписку");
  expect(html).toContain("https://client.example.test/download");
  expect(html).not.toContain("Можно делиться");
  expect(html).not.toContain("Публичная оферта");
});

it("reads fresh inventory on device entry through GET without a mutation request", () => {
  calls.length = 0;
  renderToStaticMarkup(
    <CustomerDevices
      subscription={{ id: "own-sub" } as never}
      blocked={false}
      onAdd={() => {}}
      onConnect={() => {}}
      onRefresh={() => {}}
      onRotate={() => {}}
    />,
  );
  expect(calls).toEqual(["/subscriptions/own-sub/inventory?refresh=true"]);
});

it("does not mount or fetch backup controls without the owner backend capability", () => {
  calls.length = 0;
  const session = {
    is_admin: true,
    staff_role: "admin",
    capabilities: ["users_read"],
  } as Session;
  const html = renderToStaticMarkup(
    <StaffPermissions session={session}>
      <AdminV11 section="backups" mutate={() => {}} refreshVersion={0} />
    </StaffPermissions>,
  );
  expect(calls).toEqual([]);
  expect(html).toContain("Страница не найдена");
  expect(html).not.toContain("Создать резервную копию");
});
