import { defineStore } from "pinia";
import {
  type cacheType,
  store,
  ascending,
  getKeyList,
  filterTree,
  constantMenus,
  filterNoPermissionTree,
  formatFlatteningRoutes
} from "../utils";
import { cloneDeep } from "@pureadmin/utils";
import { applyMenuMeta, filterMenusByAllowed } from "@/utils/kkMenu";
import { useUserStoreHook } from "./user";
import { useMultiTagsStoreHook } from "./multiTags";

export const usePermissionStore = defineStore("pure-permission", {
  state: () => ({
    // 静态路由生成的菜单
    constantMenus,
    // 整体路由生成的菜单（静态、动态）
    wholeMenus: [],
    // 整体路由（一维数组格式）
    flatteningRoutes: [],
    // 缓存页面keepAlive
    cachePageList: []
  }),
  actions: {
    /** 组装整体路由生成的菜单。
     *
     * 2026-10-31 RBAC：业务路由仍来自静态声明（constantMenus，组件真实引用），
     * 但**可见性以后端允许集合为准**（user store 的 menus，initRouter 时拉取）——
     * 先按允许集过滤、再把后端展示元数据（标题/图标/排序/显隐）合并进静态对象。
     * menusLoaded=false（尚未拉到）时不过滤：这是「登录前/装配前」的瞬态，
     * 路由守卫会在拉取完成后再做拦截，不会漏放行（见 router/index.ts）。
     */
    handleWholeMenus(routes: any[]) {
      const userStore = useUserStoreHook();
      let source: any[] = this.constantMenus as any[];
      if (userStore.menusLoaded) {
        source = applyMenuMeta(
          filterMenusByAllowed(cloneDeep(source) as any[], userStore.allowedMenuNames()),
          userStore.menus
        ) as any[];
      }
      this.wholeMenus = filterNoPermissionTree(
        filterTree(ascending(source.concat(routes)))
      );
      this.flatteningRoutes = formatFlatteningRoutes(
        source.concat(routes) as any
      );
    },
    /** 监听缓存页面是否存在于标签页，不存在则删除 */
    clearCache() {
      let cacheLength = this.cachePageList.length;
      const nameList = getKeyList(useMultiTagsStoreHook().multiTags, "name");
      while (cacheLength > 0) {
        nameList.findIndex(v => v === this.cachePageList[cacheLength - 1]) ===
          -1 &&
          this.cachePageList.splice(
            this.cachePageList.indexOf(this.cachePageList[cacheLength - 1]),
            1
          );
        cacheLength--;
      }
    },
    cacheOperate({ mode, name }: cacheType) {
      const delIndex = this.cachePageList.findIndex(v => v === name);
      switch (mode) {
        case "refresh":
          this.cachePageList = this.cachePageList.filter(v => v !== name);
          this.clearCache();
          break;
        case "add":
          this.cachePageList.push(name);
          break;
        case "delete":
          delIndex !== -1 && this.cachePageList.splice(delIndex, 1);
          this.clearCache();
          break;
      }
    },
    /** 清空缓存页面 */
    clearAllCachePage() {
      this.wholeMenus = [];
      this.cachePageList = [];
    }
  }
});

export function usePermissionStoreHook() {
  return usePermissionStore(store);
}
