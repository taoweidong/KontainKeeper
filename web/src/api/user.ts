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

export type RefreshTokenResult = {
  success: boolean;
  data: {
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
 * 后端返回 {token, username}，这里适配成 pure-admin 期望的 {success, data: {...}}
 */
export const getLogin = (data?: object) => {
  return http
    .request<{ token: string; username: string }>("post", "/api/login", {
      data
    })
    .then(res => {
      const expires = new Date(Date.now() + 12 * 3600 * 1000);
      return {
        success: true,
        data: {
          avatar: "",
          username: res.username,
          nickname: res.username,
          roles: ["admin"] as string[],
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

/**
 * 刷新 token：后端暂无此接口。
 * 12h 过期后 401 → 这里抛错 → http 拦截器会清 token 并跳登录页。
 *
 * 入参签名故意与原模板一致（可接收 refresh token）：用户端可能传入，
 * 真正的实现等后端补接口再补——签名稳定可避免 store 那侧的改动连锁。
 */
export const refreshTokenApi = (_?: object): Promise<RefreshTokenResult> => {
  return Promise.reject(new Error("refresh-token not supported"));
};
