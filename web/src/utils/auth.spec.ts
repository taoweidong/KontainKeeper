/** utils/auth.ts 的单元测试：会话的落盘与读回（cookie + localStorage 双写）。
 *
 *  锁住的语义：
 *  - `getToken` 优先读 cookie（`authorized-token`），无 cookie 时回落 localStorage 的
 *    `user-info`——**回落体没有 accessToken 字段**，所以「过期后只剩本地画像」的会话
 *    拿不到 token，请求会裸奔到 401（http 拦截器依赖这条形状）。
 *  - `setToken` 必须同时写 cookie（含过期属性）与 localStorage（含 roles/permissions 画像），
 *    `isRemembered` 决定 `multiple-tabs` 是否带 loginDay 过期。
 *  - `removeToken` 要清得干净且幂等，否则登出后刷新会「复活」半个会话。
 *  - `formatToken` 的 `Bearer ` 字面量是后端 `current_user` 的解析前提。
 *  - `hasPerms` 的 `*:*:*` 全权限短路（登录适配层产出的就是这个值）。
 *  js-cookie 与 localStorage 都需要浏览器 API → jsdom。
 */
// @vitest-environment jsdom
import Cookies from "js-cookie";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const h = vi.hoisted(() => ({
  store: {
    isRemembered: false,
    loginDay: 7,
    permissions: [] as string[],
    SET_AVATAR: vi.fn(),
    SET_USERNAME: vi.fn(),
    SET_NICKNAME: vi.fn(),
    SET_ROLES: vi.fn(),
    SET_PERMS: vi.fn()
  }
}));

vi.mock("@/store/modules/user", () => ({ useUserStoreHook: () => h.store }));

import { storageLocal } from "@pureadmin/utils";
import {
  TokenKey,
  formatToken,
  getToken,
  hasPerms,
  multipleTabsKey,
  removeToken,
  setToken,
  userKey
} from "./auth";

const NOW = new Date("2026-04-30T08:00:00Z");
const FULL = {
  accessToken: "at-1",
  refreshToken: "rt-1",
  expires: new Date(NOW.getTime() + 12 * 3600 * 1000),
  avatar: "a.png",
  username: "admin",
  nickname: "管理员",
  roles: ["admin"],
  permissions: ["*:*:*"]
};

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(NOW);
  removeToken();
  storageLocal().clear();
  Object.assign(h.store, { isRemembered: false, loginDay: 7, permissions: [] });
  for (const k of ["SET_AVATAR", "SET_USERNAME", "SET_NICKNAME", "SET_ROLES", "SET_PERMS"] as const)
    h.store[k].mockClear();
});

afterEach(() => {
  vi.useRealTimers();
});

describe("formatToken", () => {
  it("Bearer 前缀逐字（后端按这个拆 Authorization）", () => {
    expect(formatToken("abc.def.ghi")).toBe("Bearer abc.def.ghi");
    expect(formatToken("")).toBe("Bearer ");   // 现状：空串也拼前缀，非空判定在调用方
  });
});

