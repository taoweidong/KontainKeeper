/** api/user.ts 的单元测试：登录/登出这条链是全站唯一「前端形状适配后端形状」的地方。
 *
 *  锁住的语义：
 *  - getLogin 把后端 `{token, username}` 适配成 pure-admin 的 `{success, data:{...}}`，
 *    roles/permissions/accessToken/refreshToken/expires 全在这条路径上生成——任何一处
 *    漏掉都会让 setToken 写进残缺会话（登录后立刻 401 或直接白屏）。
 *  - logoutApi 自吞错误（`.catch(() => undefined)`）：后端 401/断网都不许让调用方抛异常，
 *    否则 logOut() 的本地清理会被网络问题打断（QR29-W1 的后半段）。
 *  - refreshTokenApi 恒 reject（后端无此接口），由 http 拦截器统一登出兜底。
 *  expires 用 fake timers 钉死时钟，断言「签发即 12h」而不是「大概 12h」。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const httpMock = vi.hoisted(() => ({ request: vi.fn() }));
vi.mock("@/utils/http", () => ({ http: httpMock }));

import { getLogin, logoutApi, refreshTokenApi } from "./user";

const NOW = new Date("2026-04-30T08:00:00Z");

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(NOW);
  httpMock.request.mockReset();
});

afterEach(() => {
  vi.useRealTimers();
});

describe("getLogin：后端 {token, username} → pure-admin {success, data}", () => {
  it("端点、体与完整适配形状：POST /api/login 凭据进 data；token 双用、昵称占位、roles/permissions 字面量、expires = 签发 + 12h", async () => {
    httpMock.request.mockResolvedValue({ token: "tk-9", username: "root" });

    await getLogin({ username: "root", password: "x" });

    expect(httpMock.request).toHaveBeenCalledWith("post", "/api/login", {
      data: { username: "root", password: "x" }
    }); // 凭据走 body，口令不许进 URL

    const res = await getLogin({ username: "root", password: "x" });
    expect(res.success).toBe(true);
    expect(res.data).toEqual({
      avatar: "",
      username: "root",
      nickname: "root", // 后端无昵称，用用户名占位
      roles: ["admin"],
      permissions: ["*:*:*"], // hasPerms 的全权限短路依赖这个字面量
      accessToken: "tk-9",
      refreshToken: "tk-9", // 后端只有一种 token
      expires: new Date(NOW.getTime() + 12 * 3600 * 1000)
    });
    expect(res.data.expires).toBeInstanceOf(Date);

    // 每次调用重算过期时刻（不共享上一个 Date 实例）
    vi.setSystemTime(new Date(NOW.getTime() + 60_000));
    const again = await getLogin({ username: "root", password: "x" });
    expect(again.data.expires.getTime() - res.data.expires.getTime()).toBe(
      60_000
    );
  });

  it("后端回空对象时不编造用户名与 token（缺字段原样缺着）", async () => {
    httpMock.request.mockResolvedValue({});
    const res = await getLogin({});

    expect(res.success).toBe(true); // 适配层不做成功判定，HTTP 2xx 就是成功
    expect(res.data.username).toBeUndefined();
    expect(res.data.accessToken).toBeUndefined();
  });

  it("HTTP 层的错误原样抛出（登录页要靠 errText 透出「用户名或密码错误」）", async () => {
    const err = {
      response: { data: { detail: "用户名或密码错误" } },
      message: "Request failed"
    };
    httpMock.request.mockRejectedValue(err);

    await expect(getLogin({ username: "a", password: "b" })).rejects.toBe(err);
  });
});

describe("logoutApi：永不 reject", () => {
  it("POST /api/logout 无 body（会话从 Authorization 认人）；401/断网/非 Error 拒绝值全吞成 undefined", async () => {
    httpMock.request.mockResolvedValue({ ok: true });
    await expect(logoutApi()).resolves.toEqual({ ok: true });
    expect(httpMock.request).toHaveBeenCalledWith("post", "/api/logout");

    for (const bad of [
      { response: { status: 401 }, message: "401" },
      new Error("Network Error"),
      "不是 Error 的拒绝值"
    ]) {
      httpMock.request.mockRejectedValue(bad);
      await expect(logoutApi()).resolves.toBeUndefined(); // 登出是 fire-and-forget
    }
  });
});

describe("refreshTokenApi：后端无刷新接口", () => {
  it("恒 reject 且不碰 http（避免挂起 Promise 的死分支，评审 P1-1）", async () => {
    await expect(refreshTokenApi({ refreshToken: "rt" })).rejects.toThrow(
      "refresh-token not supported"
    );
    expect(httpMock.request).not.toHaveBeenCalled();
  });
});
