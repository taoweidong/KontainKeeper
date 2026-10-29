/** api/system.ts 与 api/routes.ts 的单元测试：健康/统计端点 + 静态菜单契约。
 *
 *  锁住的语义：
 *  - `/api/health` 与 `/api/system/stats` 是无参 GET（欢迎页与系统页的唯一读数来源）；
 *  - `null`（后端明确说不知道）与 `0`（真读数）是两种语义，api 层一律不兜底（QR-W16）；
 *  - `getAsyncRoutes()` 恒返回空数组且不打后端——菜单完全静态是 prod 下不白屏的前提。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";

const httpMock = vi.hoisted(() => ({ request: vi.fn() }));
vi.mock("@/utils/http", () => ({ http: httpMock }));

import { getHealth, getStats } from "./system";
import { getAsyncRoutes } from "./routes";

beforeEach(() => {
  httpMock.request.mockReset();
  httpMock.request.mockResolvedValue({});
});

describe("getHealth", () => {
  it("GET /api/health 无参；bridge=null（未连 Broker）是第三态不被兜成空对象；agents_online=0 是真读数", async () => {
    httpMock.request.mockResolvedValue({
      ok: true, version: "0.1.0", proto_ver: 4, agents_online: 3,
      broker: "connected", bridge: { published: 12 }
    });
    const res = await getHealth();
    expect(httpMock.request).toHaveBeenCalledWith("get", "/api/health");
    expect(res.proto_ver).toBe(4);
    expect(res.broker).toBe("connected");
    expect(res.agents_online).toBe(3);

    httpMock.request.mockResolvedValue({ ok: true, broker: "disconnected", bridge: null, agents_online: 0 });
    const down = await getHealth();
    expect(down.bridge).toBeNull();
    expect(down.agents_online).toBe(0);   // 页面用 read 位区分「失败」与「真的 0 台」
  });
});

describe("getStats", () => {
  it("GET /api/system/stats 无参；last_msg_age_sec / stats 的 null 表示「从未收到消息」，不能变 0 秒前或空对象", async () => {
    await getStats();
    expect(httpMock.request).toHaveBeenCalledWith("get", "/api/system/stats");

    httpMock.request.mockResolvedValue({
      ok: true, uptime_sec: 10, hosts: { total: 0, online: 0 }, commands: {},
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

describe("getAsyncRoutes：菜单完全静态", () => {
  it("同步返回 {success:true, data:[]}，不发任何请求（多次调用形状一致）", async () => {
    const a = await getAsyncRoutes();
    const b = await getAsyncRoutes();

    expect(a).toEqual({ success: true, data: [] });
    expect(b).toEqual({ success: true, data: [] });
    expect(httpMock.request).not.toHaveBeenCalled();
  });
});
