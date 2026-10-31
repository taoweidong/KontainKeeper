import { getAuthMenus } from "@/api/rbac";
import { useUserStoreHook } from "@/store/modules/user";

type Result = {
  success: boolean;
  data: Array<any>;
};

/** 菜单装配入口（initRouter 每次刷新/登录都会调）。
 *
 * 2026-10-31 RBAC：业务路由仍全部静态声明（modules/*.ts，组件真实引用），
 * 但**可见性来自后端**——这里拉取当前用户的角色与允许菜单集合存入 user store，
 * 由 permission store 在 handleWholeMenus 里过滤、路由守卫据此拦截直连 URL。
 *
 * 路由记录本身仍返回空数组：不把后端数据当路由源（组件映射、别名、
 * params 路由都留在静态声明里），后端只回答「谁能看哪些菜单」。
 *
 * fail-closed：接口失败按「一个菜单都不给」处理（menusLoaded=true + 空集合），
 * 让侧边栏空掉并 toast 报错，而不是退回全量——把权限特性降级成摆设比报错更糟。
 */
export const getAsyncRoutes = (): Promise<Result> =>
  getAuthMenus()
    .then(res => {
      const store = useUserStoreHook();
      store.SET_ROLES(res.roles);
      store.SET_MENUS(res.menus);
      return { success: true, data: [] as Array<any> };
    })
    .catch((error: any) => {
      // 401 = 会话过期：拦截器已在走 logOut + 跳登录，这里不做 fail-closed
      // （menusLoaded 保持 false，避免登录跳转链上再插一道 403）
      if (error?.response?.status === 401) {
        return { success: true, data: [] as Array<any> };
      }
      // 其余失败 fail-closed：拉不到就当作「一个菜单都不给」，侧边栏空 + toast
      useUserStoreHook().SET_MENUS([]);
      return { success: true, data: [] as Array<any> };
    });
