/** 菜单装配契约锁（QR-W21）：buildMenuTree 平表组树 + getAsyncRoutes fail-closed。
 *  RBAC 二阶段的安全语义在此钉死：接口失败 = 空树（业务路由不注册，直连 URL 落 404），
 *  绝不退回全量菜单。 */
import { beforeEach, describe, expect, it, vi } from "vitest";

const rbacState = vi.hoisted(() => ({ getAuthMenus: vi.fn() }));
vi.mock("@/api/rbac", () => ({
  getAuthMenus: rbacState.getAuthMenus,
  getAuthMenusCache: vi.fn()
}));

import type { KkMenuMeta } from "@/api/rbac";
import { buildMenuTree, getAsyncRoutes } from "@/api/routes";

const menu = (over: Partial<KkMenuMeta> = {}): KkMenuMeta => ({
  name: "X",
  parent: "",
  title: "T",
  icon: "",
  rank: 1,
  path: "/x",
  component: "/src/views/x.vue",
  show_link: true,
  ...over
});

beforeEach(() => {
  rbacState.getAuthMenus.mockReset();
});

describe("buildMenuTree：平表组树", () => {
  it("两级装配：分组为父、页面为子，rank 升序（同 rank 按 name 稳定）", () => {
    const tree = buildMenuTree([
      menu({ name: "B2", parent: "G", rank: 2, title: "乙" }),
      menu({ name: "A1", parent: "G", rank: 1, title: "甲" }),
      menu({ name: "G", rank: 0, path: "/g", component: "Layout" }),
      menu({ name: "Top", rank: 1, path: "/top" })
    ]);
    expect(tree.map(n => n.name)).toEqual(["G", "Top"]);
    expect(tree[0].children.map((c: any) => c.name)).toEqual(["A1", "B2"]);
    expect(tree[0].component).toBe("Layout");
    expect(tree[0].children[0].component).toBe("/src/views/x.vue");
  });

  it("空 icon 不产 meta.icon；show_link 原样透传给 meta.showLink", () => {
    const [node] = buildMenuTree([
      menu({ name: "Hidden", path: "/h/:pod", icon: "", show_link: false })
    ]);
    expect(node.meta.icon).toBeUndefined();
    expect(node.meta.showLink).toBe(false);
    expect(node.path).toBe("/h/:pod");
  });

  it("component 为空不产 component 键（容器节点只做分组）", () => {
    const [node] = buildMenuTree([menu({ name: "G", component: "" })]);
    expect(node.component).toBeUndefined();
  });
});

describe("getAsyncRoutes：fail-closed 契约", () => {
  it("接口失败返回空树，success 恒 true（侧边栏为空而不是退回全量菜单）", async () => {
    rbacState.getAuthMenus.mockRejectedValueOnce(new Error("boom"));
    const res = await getAsyncRoutes();
    expect(res.success).toBe(true);
    expect(res.data).toEqual([]);
  });

  it("接口成功时返回组好的树", async () => {
    rbacState.getAuthMenus.mockResolvedValueOnce({
      roles: ["admin"],
      menus: [menu({ name: "Only", path: "/only" })]
    });
    const res = await getAsyncRoutes();
    expect(res.success).toBe(true);
    expect((res.data[0] as any).name).toBe("Only");
  });
});
