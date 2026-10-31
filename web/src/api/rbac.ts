import { http } from "@/utils/http";

/** RBAC（用户/角色/菜单，权限只控菜单）前端 API。
 *  契约见 server/src/kk_server/controllers/rbac.py；管理接口服务端要求 admin
 *  角色，前端只做展示控制（403 由 http 拦截器统一提示）。 */

export type KkMenuMeta = {
  /** 对应前端路由的 name（前后端关联主键） */
  name: string;
  /** 父菜单 name（顶级为空串） */
  parent: string;
  title: string;
  icon: string;
  rank: number;
  /** 路由 path（含 params 形态，如 /hosts/detail/:pod） */
  path: string;
  /** 组件视图 glob key（如 /src/views/host/monitor/index.vue）；分组为 "Layout" */
  component: string;
  /** 是否在侧边栏显示（false 的页面仍注册路由，如详情/趋势） */
  show_link: boolean;
};

export type AuthMenusResult = {
  roles: Array<string>;
  /** 当前角色可见的启用菜单（平表，按 parent 组树；未授权的菜单根本不下发） */
  menus: Array<KkMenuMeta>;
};

/** 当前用户：角色 + 可见菜单（initRouter 时拉取，fail-closed） */
export const getAuthMenus = () =>
  http.request<AuthMenusResult>("get", "/api/auth/menus");

// ---- 用户管理 ----

export type RbacUser = {
  username: string;
  role: string;
  role_label: string;
  enabled: boolean;
  created: number;
};

export const listUsers = () =>
  http.request<{ items: RbacUser[] }>("get", "/api/system/users");

export const createUser = (data: {
  username: string;
  password: string;
  role: string;
}) => http.request<{ ok: boolean }>("post", "/api/system/users", { data });

export const updateUser = (
  username: string,
  data: { role?: string; password?: string; enabled?: boolean }
) =>
  http.request<{ ok: boolean }>("put", `/api/system/users/${username}`, {
    data
  });

export const deleteUser = (username: string) =>
  http.request<{ ok: boolean }>("delete", `/api/system/users/${username}`);

// ---- 角色管理 ----

export type RbacRole = {
  name: string;
  label: string;
  remark: string;
  built_in: boolean;
  created: number;
  menus: Array<string>;
  users: number;
};

export const listRoles = () =>
  http.request<{ items: RbacRole[] }>("get", "/api/system/roles");

export const createRole = (data: { name: string; label: string; remark: string }) =>
  http.request<{ ok: boolean }>("post", "/api/system/roles", { data });

export const updateRole = (
  name: string,
  data: { label?: string; remark?: string; menus?: Array<string> }
) =>
  http.request<{ ok: boolean }>("put", `/api/system/roles/${name}`, { data });

export const deleteRole = (name: string) =>
  http.request<{ ok: boolean }>("delete", `/api/system/roles/${name}`);

// ---- 菜单管理 ----

export type RbacMenu = {
  name: string;
  parent: string;
  title: string;
  icon: string;
  rank: number;
  path: string;
  component: string;
  show_link: boolean;
  enabled: boolean;
  built_in: boolean;
};

export const listMenus = () =>
  http.request<{ items: RbacMenu[] }>("get", "/api/system/menus");

export const updateMenu = (
  name: string,
  data: {
    title?: string;
    icon?: string;
    rank?: number;
    show_link?: boolean;
    enabled?: boolean;
  }
) =>
  http.request<{ ok: boolean }>("put", `/api/system/menus/${name}`, { data });
