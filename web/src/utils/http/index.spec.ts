/** utils/http/index.ts 的单元测试：直连后端的唯一出入口，安全语义都压在这两层拦截器上。
 *
 *  锁住的语义（为什么值得锁）：
 *  1. 请求拦截器：除 `/login` 外的请求必须带 `Authorization`；显式 `beforeRequestCallback`
 *     短路 token 注入（模板契约，业务页会传自定义回调）。这条改坏 = 全站 401 或凭据泄漏到登录接口。
 *  2. 响应拦截器：成功时返回 `response.data` 而不是 response（`api/exporting.ts` 的
 *     「拿不到 Content-Disposition」就是这条的直接后果）。
 *  3. 401 自动登出的去重窗 `AUTO_LOGOUT_DEDUP_MS = 1000`（QR29-W7）：6 路轮询同时过期时
 *     只登出一次并给出「登录已过期」提示，否则用户被静默弹回登录页且不知原因。
 *  4. 401 的三种「不登出」例外：`/login`（凭据错误 ≠ 会话失效）、`/logout`（防请求风暴，
 *     QR29-W1）、403 一律不登出（`deps.py` 的 agent IP 白名单语义，浏览器会话依然有效）。
 *  5. 取消请求被标记 `isCancelRequest`，供调用方与真失败区分。
 *
 *  实现手法：`http` 是 import 期创建的单例，且 `lastAutoLogoutAt` 是模块级可变状态，
 *  所以每条用例都走 `vi.resetModules()` + 动态 import 拿干净实例；网络层不 mock axios
 *  默认导出（会失真），而是用 axios 的 `adapter` 钩子让整条拦截器链真跑一遍，
 *  再断言 adapter 收到的最终 config。`@/utils/auth`、`@/utils/message`、
 *  `@/store/modules/user` 桩掉，避免把 js-cookie / element-plus / pinia+router 的整张依赖图拖进来。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AxiosError, CanceledError } from "axios";

const h = vi.hoisted(() => ({
  getToken: vi.fn(),
  formatToken: vi.fn((t: string) => `Bearer ${t}`),
  message: vi.fn(),
  logOut: vi.fn()
}));

vi.mock("@/utils/auth", () => ({ getToken: h.getToken, formatToken: h.formatToken }));
vi.mock("@/utils/message", () => ({ message: h.message }));
vi.mock("@/store/modules/user", () => ({
  useUserStoreHook: () => ({ logOut: h.logOut })
}));

type Http = typeof import("@/utils/http")["http"];

/** 与 utils/http/index.ts 的 AUTO_LOGOUT_DEDUP_MS 同尺（源文件未导出，改那里要同步这里） */
const DEDUP_MS = 1000;

/** 每条用例一个干净的 PureHttp（含干净的 lastAutoLogoutAt） */
async function freshHttp(): Promise<Http> {
  vi.resetModules();
  const mod = await import("@/utils/http");
  return mod.http;
}

const authOf = (config: any): string | undefined =>
  config?.headers?.["Authorization"] ?? config?.headers?.get?.("Authorization") ?? undefined;

const headerOf = (config: any, name: string): string | undefined =>
  config?.headers?.[name] ?? config?.headers?.get?.(name);

/** 造一个自定义 adapter：记录它收到的最终 config（即拦截器处理完的请求），
 *  并按 stub 的状态码决定 resolve/reject —— 与 axios 内置 adapter 的 settle 语义一致：
 *  非 2xx 必须带 response 抛 AxiosError，否则响应拦截器的失败分支根本不会被触发。 */
function captureAdapter(stub: { status: number; data?: any }) {
  const seen: any[] = [];
  const adapter = (config: any) => {
    seen.push(config);
    const response = {
      data: stub.data,
      status: stub.status,
      statusText: "",
      headers: {},
      config,
      request: {}
    };
    return stub.status >= 200 && stub.status < 300
      ? Promise.resolve(response)
      : Promise.reject(
          new AxiosError(
            `Request failed with status code ${stub.status}`,
            undefined,
            config,
            {},
            response
          )
        );
  };
  return { seen, adapter };
}

/** 让 adapter 直接抛任意错误（测取消请求、超时等非 HTTP 错误路径） */
function rejectAdapter(makeError: (config: any) => unknown) {
  const seen: any[] = [];
  const adapter = (config: any) => {
    seen.push(config);
    return Promise.reject(makeError(config));
  };
  return { seen, adapter };
}

/** 发一次请求并取回错误（失败路径专用；返回 any 便于断言 AxiosError 上的各个面） */
async function expectError(http: Http, status: number, url: string): Promise<any> {
  const { adapter } = captureAdapter({ status, data: { detail: "nope" } });
  return http.request<any>("post", url, {}, { adapter }).catch((e: any) => e);
}

