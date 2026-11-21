import { defineStore } from "pinia";
import {
  type userType,
  store,
  router,
  resetRouter,
  routerArrays,
  storageLocal
} from "../utils";
import { type UserResult, getLogin, logoutApi } from "@/api/user";
import { useMultiTagsStoreHook } from "./multiTags";
import { type DataInfo, setToken, removeToken, userKey } from "@/utils/auth";
import { clearPolls } from "@/utils/kkPoll";

export const useUserStore = defineStore("pure-user", {
  state: (): userType => ({
    // 头像
    avatar: storageLocal().getItem<DataInfo<number>>(userKey)?.avatar ?? "",
    // 用户名
    username: storageLocal().getItem<DataInfo<number>>(userKey)?.username ?? "",
    // 昵称
    nickname: storageLocal().getItem<DataInfo<number>>(userKey)?.nickname ?? "",
    // 页面级别权限
    roles: storageLocal().getItem<DataInfo<number>>(userKey)?.roles ?? [],
    // 按钮级别权限
    permissions:
      storageLocal().getItem<DataInfo<number>>(userKey)?.permissions ?? [],
    // 是否勾选了登录页的免登录
    isRemembered: false,
    // 登录页的免登录存储几天，默认7天
    loginDay: 7
  }),
  actions: {
    /** 存储头像 */
    SET_AVATAR(avatar: string) {
      this.avatar = avatar;
    },
    /** 存储用户名 */
    SET_USERNAME(username: string) {
      this.username = username;
    },
    /** 存储昵称 */
    SET_NICKNAME(nickname: string) {
      this.nickname = nickname;
    },
    /** 存储角色 */
    SET_ROLES(roles: Array<string>) {
      this.roles = roles;
    },
    /** 存储按钮级别权限 */
    SET_PERMS(permissions: Array<string>) {
      this.permissions = permissions;
    },
    /** 存储是否勾选了登录页的免登录 */
    SET_ISREMEMBERED(bool: boolean) {
      this.isRemembered = bool;
    },
    /** 设置登录页的免登录存储几天 */
    SET_LOGINDAY(value: number) {
      this.loginDay = Number(value);
    },
    /** 登入 */
    async loginByUsername(data) {
      return new Promise<UserResult>((resolve, reject) => {
        getLogin(data)
          .then(data => {
            if (data?.success) setToken(data.data);
            resolve(data);
          })
          .catch(error => {
            reject(error);
          });
      });
    },
    /** 登出：先尽力吊销服务端会话（QR29-W1），再清理本地状态。
     *  logoutApi 自吞错误且 fire-and-forget：token 已不可信，本地清理不能被
     *  网络问题阻塞；服务端会话删不掉时由 12h TTL 兜底。 */
    logOut() {
      logoutApi();
      // 全局停表兜底（QR1120-W31）：kkPoll 注释宣称的登出清理此前并未接线，
      // 只靠组件卸载的作用域清理——keep-alive 缓存页的轮询在登出后仍会带着
      // 已吊销的 token 打后端。这里显式清掉全部在跑轮询。
      clearPolls();
      this.username = "";
      this.roles = [];
      this.permissions = [];
      removeToken();
      useMultiTagsStoreHook().handleTags("equal", [...routerArrays]);
      resetRouter();
      // 会话过期带现场回跳（QR1122-W18）：401 登出后登录页读 redirect 回原页，
      // 运维不再丢失 500 台勾选 / 正在看的详情。登录页自身不附 redirect 防套娃；
      // 次序不变量（QR29-W1）不受影响——push 仍是最后一步。
      const cur = router.currentRoute?.value?.fullPath || "";
      const redirect = cur && !cur.startsWith("/login") ? cur : "";
      router.push(
        redirect ? { path: "/login", query: { redirect } } : "/login"
      );
    }
    // handRefreshToken 已删（QR1120-W31）：后端无 refresh 接口、全仓无调用方，
    // http 拦截器对 401 统一清 token 跳登录，死分支不再保留。
  }
});

export function useUserStoreHook() {
  return useUserStore(store);
}
