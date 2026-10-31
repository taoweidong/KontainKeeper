/** api/system.ts 与 api/routes.ts 的单元测试：健康/统计端点 + RBAC 菜单装配契约。
 *
 *  锁住的语义：
 *  - `/api/health` 与 `/api/system/stats` 是无参 GET（欢迎页与系统页的唯一读数来源）；
 *  - `null`（后端明确说不知道）与 `0`（真读数）是两种语义，api 层一律不兜底（QR-W16）；
 *  - 2026-10-31 RBAC 二阶段（改锁依据）：菜单树完全由数据库下发——getAsyncRoutes
 *    拉 /api/auth/menus（平表）组树后放进 data，交给 pure-admin 的
 *    handleAsyncRoutes 动态注册；未授权的菜单后端根本不下发（不加载）。
 *    失败 fail-closed（空树）。一阶段的「前端过滤静态路由」已被本方案取代。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";

const httpMock = vi.hoisted(() => ({ request: vi.fn() }));
vi.mock("@/utils/http", () => ({ http: httpMock }));

import { getHealth, getStats } from "./system";
import { buildMenuTree, getAsyncRoutes } from "./routes";

beforeEach(() => {
  httpMock.request.mockReset();
  httpMock.request.mockResolvedValue({});
});

describe("getHealth", () => {
  it("GET /api/health 无参；bridge=null（未连 Broker）是第三态不被兜成空对象；agents_online=0 是真读数", async () => {
    httpMock.request.mockResolvedValue({
      ok: true,
      version: "0.1.0",
      proto_ver: 4,
      agents_online: 3,
      broker: "connected",
      bridge: { published: 12 }
    });
    const res = await getHealth();
    expect(httpMock.request).toHaveBeenCalledWith("get", "/api/health");
    expect(res.proto_ver).toBe(4);
    expect(res.broker).toBe("connected");
    expect(res.agents_online).toBe(3);

    httpMock.request.mockResolvedValue({
      ok: true,
      broker: "disconnected",
      bridge: null,
      agents_online: 0
    });
    const down = await getHealth();
    expect(down.bridge).toBeNull();
    expect(down.agents_online).toBe(0); // 页面用 read 位区分「失败」与「真的 0 台」
  });
});

describe("getStats", () => {
  it("GET /api/system/stats 无参；last_msg_age_sec / stats 的 null 表示「从未收到消息」，不能变 0 秒前或空对象", async () => {
    await getStats();
    expect(httpMock.request).toHaveBeenCalledWith("get", "/api/system/stats");

    httpMock.request.mockResolvedValue({
      ok: true,
      uptime_sec: 10,
      hosts: { total: 0, online: 0 },
      commands: {},
      storage: { heartbeats: 0, hourly: 0 },
      broker: { connected: true, stats: null, last_msg_age_sec: null }
    });
    const res = await getStats();
    expect(res.broker.last_msg_age_sec).toBeNull();
    expect(res.broker.stats).toBeNull();
  });

  it("agent_latest_ver / agents_outdated / updates 是可选字段，缺省时不补 0", async () => {
    httpMock.request.mockResolvedValue({ ok: true });
    const res = await getStats();
    expect(res.agent_latest_ver).toBeUndefined();
    expect(res.agents_outdated).toBeUndefined();
    expect(res.updates).toBeUndefined();
  });
});

describe("getAsyncRoutes / buildMenuTree：数据库菜单装配", () => {
  const rows = [
    { name: "Hosts", parent: "", title: "主机管理", icon: "ep/monitor", rank: 1, path: "/hosts", component: "Layout", show_link: true },
    { name: "HostUpdate", parent: "Hosts", title: "版本与更新", icon: "", rank: 4, path: "/hosts/update", component: "/src/views/host/update/index.vue", show_link: true },
    { name: "HostMonitor", parent: "Hosts", title: "主机总览", icon: "", rank: 1, path: "/hosts/monitor", component: "/src/views/host/monitor/index.vue", show_link: true },
    { name: "HostDetail", parent: "Hosts", title: "主机详情", icon: "", rank: 2, path: "/hosts/detail/:pod", component: "/src/views/host/detail/index.vue", show_link: false },
    { name: "Home", parent: "", title: "首页", icon: "ep/home-filled", rank: 0, path: "/home", component: "Layout", show_link: true },
    { name: "Welcome", parent: "Home", title: "首页", icon: "", rank: 1, path: "/welcome", component: "/src/views/welcome/index.vue", show_link: true }
  ];

  it("拉取 /api/auth/menus 平表组树：兄弟按 rank 排序，component 字符串透传", async () => {
    httpMock.request.mockResolvedValue({ roles: ["operator"], menus: rows });
    const res = await getAsyncRoutes();

    expect(httpMock.request).toHaveBeenCalledWith("get", "/api/auth/menus");
    const tree = res.data;
    expect(tree.map((n: any) => n.name)).toEqual(["Home", "Hosts"]);
    const hosts = tree[1];
    expect(hosts.component).toBe("Layout");
    // 子级按 rank 排序；show_link=false 的详情页仍在树里（注册路由、不出侧边栏）
    expect(hosts.children.map((c: any) => c.name)).toEqual([
      "HostMonitor", "HostDetail", "HostUpdate"
    ]);
    expect(hosts.children[1].path).toBe("/hosts/detail/:pod");
    expect(hosts.children[1].meta.showLink).toBe(false);
  });

  it("buildMenuTree：乱序输入也按 rank + name 稳定组树", () => {
    const tree = buildMenuTree([...rows].reverse());
    expect(tree.map((n: any) => n.name)).toEqual(["Home", "Hosts"]);
    expect(tree[1].children[0].name).toBe("HostMonitor");
  });

  it("后端没下发的菜单不会出现在树里（未授权 = 不加载）", () => {
    const tree = buildMenuTree([rows[0], rows[3]]);
    expect(tree.map((n: any) => n.name)).toEqual(["Hosts"]);
    expect(tree[0].children.map((c: any) => c.name)).toEqual(["HostDetail"]);
  });

  it("非 401 失败 → fail-closed：空树照常 resolve（业务路由不注册，直连 404）", async () => {
    httpMock.request.mockRejectedValue({ response: { status: 500 } });
    const res = await getAsyncRoutes();
    expect(res).toEqual({ success: true, data: [] });
  });

  it("401 = 会话过期：同样返回空树（拦截器已在走登出跳登录）", async () => {
    httpMock.request.mockRejectedValue({ response: { status: 401 } });
    const res = await getAsyncRoutes();
    expect(res).toEqual({ success: true, data: [] });
  });
});
