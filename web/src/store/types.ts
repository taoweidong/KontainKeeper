import type { RouteRecordName } from "vue-router";

import type { KkMenuMeta } from "@/utils/kkMenu";

export type cacheType = {
  mode: string;
  name?: RouteRecordName;
};

export type positionType = {
  startIndex?: number;
  length?: number;
};

export type appType = {
  sidebar: {
    opened: boolean;
    withoutAnimation: boolean;
    // 判断是否手动点击Collapse
    isClickCollapse: boolean;
  };
  layout: string;
  device: string;
  viewportSize: { width: number; height: number };
};

export type multiType = {
  path: string;
  name: string;
  meta: any;
  query?: object;
  params?: object;
};

export type setType = {
  title: string;
  fixedHeader: boolean;
  hiddenSideBar: boolean;
};

export type userType = {
  avatar?: string;
  username?: string;
  nickname?: string;
  roles?: Array<string>;
  permissions?: Array<string>;
  /** RBAC 允许菜单（后端 /api/auth/menus 下发），不持久化 */
  menus?: Array<KkMenuMeta>;
  menusLoaded?: boolean;
  isRemembered?: boolean;
  loginDay?: number;
};
