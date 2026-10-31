/** api/system.ts 与 api/routes.ts 的单元测试：健康/统计端点 + RBAC 菜单装配契约。
 *
 *  锁住的语义：
 *  - `/api/health` 与 `/api/system/stats` 是无参 GET（欢迎页与系统页的唯一读数来源）；
 *  - `null`（后端明确说不知道）与 `0`（真读数）是两种语义，api 层一律不兜底（QR-W16）；
 *  - 2026-10-31 RBAC（改锁依据）：getAsyncRoutes 从「恒空数组不打后端」改为
 *    **拉取 /api/auth/menus 的允许集合**并写入 user store——路由记录仍返回空数组
 *    （组件不来自后端），失败时 fail-closed（空集合），401 交给拦截器登出不拦。
 *    旧语义（菜单完全静态）已由 RBAC 需求推翻，见 api/routes.ts 模块注释。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";

const httpMock = vi.hoisted(() => ({ request: vi.fn() }));
vi.mock("@/utils/http", () => ({ http: httpMock }));

const userStore = vi.hoisted(() => {
  const state = { menus: [] as any[], menusLoaded: false, roles: [] as string[] };
  return {
    state,
    hook: () => ({
      get menus() {
        return state.menus;
      },
      get menusLoaded() {
        return state.menusLoaded;
      },
      get roles() {
        return state.roles;
      },
      SET_MENUS(m: any[]) {
        state.menus = m;
        state.menusLoaded = true;
      },
      SET_ROLES(r: string[]) {
        state.roles = r;
      }
    })
  };
});
vi.mock("@/store/modules/user", () => ({
  useUserStoreHook: userStore.hook
}));

import { getHealth, getStats } from "./system";
import { getAsyncRoutes } from "./routes";

beforeEach(() => {
  httpMock.request.mockReset();
  httpMock.request.mockResolvedValue({});
  userStore.state.menus = [];
  userStore.state.menusLoaded = false;
  userStore.state.roles = [];
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

describe("getAsyncRoutes：RBAC 菜单装配", () => {
  const menus = [
    { name: "Home", title: "首页", icon: "ep/home-filled", rank: 0, show_link: true },
    { name: "Hosts", title: "主机管理", icon: "ep/monitor", rank: 1, show_link: true }
  ];

  it("拉取 /api/auth/menus 写入 user store，路由记录仍返回空数组（组件不来自后端）", async () => {
    httpMock.request.mockResolvedValue({ roles: ["operator"], menus });
    const res = await getAsyncRoutes();

    expect(res).toEqual({ success: true, data: [] });
    expect(httpMock.request).toHaveBeenCalledWith("get", "/api/auth/menus");
    expect(userStore.state.roles).toEqual(["operator"]);
    expect(userStore.state.menus).toEqual(menus);
    expect(userStore.state.menusLoaded).toBe(true);
  });

  it("非 401 失败 → fail-closed：置空允许集合并照常 resolve（不炸路由装配）", async () => {
    httpMock.request.mockRejectedValue({ response: { status: 500 } });
    const res = await getAsyncRoutes();

    expect(res).toEqual({ success: true, data: [] });
    expect(userStore.state.menus).toEqual([]);
    expect(userStore.state.menusLoaded).toBe(true);
  });

  it("401 = 会话过期：不置 menusLoaded（交给拦截器登出跳登录），不做 fail-closed", async () => {
    httpMock.request.mockRejectedValue({ response: { status: 401 } });
    const res = await getAsyncRoutes();

    expect(res).toEqual({ success: true, data: [] });
    expect(userStore.state.menusLoaded).toBe(false);
  });
});
