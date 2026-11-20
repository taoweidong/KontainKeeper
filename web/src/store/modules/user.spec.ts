/** store/modules/user.ts 的单元测试：会话生命周期里最容易出事的两条动作。
 *
 *  锁住的语义：
 *  1. `logOut()` 的**次序不变量**：先尽力吊销服务端会话（`logoutApi()` fire-and-forget，
 *     不 await、失败也继续），再清本地状态 → `removeToken()` → 标签复位 → `resetRouter()`
 *     → `router.push("/login")`。次序写反（先清本地）会让 /api/logout 带着空 Authorization
 *     发出，服务端会话在 12h TTL 内一直可用（QR29-W1 的实质）；await 它则网络抖动时
 *     人会被卡在原地、本地会话清不掉。
 *  2. `loginByUsername` 的成功/失败两条路径：只有 `success` 为真才 `setToken`；
 *     失败必须把原始错误抛给调用方（登录页靠 errText 透出后端中文文案）。
 *  3. `logOut()` 必须调全局 `clearPolls()`（QR1120-W31）：keep-alive 缓存页的轮询
 *     不随登出卸载，不停表就会带着已吊销的 token 继续打后端。
 *  4. 状态水合与 setter：刷新后从 localStorage 的 user-info 复原画像，未登录时是空数组。
 *
 *  语义裁决（QR1120-W31）：原第 3 组锁 `handRefreshToken` 的两条用例已随死代码删除——
 *  后端无 refresh 接口、全仓无调用方，http 拦截器对 401 统一登出兜底，锁一个不存在的
 *  动作没有意义；`refreshTokenApi` 同删（api/user.spec.ts 同步裁决）。
 *  jsdom + 真 pinia；`@/router`、`@/utils/auth`、`@/api/user`、`./multiTags` 全部桩掉，
 *  否则会把整张路由/cookie/api 依赖图拖进来。
 */
// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from "vitest";
import { setActivePinia } from "pinia";

const h = vi.hoisted(() => ({
  order: [] as string[],
  logoutApi: vi.fn(),
  getLogin: vi.fn(),
  setToken: vi.fn(),
  removeToken: vi.fn(),
  routerPush: vi.fn(),
  resetRouter: vi.fn(),
  handleTags: vi.fn(),
  mem: {} as Record<string, any>,
  routerArrays: [{ path: "/welcome", name: "Welcome", meta: { title: "首页" } }]
}));

vi.mock("../utils", async () => {
  const { createPinia } =
    await vi.importActual<typeof import("pinia")>("pinia");
  return {
    store: createPinia(),
    routerArrays: h.routerArrays,
    constantMenus: [],
    router: {
      push: (to: string) => {
        h.order.push("router.push");
        h.routerPush(to);
      }
    },
    resetRouter: () => {
      h.order.push("resetRouter");
      h.resetRouter();
    },
    storageLocal: () => ({
      getItem: (k: string) => (k in h.mem ? h.mem[k] : null),
      setItem: (k: string, v: any) => {
        h.mem[k] = v;
      },
      removeItem: (k: string) => {
        delete h.mem[k];
      },
      clear: () => {
        for (const k of Object.keys(h.mem)) delete h.mem[k];
      }
    }),
    getConfig: () => ({}),
    responsiveStorageNameSpace: () => "kk-test"
  };
});

vi.mock("@/utils/auth", () => ({
  setToken: (...a: any[]) => h.setToken(...a),
  removeToken: () => {
    h.order.push("removeToken");
    h.removeToken();
  },
  userKey: "user-info",
  TokenKey: "authorized-token"
}));

vi.mock("@/api/user", () => ({
  getLogin: (...a: any[]) => h.getLogin(...a),
  logoutApi: () => {
    h.order.push("logoutApi");
    return h.logoutApi();
  }
}));

vi.mock("./multiTags", () => ({
  useMultiTagsStoreHook: () => ({
    handleTags: (...a: any[]) => {
      h.order.push("handleTags");
      h.handleTags(...a);
    }
  })
}));

import { store } from "../utils";
import { useUserStoreHook } from "./user";
// kkPoll 用真实实现（纯定时器模块，无 DOM/网络依赖）：clearPolls 的接线要真停表才算数
import { clearPolls, pollKeys, setPoll } from "@/utils/kkPoll";

beforeEach(() => {
  vi.clearAllMocks();
  h.order.length = 0;
  h.mem = {};
  clearPolls(); // 定时器是模块级全局，逐条清掉免相互依赖
  setActivePinia(store);
  useUserStoreHook().$reset(); // store 实例在本文件内共享，逐条复位免相互依赖
  h.logoutApi.mockResolvedValue(undefined); // 真实实现自吞错误
});

