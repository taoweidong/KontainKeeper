/** KontainKeeper 业务路由：5 个页面全部静态声明。

为什么把 `getAsyncRoutes` 改成返回空数组：服务端没有下发菜单的接口，
dev 阶段靠 vite-plugin-fake-server + mock/asyncRoutes.ts 兜底，prod 打包后
那个 fake server 不存在，会变成菜单渲染源（wholeMenus）为空、整页空白。
改为静态后 dev 与 prod 行为一致——菜单来自这些 modules/*.ts。
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
      }
    ]
  },
  {
    path: "/command",
    name: "CommandCenter",
    component: Layout,
    redirect: "/command/index",
    meta: {
      icon: "ep/operation",
      title: "命令中心",
      rank: 2
    },
    children: [
      {
        path: "/command/index",
        name: "CommandCenterIndex",
        component: () => import("@/views/command/index.vue"),
        meta: { title: "命令中心" }
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
  }
];