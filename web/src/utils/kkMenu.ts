/** 菜单级 RBAC 的纯函数工具（前后端契约见 server controllers/rbac.py）。
 *
 * 后端在 /api/auth/menus 下发**允许集合**（含每项的展示元数据），前端不做任何
 * 授权判断的“或”运算——集合之外一律不渲染、不放过（fail-closed）。
 * 这里只做三件纯事：收集受控路由名、按允许集过滤菜单树、把后端元数据合并进
 * 静态路由对象。全部无副作用，输入不被修改（调用方先 cloneDeep）。
 */

/** 后端下发的菜单项（/api/auth/menus 的 menus 元素） */
export interface KkMenuMeta {
  /** 对应前端路由的 name（静态路由 modules/*.ts） */
  name: string;
  title: string;
  icon: string;
  rank: number;
  /** 是否在侧边栏显示（show_link=0 的页面仍可路由跳转，如详情/趋势） */
  show_link: boolean;
}

/** 收集路由树里所有节点的 name：受控集合 = modules/*.ts 声明的业务路由。 */
export function collectRouteNames(tree: Array<{ name?: string; children?: any[] }>): Set<string> {
  const out = new Set<string>();
  const walk = (nodes: any[]) => {
    for (const n of nodes || []) {
      if (n?.name) out.add(n.name);
      if (n?.children?.length) walk(n.children);
    }
  };
  walk(tree);
  return out;
}

/** 按允许集过滤菜单树。
 *  叶子节点：名字在允许集内才保留；分组节点：**至少有一个可见子项才保留**
 *  ——组名在集合里但子项全被过滤时整组隐藏（侧边栏不留空壳分组；角色页的
 *  树勾选带级联，正常授权不会出现这种孤儿分组，这里兜住手工构造的数据）。 */
export function filterMenusByAllowed<
  T extends { name?: string; children?: T[] }
>(tree: T[], allowed: Set<string>): T[] {
  const keep = (node: T): boolean => {
    if (node.children?.length) return node.children.some(keep);
    return !!node.name && allowed.has(node.name);
  };
  const walk = (nodes: T[]): T[] =>
    (nodes || [])
      .filter(keep)
      .map(node =>
        node.children?.length
          ? { ...node, children: walk(node.children) }
          : node
      );
  return walk(tree);
}

/** 把后端下发的展示元数据合并进路由树（title/icon/rank/showLink）。
 *  菜单管理页改的标题、图标、排序、显隐在这里落到侧边栏；路由 name 是主键，
 *  找不到元数据的节点保持静态声明原值（例如 remaining 里的页面）。 */
export function applyMenuMeta<
  T extends { name?: string; meta?: Record<string, any>; children?: T[] }
>(tree: T[], menus: KkMenuMeta[]): T[] {
  const metaMap = new Map(menus.map(m => [m.name, m]));
  const walk = (nodes: T[]): T[] =>
    (nodes || []).map(node => {
      const m = node.name ? metaMap.get(node.name) : undefined;
      const next: T = m
        ? {
            ...node,
            meta: {
              ...node.meta,
              ...(m.title ? { title: m.title } : {}),
              ...(m.icon ? { icon: m.icon } : {}),
              ...(m.rank !== undefined ? { rank: m.rank } : {}),
              showLink: m.show_link
            }
          }
        : node;
      return next.children?.length
        ? { ...next, children: walk(next.children) }
        : next;
    });
  return walk(tree);
}
