/** 根容器：业务路由全部改为数据库下发（RBAC）后，这里只剩一个隐藏的
 *  「/」记账容器——handleAsyncRoutes 把动态菜单拍平后 push 进
 *  router.options.routes[0].children 并用 addRoute 重注册（pure-admin 机制），
 *  routes[0] 必须是 path 为 "/" 的路由。本体不渲染任何业务页面（children 空、
 *  showLink:false），redirect 指向 /welcome 兜底直访。
 *
 *  注意 rank:-1 让 ascending() 把它排到 constantRoutes 首位。
 */
const Layout = () => import("@/layout/index.vue");

export default {
  path: "/",
  name: "Root",
  component: Layout,
  redirect: "/welcome",
  meta: {
    title: "Root",
    icon: "",
    showLink: false,
    rank: -1
  },
  children: []
};