describe("logOut：次序与副作用", () => {
  it("吊销会话 → 清状态 → 清 token → 复位标签 → 复位路由 → 跳登录，一步不多一步不少", () => {
    const s = useUserStoreHook();
    s.username = "admin";
    s.roles = ["admin"];
    s.permissions = ["*:*:*"];

    s.logOut();

    expect(h.order).toEqual([
      "logoutApi",
      "removeToken",
      "handleTags",
      "resetRouter",
      "router.push"
    ]);
    expect(h.routerPush).toHaveBeenCalledWith("/login");
    expect(h.handleTags).toHaveBeenCalledWith("equal", h.routerArrays);
    expect(s.username).toBe("");
    expect(s.roles).toEqual([]);
    expect(s.permissions).toEqual([]);
  });

  it("logoutApi 还在途时就已完成全部本地清理（fire-and-forget，不许 await 网络）", () => {
    let resolveLogout!: (value?: unknown) => void;
    h.logoutApi.mockReturnValue(
      new Promise(r => {
        resolveLogout = r;
      })
    );

    useUserStoreHook().logOut();

    // 同步就把本地清完并跳走：网络再慢也不把人卡在旧会话里
    expect(h.order).toEqual([
      "logoutApi",
      "removeToken",
      "handleTags",
      "resetRouter",
      "router.push"
    ]);
    resolveLogout();
  });

  it("登出被重复触发不抛异常", () => {
    const s = useUserStoreHook();
    expect(() => {
      s.logOut();
      s.logOut();
    }).not.toThrow();
    expect(h.routerPush).toHaveBeenCalledTimes(2);
  });
});

describe("loginByUsername", () => {
  const okResult = {
    success: true,
    data: {
      accessToken: "tk",
      refreshToken: "tk",
      expires: new Date("2026-04-30T20:00:00Z"),
      username: "admin",
      roles: ["admin"],
      permissions: ["*:*:*"]
    }
  };

  it("成功：setToken 收到 data.data（会话写入的唯一入口），原样回传结果且不碰登出副作用", async () => {
    h.getLogin.mockResolvedValue(okResult);

    const res = await useUserStoreHook().loginByUsername({
      username: "admin",
      password: "x"
    } as any);

    expect(h.getLogin).toHaveBeenCalledWith({
      username: "admin",
      password: "x"
    });
    expect(h.setToken).toHaveBeenCalledTimes(1);
    expect(h.setToken.mock.calls[0][0]).toBe(okResult.data);
    expect(res).toBe(okResult);
    expect(h.handleTags).not.toHaveBeenCalled();
    expect(h.order).toEqual([]);
  });

  it("success 为假时不写 token，但仍 resolve（形状异常由调用方判定，store 不擅自失败）", async () => {
    h.getLogin.mockResolvedValue({
      success: false,
      data: { accessToken: "x" }
    });

    const res = await useUserStoreHook().loginByUsername({} as any);

    expect(h.setToken).not.toHaveBeenCalled();
    expect(res.success).toBe(false);
  });

  it("失败：错误原样 reject，且不落任何会话", async () => {
    const err = { response: { data: { detail: "用户名或密码错误" } } };
    h.getLogin.mockRejectedValue(err);

    await expect(useUserStoreHook().loginByUsername({} as any)).rejects.toBe(
      err
    );
    expect(h.setToken).not.toHaveBeenCalled();
  });
});

describe("logOut 的全局停表兜底（QR1120-W31）", () => {
  it("登出清掉全部在跑轮询：keep-alive 缓存页不卸载，不许带着已吊销 token 继续打后端", () => {
    const tick = vi.fn();
    setPoll("opt1120-a", tick, 1000);
    setPoll("opt1120-b", tick, 3000);
    expect(pollKeys().sort()).toEqual(["opt1120-a", "opt1120-b"]);

    useUserStoreHook().logOut();

    expect(pollKeys()).toEqual([]); // clearPolls 真的接线了，不是注释宣称
    expect(tick).not.toHaveBeenCalled();
  });

  it("没有在跑轮询时登出不抛异常（clearPolls 对空表无副作用）", () => {
    expect(pollKeys()).toEqual([]);
    expect(() => useUserStoreHook().logOut()).not.toThrow();
  });
});

describe("状态与 setter", () => {
  it("逐个 setter 写回对应字段，SET_LOGINDAY 强转数字且 0 不被兜成默认", () => {
    const s = useUserStoreHook();
    s.SET_AVATAR("a.png");
    s.SET_USERNAME("bob");
    s.SET_NICKNAME("鲍勃");
    s.SET_ROLES(["r1", "r2"]);
    s.SET_PERMS(["host:read"]);
    s.SET_ISREMEMBERED(true);
    s.SET_LOGINDAY("5" as any);

    expect([
      s.avatar,
      s.username,
      s.nickname,
      s.roles,
      s.permissions,
      s.isRemembered
    ]).toEqual(["a.png", "bob", "鲍勃", ["r1", "r2"], ["host:read"], true]);
    expect(s.loginDay).toBe(5);

    s.SET_LOGINDAY(0);
    expect(s.loginDay).toBe(0); // 0 是真值「永不过期」，不能被兜成 7
  });

  it("未登录时是空数组而不是 undefined；有 user-info 时水合画像（hasPerms 与菜单依赖）", () => {
    const s = useUserStoreHook();
    expect(s.roles).toEqual([]);
    expect(s.permissions).toEqual([]);
    expect(s.username).toBe("");
    expect(s.avatar).toBe("");
    expect(s.loginDay).toBe(7);

    h.mem["user-info"] = {
      username: "admin",
      avatar: "",
      nickname: "管理员",
      roles: ["admin"],
      permissions: ["*:*:*"]
    };
    s.$reset(); // state() 每次重建 store 时重新求值 → 复现「刷新后从 localStorage 复原」

    expect(s.username).toBe("admin");
    expect(s.roles).toEqual(["admin"]);
    expect(s.permissions).toEqual(["*:*:*"]);
    expect(s.nickname).toBe("管理员");
  });
});
