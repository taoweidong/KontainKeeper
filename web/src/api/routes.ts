import { getAuthMenus, type KkMenuMeta } from "@/api/rbac";

type Result = {
  success: boolean;
  data: Array<any>;
};

/** 菜单装配入口（initRouter 每次登录/硬刷新都会调）。
 *
 * 2026-10-31 RBAC 二阶段：**菜单树完全由数据库下发**——后端按当前用户的角色
 * 返回可见菜单（含路由注册数据 path/component），未授权的菜单根本不出现在
 * 响应里，前端也就不会注册对应路由（直连 URL 落到 404，而不是渲染后再拦）。
 * 这里的职责只剩：平表组树 + 排序，交给 pure-admin 的 handleAsyncRoutes
 * 走标准动态路由注册（addAsyncRoutes 按 component 字符串解析组件，
 * 分组为 "Layout" 字面量）。
 *
 * fail-closed：接口失败返回空树——侧边栏为空、业务路由不注册（404），
 * 绝不退回全量菜单。
 */
export const getAsyncRoutes = (): Promise<Result> =>
  getAuthMenus()
    .then(res => ({ success: true, data: buildMenuTree(res.menus) as any[] }))
    .catch(() => ({ success: true, data: [] as Array<any> }));

/** 平表 → 路由树：兄弟按 rank 升序（稳定次序再按 name）；
 *  组件以字符串形态交给 addAsyncRoutes 解析（分组 "Layout"、叶子为 glob key）。 */
export function buildMenuTree(rows: KkMenuMeta[]): Array<any> {
  const byParent = new Map<string, KkMenuMeta[]>();
  [...rows]
    .sort((a, b) => a.rank - b.rank || a.name.localeCompare(b.name))
    .forEach(m => {
      const list = byParent.get(m.parent) ?? [];
      list.push(m);
      byParent.set(m.parent, list);
    });
  const build = (parent: string): Array<any> =>
    (byParent.get(parent) ?? []).map(m => {
      const children = build(m.name);
      const node: any = {
        path: m.path,
        name: m.name,
        meta: {
          title: m.title,
          ...(m.icon ? { icon: m.icon } : {}),
          rank: m.rank,
          showLink: m.show_link
        }
      };
      if (m.component) node.component = m.component;
      if (children.length) node.children = children;
      return node;
    });
  return build("");
}