beforeEach(() => {
  vi.useFakeTimers();
  // 去重窗判定用 Date.now()；取一个远大于窗口的固定时刻，保证「第一次一定放行」
  vi.setSystemTime(new Date("2026-04-30T08:00:00Z"));
  h.getToken.mockReset();
  h.formatToken.mockClear();
  h.message.mockReset();
  h.logOut.mockReset();
});

afterEach(() => {
  vi.useRealTimers();
});

describe("请求拦截器：Authorization 注入矩阵", () => {
  it("非 /login 带 token 才注入；/login 与无 token/缺字段都不注入", async () => {
    const http = await freshHttp();

    h.getToken.mockReturnValue({ accessToken: "tk-1", expires: 0, refreshToken: "" });
    const a = captureAdapter({ status: 200, data: {} });
    await http.request("get", "/api/containers", {}, { adapter: a.adapter });
    expect(authOf(a.seen[0])).toBe("Bearer tk-1");
    expect(h.formatToken).toHaveBeenCalledWith("tk-1");

    const login = captureAdapter({ status: 200, data: {} });
    await http.request("post", "/api/login", { data: { username: "admin" } }, { adapter: login.adapter });
    expect(authOf(login.seen[0])).toBeUndefined();   // 凭据接口不复用会话

    h.getToken.mockReturnValue(undefined);
    const noToken = captureAdapter({ status: 200, data: {} });
    await http.request("get", "/api/health", {}, { adapter: noToken.adapter });
    expect(authOf(noToken.seen[0])).toBeUndefined(); // 未登录首屏请求要能发出去

    // getToken 回落 localStorage 的形状没有 accessToken —— 字段缺失不等于有 token
    h.getToken.mockReturnValue({ refreshToken: "rt", username: "admin" });
    const partial = captureAdapter({ status: 200, data: {} });
    await http.request("get", "/api/system/stats", {}, { adapter: partial.adapter });
    expect(authOf(partial.seen[0])).toBeUndefined();
  });

  it("显式 beforeRequestCallback 短路默认逻辑：回调收到 config，token 注入整段被跳过", async () => {
    h.getToken.mockReturnValue({ accessToken: "tk-1" });
    const http = await freshHttp();
    const { seen, adapter } = captureAdapter({ status: 200, data: {} });
    const spy = vi.fn();

    await http.request("get", "/api/containers", {}, { adapter, beforeRequestCallback: spy });

    expect(spy).toHaveBeenCalledTimes(1);
    expect(spy.mock.calls[0][0].url).toBe("/api/containers");
    expect(authOf(seen[0])).toBeUndefined();
    expect(h.getToken).not.toHaveBeenCalled();
  });

  it("initConfig 的全局回调同样短路 token 注入（模板预留的装配点，别悄悄失效）", async () => {
    h.getToken.mockReturnValue({ accessToken: "tk-1" });
    const http = await freshHttp();
    const initConfig = (http.constructor as any).initConfig;
    const spy = vi.fn();
    initConfig.beforeRequestCallback = spy;
    const { seen, adapter } = captureAdapter({ status: 200, data: {} });

    await http.request("get", "/api/containers", {}, { adapter });

    expect(spy).toHaveBeenCalledTimes(1);
    expect(authOf(seen[0])).toBeUndefined();
    initConfig.beforeRequestCallback = undefined;   // 单例静态位，还回去
  });

  it("默认配置随请求下发，业务页可覆盖；第 4 个参数覆盖第 3 个（axiosConfig 后展开）", async () => {
    h.getToken.mockReturnValue(undefined);
    const http = await freshHttp();

    const plain = captureAdapter({ status: 200, data: {} });
    await http.request("get", "/api/containers", {}, { adapter: plain.adapter });
    expect(plain.seen[0].timeout).toBe(10000);
    expect(headerOf(plain.seen[0], "Content-Type")).toBe("application/json");

    const exportish = captureAdapter({ status: 200, data: {} });
    await http.request("get", "/api/export/hosts", { timeout: 0, responseType: "blob" }, { adapter: exportish.adapter });
    expect(exportish.seen[0].timeout).toBe(0);      // 导出必须 timeout:0，否则千行必超时
    expect(exportish.seen[0].responseType).toBe("blob");

    const both = captureAdapter({ status: 200, data: {} });
    await http.request("post", "/api/commands", { timeout: 1234 }, { adapter: both.adapter, timeout: 5678 });
    expect(both.seen[0].timeout).toBe(5678);
    expect(both.seen[0].method).toBe("post");
  });

  it("post/get 便捷方法只改 method，其余语义与 request 一致", async () => {
    h.getToken.mockReturnValue({ accessToken: "tk-2" });
    const http = await freshHttp();
    const a = captureAdapter({ status: 200, data: "D" });
    const b = captureAdapter({ status: 200, data: "G" });

    await expect(http.post("/api/x", { data: { a: 1 } }, { adapter: a.adapter })).resolves.toBe("D");
    await expect(http.get("/api/y", { params: { q: "1" } }, { adapter: b.adapter })).resolves.toBe("G");

    expect(a.seen[0].method).toBe("post");
    // 默认 transformRequest 会按 Content-Type: application/json 把 body 序列化成字符串，
    // 到 adapter 手里已是文本——断言序列化结果，防止「以为还能传对象」
    expect(typeof a.seen[0].data).toBe("string");
    expect(JSON.parse(a.seen[0].data)).toEqual({ a: 1 });
    expect(b.seen[0].method).toBe("get");
    expect(b.seen[0].params).toEqual({ q: "1" });
    expect(authOf(b.seen[0])).toBe("Bearer tk-2");
  });
});

