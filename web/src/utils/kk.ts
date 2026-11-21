import type { HostSummary } from "@/api/containers";

/** KontainKeeper 业务页共用的格式化与状态映射。

后端所有时间字段都是秒级 Unix 时间戳（int），这里统一成中文可读格式。
状态语言是四档圆点（QR1121 收口，真相在 cmdStateClass/hostStateClass）：
在线/成功=绿，失败/告警=红，未终态（执行中/已下发）=琥珀，离线/等待=空心灰。
*/
/** zh-CN 时间格式化器模块级缓存（QR1120-W6）：toLocaleString 每次调用都新建
 *  Intl.DateTimeFormat，总览 100 行 × 10s 轮询是热路径。选项与原调用逐字相同
 *  （只传 hour12:false，日期/时间分量走 locale 默认），输出不变；
 *  trend/index.vue 的 FE-16 同款优化收口到这里。 */
const tsFmt = new Intl.DateTimeFormat("zh-CN", { hour12: false });

export const tsText = (ts?: number | null): string => {
  if (!ts) return "-";
  return tsFmt.format(new Date(ts * 1000));
};

export const ageText = (sec?: number | null): string => {
  if (sec === null || sec === undefined) return "-";
  if (sec < 60) return `${Math.max(0, Math.round(sec))} 秒前`;
  if (sec < 3600) return `${Math.floor(sec / 60)} 分钟前`;
  if (sec < 86400) return `${Math.floor(sec / 3600)} 小时前`;
  return `${Math.floor(sec / 86400)} 天前`;
};

/** 时长格式化（非相对时间）：用于运行时长、执行耗时等区间语义 */
export const durText = (sec?: number | null): string => {
  if (sec === null || sec === undefined) return "-";
  const s = Math.max(0, Math.round(sec));
  if (s < 60) return `${s} 秒`;
  if (s < 3600) return `${Math.floor(s / 60)} 分 ${s % 60} 秒`;
  if (s < 86400)
    return `${Math.floor(s / 3600)} 小时 ${Math.floor((s % 3600) / 60)} 分`;
  return `${Math.floor(s / 86400)} 天 ${Math.floor((s % 86400) / 3600)} 小时`;
};

export const mbText = (mb?: number | null): string => {
  if (mb === null || mb === undefined) return "-";
  return mb >= 1024 ? `${(mb / 1024).toFixed(2)} GB` : `${Math.round(mb)} MB`;
};

export const numText = (v?: number | null, digits = 1): string =>
  v === null || v === undefined || !Number.isFinite(Number(v))
    ? "-"
    : Number(v).toFixed(digits);

/** 百分比读数（QR1120-W14）：有限值带 %，没读到仍是 -（不渲染 "-%"）。
 *  总览计量条、HostPicker、趋势页头共用——原先有的列带 % 有的裸数字。 */
export const pctText = (v?: number | null, digits = 1): string => {
  const t = numText(v, digits);
  return t === "-" ? "-" : `${t}%`;
};

/** 计量条填充：钳到 0–100，坏值不画出越界的条（QR29-W5，QR1120-W15 抽到公共层）。
 *  NaN/Infinity 必须显式挡掉：Math.min/max 会透传 NaN，
 *  界面会渲染出字面 NaN，插件写坏指标即可触发。monitor/detail 共用。 */
export function fillPct(v: number | null | undefined): number {
  if (v === null || v === undefined || !Number.isFinite(v)) return 0;
  return Math.max(0, Math.min(100, Math.round(v)));
}

/** 请求失败时数字读数的占位（QR-W16）。0 在这些页面上的意思是「服务端报了 0」，
 *  拿它当「没读到」就是编数据：调用方必须能区分这两种状态，别拿 `?? 0` 代替。 */
export const nOr = (read: boolean, v: number): number | string =>
  read ? v : "—";

/** 命令执行耗时（毫秒 → 秒，两位小数）：执行历史与总览最近命令共用一种口径 */
export const elapsedText = (ms?: number | null): string =>
  ms === null || ms === undefined ? "-" : `${numText(ms / 1000, 2)} s`;

