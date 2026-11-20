import { http } from "@/utils/http";

export type UserResult = {
  success: boolean;
  data: {
    /** 头像 */
    avatar: string;
    /** 用户名 */
    username: string;
    /** 昵称 */
    nickname: string;
    /** 当前登录用户的角色 */
    roles: Array<string>;
    /** 按钮级别权限 */
    permissions: Array<string>;
    /** `token` */
    accessToken: string;
    /** 用于调用刷新`accessToken`的接口时所需的`token` */
    refreshToken: string;
    /** `accessToken`的过期时间（格式'xxxx/xx/xx xx:xx:xx'） */
    expires: Date;
  };
};

/**
 * 登录：对接 KontainKeeper 后端 POST /api/login
 * 后端返回 {token, username, roles}，这里适配成 pure-admin 期望的 {success, data: {...}}
 */
export const getLogin = (data?: object) => {
  return http
    .request<{ token: string; username: string; roles?: Array<string> }>(
      "post",
      "/api/login",
      { data }
    )
    .then(res => {
      const expires = new Date(Date.now() + 12 * 3600 * 1000);
      return {
        success: true,
        data: {
          avatar: "",
          username: res.username,
          nickname: res.username,
          // RBAC（2026-10-31）：真实角色来自后端；旧后端无此字段时按 admin 兜底
          roles: res.roles ?? (["admin"] as string[]),
          permissions: ["*:*:*"] as string[],
          accessToken: res.token,
          refreshToken: res.token,
          expires
        }
      } as UserResult;
    });
};

/**
 * 登出：吊销服务端会话（QR29-W1）。后端 POST /api/logout 会删除会话并落一条
 * logout 审计，此后该 token 立即失效（此前前端只清本地，token 在 12h TTL 内一直可用）。
 * 失败不阻断本地登出：token 已不可信，本地清理必须完成；网络断开时由服务端
 * 会话 TTL 兜底，所以这里吞掉一切错误、永不 reject。
 */
export const logoutApi = (): Promise<{ ok: boolean } | undefined> =>
  http.request<{ ok: boolean }>("post", "/api/logout").catch(() => undefined);

// refreshTokenApi 已删（QR1120-W31）：后端无刷新接口、全仓无调用方。
// token 12h 过期后 401 由 http 响应拦截器统一清 token 跳登录页。