describe("响应拦截器：成功路径", () => {
  it("返回 response.data 而不是整个 response，空数组与 0 都原样透传", async () => {
    const http = await freshHttp();

    const obj = captureAdapter({ status: 200, data: { items: [1, 2], total: 2 } });
    await expect(http.request("get", "/api/audit", {}, { adapter: obj.adapter })).resolves.toEqual({
      items: [1, 2],
      total: 2
    });

    const arr = captureAdapter({ status: 200, data: [] });
    await expect(http.request("get", "/api/x", {}, { adapter: arr.adapter })).resolves.toEqual([]);

    const zero = captureAdapter({ status: 200, data: { total: 0 } });
    await expect(http.request("get", "/api/y", {}, { adapter: zero.adapter })).resolves.toEqual({ total: 0 });
  });

  it("beforeResponseCallback 收到完整 response，返回值仍是 data；非 2xx 由 validateStatus 裁决", async () => {
    const http = await freshHttp();

    const spy = vi.fn();
    const cb = captureAdapter({ status: 204, data: { ok: true } });
    const res = await http.request("get", "/api/x", {}, { adapter: cb.adapter, beforeResponseCallback: spy });
    expect(spy.mock.calls[0][0].status).toBe(204);   // 回调能看到状态码
    expect(res).toEqual({ ok: true });

    // initConfig 预留的全局响应回调走同一条短路（装配点在 create_app 那侧，别悄悄失效）
    const globalSpy = vi.fn();
    (http.constructor as any).initConfig.beforeResponseCallback = globalSpy;
    const g = captureAdapter({ status: 200, data: { n: 1 } });
    await expect(http.request("get", "/api/y", {}, { adapter: g.adapter })).resolves.toEqual({ n: 1 });
    expect(globalSpy).toHaveBeenCalledTimes(1);
    expect(globalSpy.mock.calls[0][0].status).toBe(200);
    (http.constructor as any).initConfig.beforeResponseCallback = undefined;

    const redirect = captureAdapter({ status: 302, data: "redirect" });
    await expect(http.request("get", "/api/x", {}, { adapter: redirect.adapter })).rejects.toBeTruthy();
  });
});

describe("401 自动登出去重窗（QR29-W7）", () => {
  const unauthorized = async (http: Http, url = "/api/containers"): Promise<any> => {
    const { adapter } = captureAdapter({ status: 401, data: { detail: "unauthorized" } });
    return http.request<any>("get", url, {}, { adapter }).catch((e: any) => e);
  };

  it("窗口内 6 路轮询同时过期只登出一次、只弹一条过期提示，但错误照常抛给调用方", async () => {
    const http = await freshHttp();

    for (let i = 0; i < 6; i++) {
      const e = await unauthorized(http);
      expect(e.response.status).toBe(401);          // 去重只限副作用
      expect(e.isAxiosError).toBe(true);
    }

    expect(h.logOut).toHaveBeenCalledTimes(1);
    expect(h.message).toHaveBeenCalledTimes(1);
    expect(h.message.mock.calls[0][0]).toBe("登录已过期，请重新登录");
    expect(h.message.mock.calls[0][1]).toEqual({ type: "warning" });
  });

  it("严格跨过 1000ms 才允许第二次登出（边界值本身仍被去重）", async () => {
    const http = await freshHttp();
    await unauthorized(http);
    expect(h.logOut).toHaveBeenCalledTimes(1);

    vi.advanceTimersByTime(DEDUP_MS);   // 恰好等于窗口 → 仍不去重窗内
    await unauthorized(http);
    expect(h.logOut).toHaveBeenCalledTimes(1);

    vi.advanceTimersByTime(1);
    await unauthorized(http);
    expect(h.logOut).toHaveBeenCalledTimes(2);
    expect(h.message).toHaveBeenCalledTimes(2);
  });
});

