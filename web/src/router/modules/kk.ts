/** KontainKeeper 业务路由：页面组件全部静态声明（组件真实引用、含 params 路由）。
 *
 * 2026-10-31 RBAC：路由注册仍是静态的（不把后端数据当路由源），但**菜单可见性
 * 来自后端**——/api/auth/menus 下发当前用户的允许集合（菜单表与这里的 name
 * 一一对应，种子见 models/_store_rbac.py），permission store 据此过滤侧边栏、
 * 路由守卫据此拦直连 URL（router/index.ts 的菜单级守卫）。
 * 因此：这里的 name 改名必须同步 _store_rbac.py 的内置菜单种子。
 */
const Layout = () => import("@/layout/index.vue");

export default [
  {
    path: "/hosts",
    name: "Hosts",
    component: Layout,
    redirect: "/hosts/monitor",
    meta: {
      icon: "ep/monitor",
      title: "主机管理",
      rank: 1
    },
    children: [
      {
        path: "/hosts/monitor",
        name: "HostMonitor",
        component: () => import("@/views/host/monitor/index.vue"),
        meta: { title: "主机总览" }
      },
      {
        path: "/hosts/detail/:pod",
        name: "HostDetail",
        component: () => import("@/views/host/detail/index.vue"),
        // 详情是「点列表某行进去」的二级页：不出现在侧边栏，但仍可路由跳转
        meta: { title: "主机详情", showLink: false }
      },
      {
        // 单机使用趋势（内存/磁盘百分比 + 告警上限）：与详情同款的二级页，
        // 从总览操作列与详情页头进入，不占侧边栏
        path: "/hosts/trend/:pod",
        name: "HostTrend",
        component: () => import("@/views/host/trend/index.vue"),
        meta: { title: "使用趋势", showLink: false }
      },
      {
        // 版本与更新（D2.4）：上传新版本后唯一的入口。与主机总览并列但不嵌在二级下，
        // 让运维点开「主机管理」就能直接看到，不会被「详情」二级页挡住。
        path: "/hosts/update",
        name: "HostUpdate",
        component: () => import("@/views/host/update/index.vue"),
        meta: { title: "版本与更新" }
      }
    ]
  },
  {
    path: "/command",
    name: "CommandCenter",
    component: Layout,
    redirect: "/command/shell",
    meta: {
      icon: "ep/operation",
      title: "命令中心",
      rank: 2
    },
    children: [
      {
        path: "/command/shell",
        name: "CommandShell",
        component: () => import("@/views/command/shell/index.vue"),
        // 旧单页路径（合并 tabs 时代）作为别名保留，旧书签可用
        alias: "/command/index",
        meta: { title: "命令面板" }
      },
      {
        path: "/command/collect",
        name: "CommandCollect",
        component: () => import("@/views/command/collect/index.vue"),
        meta: { title: "采集面板" }
      }
    ]
  },
  {
    path: "/audit",
    name: "Audit",
    component: Layout,
    redirect: "/audit/index",
    meta: {
      icon: "ep/document",
      title: "审计日志",
      rank: 3
    },
    children: [
      {
        path: "/audit/index",
        name: "AuditIndex",
        component: () => import("@/views/audit/index.vue"),
        meta: { title: "审计日志" }
      }
    ]
  },
  {
    // 系统统计（v4 方案 §7.3.7，rank 4）：把服务端与桥接的进程内计数器摊成读数页。
    // 排在最后——它是排障时才进的页面，不是日常操作入口。
    path: "/system",
    name: "System",
    component: Layout,
    redirect: "/system/index",
    meta: {
      icon: "ep/odometer",
      title: "系统统计",
      rank: 4
    },
    children: [
      {
        path: "/system/index",
        name: "SystemStats",
        component: () => import("@/views/system/index.vue"),
        meta: { title: "系统统计" }
      }
    ]
  },
  {
    // 系统管理（RBAC，rank 5）：仅 admin 角色可见（菜单表种子不含 operator）。
    // 用户/角色/菜单三页的接口服务端都挂 AdminUser 依赖，前端只是入口。
    path: "/rbac",
    name: "Rbac",
    component: Layout,
    redirect: "/rbac/users",
    meta: {
      icon: "ep/setting",
      title: "系统管理",
      rank: 5
    },
    children: [
      {
        path: "/rbac/users",
        name: "SysUser",
        component: () => import("@/views/system/user/index.vue"),
        meta: { title: "用户管理" }
      },
      {
        path: "/rbac/roles",
        name: "SysRole",
        component: () => import("@/views/system/role/index.vue"),
        meta: { title: "角色管理" }
      },
      {
        path: "/rbac/menus",
        name: "SysMenu",
        component: () => import("@/views/system/menu/index.vue"),
        meta: { title: "菜单管理" }
      }
    ]
  }
];