/** 统一的后端错误取词：优先取 FastAPI detail（后端文案是中文），其次 message。
 *  各页曾各自内联 `e?.response?.data?.detail ?? e?.message ?? e`，错误文案口径不一。
 *  detail 是数组时（FastAPI 422 校验错误：`[{loc,msg,type}]`）逐条取 msg 拼接，
 *  对象型降级 JSON——原样把数组/对象塞给 toast 会显示成 [object Object]（QR29-W11）。 */
export const errText = (e: any): string => {
  const detail = e?.response?.data?.detail;
  if (typeof detail === "string" && detail) return detail;
  if (Array.isArray(detail)) {
    return detail
      .map((d: any) => (typeof d?.msg === "string" ? d.msg : JSON.stringify(d)))
      .join("；");
  }
  if (detail && typeof detail === "object") return JSON.stringify(detail);
  if (typeof e?.message === "string" && e.message) return e.message;
  return String(e);
};

/** 命令状态 → Element Plus 标签类型 */
export const statusType = (
  s: string
): "success" | "danger" | "info" | "warning" | "primary" => {
  switch (s) {
    case "done":
      return "success";
    case "failed":
    case "timeout":
    case "lost":
      return "danger";
    case "running":
      return "primary";
    case "sent":
      return "warning";
    default:
      return "info";
  }
};

/** 触发浏览器下载一个 Blob。
 *
 * 不能用 `window.open(导出接口)`：后端导出走 Bearer 鉴权，地址栏带不上 token；
 * 也不能依赖 `Content-Disposition`——响应拦截器只返回 `response.data`，
 * 拿不到响应头，所以文件名由前端拼。
 */
export const downloadBlob = (blob: Blob, filename: string): void => {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.style.display = "none";
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  // 立即回收：晚一点（setTimeout）会让部分浏览器拿不到已开始的下载
  URL.revokeObjectURL(url);
};