describe("401/403 的例外与边界", () => {
  it("/login 的 401 是凭据错误、/logout 的 401 是风暴源，都不登出（QR29-W1）", async () => {
    const http = await freshHttp();

    for (const url of ["/api/login", "/api/logout"]) {
      const err = await expectError(http, 401, url);
      expect(err.response.status).toBe(401);
    }
    expect(h.logOut).not.toHaveBeenCalled();
    expect(h.message).not.toHaveBeenCalled();
  });

  it("403 一律不登出：agent IP 白名单拒绝（deps.py）与浏览器会话无关", async () => {
    const http = await freshHttp();

    for (const url of ["/api/system/agent", "/api/containers", "/api/login", "/api/logout"])
      expect((await expectError(http, 403, url)).response.status).toBe(403);

    expect(h.logOut).not.toHaveBeenCalled();
    expect(h.message).not.toHaveBeenCalled();
  });

  it("非例外 URL 的 401 才登出：例外表只认 /login 与 /logout 两个后缀", async () => {
    const http = await freshHttp();

    await expectError(http, 401, "/api/system/agent/rollback");
    vi.advanceTimersByTime(DEDUP_MS + 1);   // 出窗，否则第二发被去重吃掉
    await expectError(http, 401, "/api/loggedin-users");   // 看着像 logout 但不是后缀
    expect(h.logOut).toHaveBeenCalledTimes(2);
  });

  it("其它状态码与无 response 的传输层错误都不算会话失效", async () => {
    const http = await freshHttp();

    for (const status of [500, 502, 422])
      expect((await expectError(http, status, "/api/containers")).response.status).toBe(status);

    const { adapter } = rejectAdapter(
      config => new AxiosError("timeout of 10000ms exceeded", "ETIMEDOUT", config, {})
    );
    const timeout = await http
      .request<any>("get", "/api/containers", {}, { adapter })
      .catch((e: any) => e);
    expect(timeout.response).toBeUndefined();
    expect(timeout.isCancelRequest).toBe(false);
    expect(h.logOut).not.toHaveBeenCalled();
  });
});

describe("取消请求标记", () => {
  it("axios 取消错误标 isCancelRequest=true 且不登出；普通失败标 false", async () => {
    const http = await freshHttp();

    const { adapter } = rejectAdapter(config => new CanceledError("请求已取消", config));
    const cancelled = await http
      .request<any>("get", "/api/containers", {}, { adapter })
      .catch((e: any) => e);
    expect(cancelled.isCancelRequest).toBe(true);
    expect(h.logOut).not.toHaveBeenCalled();

    const failed = await expectError(http, 500, "/api/containers");
    expect(failed.isCancelRequest).toBe(false);
  });
});

describe("URL 后缀匹配的脆弱面（登记，不作为正确性背书）", () => {
  it("带查询串的 /login?x=1 不再命中例外表 → 401 会被当成会话失效登出", async () => {
    const http = await freshHttp();
    const { adapter } = captureAdapter({ status: 401, data: {} });

    await http.request("post", "/api/login?redirect=/welcome", {}, { adapter }).catch(() => undefined);

    // 当前 api 层不会给登录/登出拼查询串，所以线上不触发；一旦有人加 query 就是行为漂移。
    expect(h.logOut).toHaveBeenCalledTimes(1);
  });
});

describe("getToken 抛异常时的爆炸半径（现状锁，缺陷已登记）", () => {
  it("token 读取抛错 → 非登录请求直接 reject，且不会触发自动登出（用户被卡在坏 cookie 上）", async () => {
    h.getToken.mockImplementation(() => {
      throw new SyntaxError("Unexpected token o in JSON at position 1");   // cookie 是坏 JSON
    });
    const http = await freshHttp();
    const { seen, adapter } = captureAdapter({ status: 200, data: {} });

    const err = await http.request<any>("get", "/api/containers", {}, { adapter }).catch((e: any) => e);

    expect(err).toBeInstanceOf(SyntaxError);
    expect(seen).toHaveLength(0);          // 请求根本没发出去
    expect(h.logOut).not.toHaveBeenCalled();   // 401 永不到来 → 「自动登出」这条自愈路径失效
    // 现状问题：utils/auth.ts 的 getToken 对 cookie 做裸 JSON.parse，坏值不自愈；
    // 期望语义是「解析失败视为未登录 → 清 cookie 并回落」，让 401/登出路径重新接管。
  });
});