describe("setToken：双写与过期", () => {
  it("cookie 存三件套（expires 为毫秒时间戳），localStorage 存画像，画像同步进 store", () => {
    setToken(FULL);

    expect(JSON.parse(Cookies.get(TokenKey)!)).toEqual({
      accessToken: "at-1",
      expires: NOW.getTime() + 12 * 3600 * 1000,
      refreshToken: "rt-1"
    });
    // user-info 里没有 accessToken —— 这正是 getToken 回落体的形状来源
    expect(storageLocal().getItem(userKey)).toEqual({
      refreshToken: "rt-1",
      expires: NOW.getTime() + 12 * 3600 * 1000,
      avatar: "a.png",
      username: "admin",
      nickname: "管理员",
      roles: ["admin"],
      permissions: ["*:*:*"]
    });
    expect(h.store.SET_AVATAR).toHaveBeenCalledWith("a.png");
    expect(h.store.SET_USERNAME).toHaveBeenCalledWith("admin");
    expect(h.store.SET_NICKNAME).toHaveBeenCalledWith("管理员");
    expect(h.store.SET_ROLES).toHaveBeenCalledWith(["admin"]);
    expect(h.store.SET_PERMS).toHaveBeenCalledWith(["*:*:*"]);
  });

  it("缺少 username/roles 时不覆盖画像，沿用已有 localStorage（登录重试不许把人清成白屏）", () => {
    setToken(FULL);
    h.store.SET_USERNAME.mockClear();

    setToken({ accessToken: "at-2", refreshToken: "rt-2", expires: new Date(NOW.getTime() + 1000) });

    expect(h.store.SET_USERNAME).toHaveBeenCalledWith("admin");   // 沿用旧画像
    expect(JSON.parse(Cookies.get(TokenKey)!).accessToken).toBe("at-2");   // token 仍是新写入的
  });

  it("multiple-tabs 随 isRemembered 落盘；勾选时带 loginDay，未勾选走会话 cookie", () => {
    setToken(FULL);
    expect(Cookies.get(multipleTabsKey)).toBe("true");

    h.store.isRemembered = true;
    h.store.loginDay = 3;
    setToken({ ...FULL, accessToken: "at-3" });
    expect(Cookies.get(multipleTabsKey)).toBe("true");
  });

  it("expires 不可解析（Invalid Date → NaN）时降级为会话 cookie，体内 expires 被序列化成 null", () => {
    setToken({ ...FULL, expires: new Date("not-a-date") as any });

    expect(Cookies.get(TokenKey)).toContain("at-1");   // 仍然是写了的
    // JSON.stringify 把 NaN 写成 null：读回后「过期时间不可解析」与「永不过期」难以区分（已登记缺陷）
    expect(JSON.parse(Cookies.get(TokenKey)!).expires).toBeNull();
  });

  it("expires 是过去时刻 → cookie 立即不可读，getToken 只剩没有 accessToken 的画像回落", () => {
    setToken({ ...FULL, expires: new Date(NOW.getTime() - 1000) });

    expect(Cookies.get(TokenKey)).toBeUndefined();
    const back = getToken();
    expect(back?.accessToken).toBeUndefined();   // 请求会裸奔到 401 → 统一登出
    expect(back?.username).toBe("admin");        // 画像还在：「看着像登录着，其实没 token」
  });
});

describe("getToken：读回的三态", () => {
  it("两侧都空 → null；有 cookie 时优先 cookie，忽略 localStorage 的旧画像", () => {
    expect(getToken()).toBeNull();   // storageLocal 的缺键语义，调用方只能靠可选链

    Cookies.set(TokenKey, JSON.stringify({ accessToken: "from-cookie", expires: 1, refreshToken: "r" }));
    storageLocal().setItem(userKey, { accessToken: "from-local", roles: [] });
    expect(getToken().accessToken).toBe("from-cookie");
  });

  it("cookie 是坏 JSON 时抛异常（现状锁：无 try/catch，会波及 http 请求拦截器）", () => {
    Cookies.set(TokenKey, "{不是 JSON");

    expect(() => getToken()).toThrow();
    // 期望语义应是「坏 cookie 视为未登录并回落/清理」。抛点落在请求拦截器里会让
    // 每个接口请求直接 reject（已登记缺陷，本轮不改生产码）。
  });
});

describe("removeToken：清得干净且幂等", () => {
  it("清 cookie + localStorage 双侧，之后 getToken 为 null；重复清理不抛", () => {
    setToken({
      accessToken: "at-1",
      refreshToken: "rt-1",
      expires: new Date(NOW.getTime() + 3600_000),
      username: "admin",
      roles: ["admin"]
    });
    expect(getToken()).toBeTruthy();

    removeToken();

    expect(Cookies.get(TokenKey)).toBeUndefined();
    expect(Cookies.get(multipleTabsKey)).toBeUndefined();
    expect(storageLocal().getItem(userKey)).toBeNull();
    expect(getToken()).toBeNull();

    removeToken();   // 登出被两条路径同时触达
    expect(getToken()).toBeNull();
  });
});

describe("hasPerms：按钮级权限", () => {
  it("*:*:* 单元素时全通过（登录适配层的产出）", () => {
    h.store.permissions = ["*:*:*"];
    expect(hasPerms("anything:at:all")).toBe(true);
    expect(hasPerms(["a", "b"])).toBe(true);
  });

  it("字符串命中与未命中；数组要求全部命中；空权限/入参为空/未水合都判否而不抛", () => {
    h.store.permissions = ["host:read", "cmd:exec"];
    expect(hasPerms("host:read")).toBe(true);
    expect(hasPerms("host:write")).toBe(false);
    expect(hasPerms(["host:read", "cmd:exec"])).toBe(true);
    expect(hasPerms(["host:read", "cmd:delete"])).toBe(false);

    h.store.permissions = [];
    expect(hasPerms("host:read")).toBe(false);

    h.store.permissions = undefined as any;
    expect(hasPerms("host:read")).toBe(false);

    h.store.permissions = ["host:read"];
    expect(hasPerms(undefined as any)).toBe(false);
    expect(hasPerms("")).toBe(false);
  });
});