/** 导出文件名的时间戳后缀：同一天多次导出不会互相覆盖。 */
export const fileStamp = (d = new Date()): string => {
  const p = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}-${p(d.getHours())}${p(d.getMinutes())}${p(d.getSeconds())}`;
};

export const statusLabel = (s: string): string => {
  return (
    {
      pending: "待下发",
      sent: "已下发",
      running: "执行中",
      done: "已完成",
      failed: "失败",
      timeout: "超时",
      lost: "结果丢失",
      queued: "已排队"
    }[s] || s
  );
};

/** 选机升级被跳过的原因 → 中文。键集与后端 `controllers/agent_update.py` 穷举的
 *  reason 一一对应（not_found / already_latest / in_flight / no_binary /
 *  bad_version / no_broker），未知原因原样回显以便排障。
 *  收口到这里的理由：原先「版本与更新」页与「主机详情」页各写一份，
 *  而 update 那份定义后从未被引用（QR-W9 的 eslint 红灯）。 */
export const upgradeSkipText = (reason?: string): string => {
  return (
    {
      not_found: "主机不存在",
      already_latest: "已是最新",
      in_flight: "已有升级在途",
      no_binary: "未上传任何版本",
      bad_version: "版本号无效",
      no_broker: "服务端未连 Broker"
    }[reason || ""] ||
    reason ||
    "未知原因"
  );
};

/** 磁盘告警阈值（%）：与服务端 store.DISK_ALERT_PCT 同一口径。前后端各一份
 *  是部署形态决定的（前端拿不到服务端模块），改口径必须两边同步（QR29-E.2）。 */
export const DISK_ALERT_PCT = 85;

/** 秒级当前时刻：轮询同步读数条与相对时间换算共用（原先三处内联，QR29-E.2）。 */
export const nowSec = (): number => Math.floor(Date.now() / 1000);

/** 命令内容预览的统一出口（QR29-E.2）：welcome 与命令中心原先各一套语义——
 *  一个 parse 字符串、一个不 parse，items 前缀一处「采集项:」一处「items:」。
 *  统一后：字符串先试 JSON.parse（D.4 之后列表接口已回解析结构，字符串只剩
 *  历史脏数据，解析失败原样展示）；items 结构统一「采集项: 」前缀，与
 *  「采集面板」的产品术语一致（一处可见文案变更，评审 §6.2 第 5 行的裁决）。 */
export function argvText(raw: unknown): string {
  let argv = raw as any;
  if (typeof argv === "string") {
    try {
      argv = JSON.parse(argv);
    } catch {
      return argv || "-";
    }
  }
  if (Array.isArray(argv)) return argv.join(" ");
  if (argv && typeof argv === "object") {
    if (Array.isArray(argv.items)) return "采集项: " + argv.items.join(", ");
    return JSON.stringify(argv);
  }
  return "-";
}

/** 离线要把服务端记的原因说出来：updating 是设计内的自更新窗口，不是故障，
 *  运维看到它不该去重启 Agent（B6 的语义此前在 UI 上完全丢失）。
 *  取值集合只有 online / updating / LWT 空，其余一律按「离线」呈现。 */
export const OFFLINE_REASON_LABEL: Record<string, string> = {
  updating: "更新中"
};

/** 在线三态判定只依赖这两个字段（QR1120-W10 收窄入参）：列表行 HostSummary
 *  与详情 HostDetail 同形可用，不必为详情页伪造一份 summary。 */
type HostStateInput = Pick<HostSummary, "online" | "status_reason">;

/** 在线/离线状态：空心点 = 离线（含自更新窗口的方点变体），实心 = 在线。
 *  monitor 总览与 update 升级页共用同一映射（QR-W19：两份实现已收敛到此）。 */
export function hostStateClass(row: HostStateInput): string {
  if (row.online) return "kk-state";
  return row.status_reason === "updating"
    ? "kk-state kk-state--updating"
    : "kk-state kk-state--idle";
}

export function hostStateText(row: HostStateInput): string {
  if (row.online) return "在线";
  return OFFLINE_REASON_LABEL[row.status_reason || ""] || "离线";
}

/** 在线三态 → el-tag 类型（QR1120-W10）：仍用标签词汇的页面（HostPicker 已选
 *  标签）与圆点词汇共用同一份三态判定，updating 不再被显成普通离线。 */
export function hostStateTagType(
  row: HostStateInput
): "success" | "warning" | "info" {
  if (row.online) return "success";
  return row.status_reason === "updating" ? "warning" : "info";
}

/** Broker 链路状态 → 圆点类名（QR1122-W12，system 统计与 welcome 共用）。
 *  四态分开说（QR-W16 红线）：没读数 ≠ 桥接未启动 ≠ 断开 ≠ 连着。
 *  「已断开」是告警语义给红点——它不是「离线场景」那种中性空心灰；
 *  未知/未启动保持空心（没探测到不是错误）。 */
export function brokerStateClass(
  hasStats: boolean,
  hasBroker: boolean,
  connected: boolean
): string {
  if (!hasStats || !hasBroker) return "kk-state kk-state--idle";
  return connected ? "kk-state" : "kk-state kk-state--bad kk-bad";
}

/** 轮询 keyed 合并（QR1120-W2）：按 pod 认人，实质字段未变的行保留旧对象引用，
 *  富单元格不必每 10s 全量重渲染（monitor/update 两页共用，可单测）。
 *
 *  两条比较口径：
 *  - age_sec 是随行就市的时钟读数（服务端 now - last_seen），每轮必变但不代表
 *    主机状态变化——比较时豁免；命中合并时**就地**补新值，对象引用不变，
 *    只有读它的那一格（最近心跳）触发细粒度更新。
 *  - caps 等对象字段每轮响应都是新引用（=== 恒 false），按 JSON 比较。
 *
 *  整表逐行未变且顺序一致时返回原数组引用，paged 切片与表格 diff 全部短路。 */
export function mergeHostRows<T extends { pod: string }>(
  prev: readonly T[],
  next: readonly T[]
): T[] {
  const byPod = new Map(prev.map(r => [r.pod, r]));
  let sameAll = prev.length === next.length;
  const merged = next.map((row, i) => {
    const old = byPod.get(row.pod) as (T & Record<string, any>) | undefined;
    if (old && sameDisplay(old, row)) {
      if ("age_sec" in row) (old as any).age_sec = (row as any).age_sec;
      if (prev[i] !== old) sameAll = false;
      return old;
    }
    sameAll = false;
    return row;
  });
  return sameAll ? (prev as T[]) : merged;
}

function sameDisplay(a: Record<string, any>, b: Record<string, any>): boolean {
  const ka = Object.keys(a);
  if (ka.length !== Object.keys(b).length) return false;
  return ka.every(k => {
    if (k === "age_sec") return true;
    const va = a[k];
    const vb = b[k];
    if (va === vb) return true;
    if (va && vb && typeof va === "object" && typeof vb === "object")
      return JSON.stringify(va) === JSON.stringify(vb);
    return false;
  });
}

/** 全选 = 并集（QR1120-W19）：筛选外的已选（如勾了离线机后开「仅在线」）
 *  不被候选集整体覆盖。HostPicker 的全选按钮走这里。 */
export function unionSelection(
  draft: string[],
  candidates: string[]
): string[] {
  return [...new Set([...draft, ...candidates])];
}

/** 反选 = draft 与 candidates 的对称差（QR1120-W19）：只翻候选内的勾选，
 *  筛选外的已选原样保留——原先用 candidates 整体覆盖会静默清空它们。 */
export function invertSelection(
  draft: string[],
  candidates: string[]
): string[] {
  const cand = new Set(candidates);
  const picked = new Set(draft);
  const next = new Set<string>();
  for (const p of picked) if (!cand.has(p)) next.add(p);
  for (const p of cand) if (!picked.has(p)) next.add(p);
  return [...next];
}

/** 命令/台账状态 → 圆点类名（QR1121-W2，逐字搬自命令历史 hook 的
 *  statusStateClass，welcome/detail/统计/更新台账共用同一份判级）。
 *
 * 状态列的严重度判级：**逐档照搬 `statusType` 返回值**（五档 el-tag
 * type 映射到本仓四档圆点），语义一条不许丢——这是 L1-5 评审揪出的那类回归：
 * `.kk-state::before` 默认圆点是 `var(--kk-live)`（绿 = 在线/健康），只改文字色的
 * `.kk-bad` 不动圆点，于是 `failed`/`timeout`/`lost` 会顶着绿色健康点说「一切正常」。
 *
 * | statusType | 状态值 | 圆点 |
 * |---|---|---|
 * | `danger` | failed / timeout / lost | 红点 + 红字（`--bad` 上色圆点，`kk-bad` 上色文字） |
 * | `warning` | sent | 琥珀点（已下发等结果） |
 * | `primary` | running | 琥珀点（与 sent 同档，见下） |
 * | `success` | done | 默认实心绿点（返回基类，不加变体） |
 * | `info` | pending / queued / 未知 | 空心点（未开始，不是错误也不是健康） |
 *
 * `primary`（running）与 `warning`（sent）并进同一档琥珀点，是本仓状态语言的取舍：
 * `kk.scss` 的四档词汇（绿/红/琥珀/空心）里没有蓝色圆点，而 `running` 与 `sent` 的
 * 严重度本来就相同——都是「未终态、等结果」，也正是 `hasActive` 与 3s 快轮询要一起
 * 捞的那一档；两者的**文字**仍由 `statusLabel` 分别给出「执行中 / 已下发」，finer 区分
 * 没有丢。另两条禁区：`kk-warn` 在本仓专指「Agent 版本落后于待分发版本」，`kk-state--updating`
 * 是方点、承载 B6「更新中」语义——都不许挪用去表达命令严重度。
 */
export function cmdStateClass(s: string): string {
  switch (statusType(s)) {
    case "danger":
      return "kk-state kk-state--bad kk-bad";
    case "warning":
    case "primary":
      return "kk-state kk-state--warn";
    case "success":
      return "kk-state";
    default:
      return "kk-state kk-state--idle";
  }
}

/** 轮询页新鲜度读数统一口径（QR1121-W8）：monitor 的「已同步 · 时刻」信息量
 *  最高，detail/trend/update 原先显静态「每 N 秒自动刷新」——刷新失败时用户
 *  看不出数据已过期。三态：失败变冷文案 / 尚未同步 / 已同步 · 时刻。 */
export function syncReadout(failed: boolean, lastLoadedAt: number): string {
  if (failed) return "刷新失败，数据可能已过期";
  if (!lastLoadedAt) return "尚未同步";
  return `已同步 · ${tsText(lastLoadedAt)}`;
}

/** 采集项中文（QR1121-W3）：键集与服务端 COLLECT_ITEMS 白名单同口径
 *  （config.py），未知项原样回显——后端加新项前端不崩也不编文案。 */
export const COLLECT_ITEM_LABEL: Record<string, string> = {
  cpu: "CPU",
  mem: "内存",
  disk: "磁盘",
  disk_io: "磁盘 IO",
  net: "网卡",
  proc: "进程",
  user: "登录用户",
  sys: "系统信息"
};

export const collectItemText = (item: string): string =>
  COLLECT_ITEM_LABEL[item] || item;

/** 审计动作中文（QR1121-W4）：键集穷举后端 store.add_audit 的实际动作
 *  （auth/commands/agent_update/exporting/rbac/containers/mqtt_bridge），
 *  未知动作原样回显以便排障。**只管文字，不碰判级**——严重度仍走
 *  audit hook 的 actionStateClass(raw) 四档，映射命中与否不影响圆点颜色。 */
export const AUDIT_ACTION_LABEL: Record<string, string> = {
  login_ok: "登录成功",
  login_fail: "登录失败",
  login_locked: "登录锁定",
  login_disabled: "禁用账号登录",
  logout: "登出",
  command_create: "下发命令",
  command_blocked: "命令拦截",
  export: "导出",
  agent_upload: "上传 Agent",
  agent_rollback: "回滚 Agent",
  agent_prev_lost: "旧版二进制丢失",
  agent_upgrade_batch: "批量升级",
  agent_update_failed: "升级失败",
  agent_binary_download: "下载 Agent",
  user_create: "新增用户",
  user_update: "修改用户",
  user_delete: "删除用户",
  role_create: "新增角色",
  role_update: "修改角色",
  role_delete: "删除角色",
  menu_update: "修改菜单",
  host_limits_set: "设置主机上限",
  ip_rejected: "IP 白名单拒绝",
  proto_mismatch: "协议版本不匹配",
  host_mismatch: "主机身份不匹配",
  hb_unknown_host: "未知主机心跳",
  interval_violation: "心跳间隔违规",
  result_unknown_cmd: "未知命令结果",
  result_mismatch: "结果归属不匹配",
  update_result_mismatch: "升级结果不匹配"
};

export const auditActionText = (action: string): string =>
  AUDIT_ACTION_LABEL[action] || action;

/** 命令类型中文（QR1121-W19）：收编 welcome 内联 kindLabel，
 *  命令历史/详情/首页共用一份。未知类型原样回显。 */
export const kindText = (kind: string): string =>
  ({ shell: "命令", collect: "采集", plugin_reload: "插件重载" })[kind] || kind;

/** 审计明细长值截断（QR1121-W16）：>120 字符截断加 …，full 留完整值供
 *  tooltip 展开。字符串化口径与明细列原渲染逐字一致
 *  （typeof v === "object" ? JSON.stringify(v) : String(v)），
 *  短值的 text === full，渲染路径不变（QR1120-W26 结构锁）。 */
export function detailValueText(v: unknown): {
  text: string;
  full: string;
  truncated: boolean;
} {
  const full = typeof v === "object" ? JSON.stringify(v) : String(v);
  const truncated = full.length > 120;
  return { text: truncated ? full.slice(0, 120) + "…" : full, full, truncated };
}

/** 表头单元格样式（QR1121）：monitor/update 曾各内联一份同样的对象，
 *  收口为常量。颜色走 EP 变量，light/dark 与主题预设天然适配。 */
export const kkHeaderCellStyle: Readonly<Record<string, string>> = {
  background: "var(--el-fill-color-light)",
  color: "var(--el-text-color-primary)"
};
