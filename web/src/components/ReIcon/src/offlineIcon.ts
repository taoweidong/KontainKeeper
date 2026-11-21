// 这里存放本地图标，在 src/layout/index.vue 文件中加载，避免在首启动加载
import { getSvgInfo } from "@pureadmin/utils";
import { addIcon } from "@iconify/vue/dist/offline";

// https://icon-sets.iconify.design/ep/?keyword=ep
import EpHomeFilled from "~icons/ep/home-filled?raw";
import EpMonitor from "~icons/ep/monitor?raw";
import EpOperation from "~icons/ep/operation?raw";
import EpDocument from "~icons/ep/document?raw";
import EpOdometer from "~icons/ep/odometer?raw";
import EpSetting from "~icons/ep/setting?raw";

// https://icon-sets.iconify.design/ri/?keyword=ri
import RiSearchLine from "~icons/ri/search-line?raw";
import RiInformationLine from "~icons/ri/information-line?raw";

const icons = [
  // Element Plus Icon: https://github.com/element-plus/element-plus-icons
  ["ep/home-filled", EpHomeFilled],
  // 菜单分组图标（QR1121）：与后端 models/_store_rbac.py 的内置菜单种子逐字对应。
  // 此前只注册了 home-filled，其余 5 个分组在侧边栏渲染为空白——种子声明了
  // icon 就该渲染，注册补齐即修复。菜单管理页的图标预览（KkMenuIcon）也认这份名单。
  ["ep/monitor", EpMonitor],
  ["ep/operation", EpOperation],
  ["ep/document", EpDocument],
  ["ep/odometer", EpOdometer],
  ["ep/setting", EpSetting],
  // Remix Icon: https://github.com/Remix-Design/RemixIcon
  ["ri/search-line", RiSearchLine],
  ["ri/information-line", RiInformationLine]
];

// 本地菜单图标，后端在路由的 icon 中返回对应的图标字符串并且前端在此处使用 addIcon 添加即可渲染菜单图标
icons.forEach(([name, icon]) => {
  addIcon(name as string, getSvgInfo(icon as string));
});

/** 已离线注册的菜单图标名单（QR1121-W5）：菜单管理页预览与 kkIcons.menuIconKnown
 *  的唯一真相。含 `:` 的字符串会走 IconifyIconOnline 运行时联网（离线部署红线），
 *  不在这份名单里的字符串一律按「未注册」呈现，绝不递给渲染链。 */
export const REGISTERED_MENU_ICONS: ReadonlySet<string> = new Set(
  icons.map(([name]) => name as string)
);
