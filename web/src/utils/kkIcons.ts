import type { FunctionalComponent, SVGAttributes } from "vue";
import { REGISTERED_MENU_ICONS } from "@/components/ReIcon/src/offlineIcon";

/** ~icons/* 的默认导出是 FunctionalComponent<SVGAttributes>（unplugin-icons
 *  types/vue3）——映射表用它而非宽泛的 Component，JSX 里直接作标签才有调用签名 */
type IconComponent = FunctionalComponent<SVGAttributes>;

/** 全站图标词汇表（QR1121）：业务页一律从这里导入，不再散写 `~icons/*`。
 *
 * 全部是 unplugin-icons 编译期离线图标（ep = Element Plus、ri = Remix），
 * 运行时零网络依赖——离线部署红线。选型语义：
 * - 状态/告警类图标只做「类别标记」，严重度仍由圆点四档（kk-state）承载，
 *   图标本身不着饱和色（14px 单色，颜色走 --el-text-color-secondary）。
 * - 主机类型三态（metal/vm/container）与采集项八项的映射表沉在这里，
 *   未知键无图标/原串兜底，后端加新项不会让前端崩。
 */

// 主机 / 总览
import IconHost from "~icons/ri/dashboard-2-line";
import IconMetal from "~icons/ri/server-line";
import IconVm from "~icons/ri/computer-line";
import IconContainer from "~icons/ep/box";

// 指标 / 采集项 / 详情页小节
import IconCpu from "~icons/ep/cpu";
import IconMem from "~icons/ri/ram-line";
import IconDisk from "~icons/ri/hard-drive-2-line";
import IconDiskIo from "~icons/ri/exchange-line";
import IconNet from "~icons/ep/connection";
import IconProc from "~icons/ri/list-ordered";
import IconSys from "~icons/ri/information-line";

// 命令 / 采集 / 审计
import IconCommand from "~icons/ri/terminal-box-line";
import IconCollect from "~icons/ri/pulse-line";
import IconAudit from "~icons/ri/file-list-3-line";

// 通用动作
import IconRefresh from "~icons/ep/refresh";
import IconRetry from "~icons/ep/refresh-right";
import IconSearch from "~icons/ri/search-line";
import IconDetail from "~icons/ep/view";
import IconTrend from "~icons/ep/trend-charts";
import IconBack from "~icons/ep/back";
import IconSend from "~icons/ep/position";
import IconPlus from "~icons/ep/plus";
import IconEdit from "~icons/ep/edit";
import IconDelete from "~icons/ep/delete";
import IconDownload from "~icons/ep/download";

// 版本与更新（update 页既有选型收编）
import IconUpgrade from "~icons/ep/arrow-up";
import IconUpload from "~icons/ep/upload";
import IconRollback from "~icons/ep/refresh-left";
import IconVersion from "~icons/ri/price-tag-3-line";

// 统计 / 系统管理
import IconStats from "~icons/ep/odometer";
import IconAlert from "~icons/ep/warning";
import IconOnline from "~icons/ep/circle-check";
import IconClock from "~icons/ep/clock";
import IconGrid from "~icons/ep/grid";
import IconDb from "~icons/ri/database-2-line";
import IconUser from "~icons/ep/user";
import IconRole from "~icons/ri/shield-user-line";
import IconMenu from "~icons/ep/menu";

export {
  IconHost,
  IconMetal,
  IconVm,
  IconContainer,
  IconCpu,
  IconMem,
  IconDisk,
  IconDiskIo,
  IconNet,
  IconProc,
  IconSys,
  IconCommand,
  IconCollect,
  IconAudit,
  IconRefresh,
  IconRetry,
  IconSearch,
  IconDetail,
  IconTrend,
  IconBack,
  IconSend,
  IconPlus,
  IconEdit,
  IconDelete,
  IconDownload,
  IconUpgrade,
  IconUpload,
  IconRollback,
  IconVersion,
  IconStats,
  IconAlert,
  IconOnline,
  IconClock,
  IconGrid,
  IconDb,
  IconUser,
  IconRole,
  IconMenu,
  REGISTERED_MENU_ICONS
};

/** 主机类型 → 图标（monitor 类型列）：v3 主机 host_type 可能为空，
 *  查不到就不渲染图标、只留文字，不伪造类别。 */
export const HOST_TYPE_ICONS: Record<string, IconComponent> = {
  metal: IconMetal,
  vm: IconVm,
  container: IconContainer
};

/** 采集项 → 图标（collect 面板 / monitor 采集弹窗）：键集与服务端
 *  COLLECT_ITEMS 白名单同口径，未知项无图标、文案原串兜底。 */
export const COLLECT_ITEM_ICONS: Record<string, IconComponent> = {
  cpu: IconCpu,
  mem: IconMem,
  disk: IconDisk,
  disk_io: IconDiskIo,
  net: IconNet,
  proc: IconProc,
  user: IconUser,
  sys: IconSys
};

/** 菜单 icon 字符串是否可离线渲染（QR1121-W5）：
 *  ① 含 `:` 会走 IconifyIconOnline 运行时联网——离线部署红线，恒 false；
 *  ② 不在 addIcon 注册名单里的字符串渲染为空白，按「未注册」呈现原串。
 *  菜单管理页的图标预览（KkMenuIcon）必须先过这道闸。 */
export function menuIconKnown(icon: string): boolean {
  return (
    typeof icon === "string" &&
    !icon.includes(":") &&
    REGISTERED_MENU_ICONS.has(icon)
  );
}
