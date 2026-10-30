/** 主机（容器）列表 / 详情 / 指标序列。

字段与后端 `server/src/kk_server/controllers/containers.py` 一一对应：
- `?view=summary` 只回标量（列表页 10s 轮询走这条）
- 详情走全量视图，带完整 metrics 与最近命令
- 筛选与排序走 `HostFilters`，列表与导出共用（QR-S36：页面看到什么就导出什么）
*/
import { http } from "@/utils/http";

/** v4 机队元信息：服务端 `_host_meta()` 一份字段，列表与详情同形。
 *
 * `caps` 后端给的是结构而不是 JSON 文本——让每个页面各自 parse 等于把
 * 「解析失败怎么办」分发出去各写一遍。 */
export type HostMeta = {
  /** metal / vm / container（Agent 自报的虚拟化形态） */
  host_type: string;
  os_name: string;
  ip: string;
  group_name: string;
  /** 该主机上报帧的协议版本；3 = 仍在兼容窗口内，服务端重启后 proto_v3_received 归零才可关窗 */
  proto_ver: number | null;
  docker_total: number;
  docker_running: number;
  docker_unhealthy: number;
  caps: Record<string, any>;
};

/** 列表摘要行：不含完整指标，避免 500 台 × 4KB 的 JSON 解析 */
export type HostSummary = HostMeta & {
  pod: string;
  image: string;
  agent_ver: string;
  /** 服务端当前待分发版本：空字符串 = 还没上传过版本，「落后」无从定义 */
  latest_agent_ver: string;
  /** 是否落后于 latest_agent_ver（D1.2 引入：详情/列表/总览共用） */
  agent_outdated: boolean;
  online: boolean;
  age_sec: number;
  cpu: number | null;
  mem_mb: number | null;
  disk_pct: number;
  disk_alert: boolean;
  /** 主机被标 offline 的原因（updating / stopped / ...），供 UI 区分运维场景 */
  status_reason: string;
};

export type HostListResult = {
  items: HostSummary[];
  total: number;
  online: number;
  alerts: number;
  /** 服务端当前待分发版本：与行内 latest_agent_ver 同源，由后端顶层再发一份（QR29-D.2 契约对账） */
  latest_agent_ver: string;
  /** 「最新版本下的落后台数」——与 summary 中 agent_outdated 等价，由后端算好避免前端再过 500 行 */
  outdated: number;
};

/** 详情视图：metrics 是最近一帧完整指标（含 disks / procs_top / users） */
export type HostDetail = HostMeta & {
  pod: string;
  image: string;
  agent_ver: string;
  latest_agent_ver: string;
  agent_outdated: boolean;
  hb_interval: number;
  first_seen: number;
  last_seen: number;
  online: boolean;
  age_sec: number;
  /** 离线原因（updating / stopped / ...）：列表行已有，详情对齐建模（QR29-D.2） */
  status_reason: string;
  metrics: Record<string, any>;
  custom: Record<string, any>;
  disk_alert: boolean;
  /** 自定义标签：`{k: v}`，超限时后端整对丢弃并留 `_truncated: true` */
  labels: Record<string, any>;
  commands: Array<Record<string, any>>;
  /** 单机告警上限（%）：null = 未自定义（磁盘回落全局 85，内存不画线） */
  mem_limit: number | null;
  disk_limit: number | null;
};

export type MetricPoint = {
  ts: number;
  cpu: number | null;
  mem_mb: number | null;
  /** 内存使用率（%）：采集器原值；null = 该帧没带（老 Agent / 采集被关），画断线 */
  mem_pct: number | null;
  /** 磁盘使用率（%）：多挂载点取最大；null 同上。序列形状 raw/hourly 双路统一 */
  disk_pct: number | null;
};

export type MetricsResult = {
  pod: string;
  hours: number;
  /** raw = 24h 内的原始点；hourly = 超出后自动切到小时聚合 */
  source: "raw" | "hourly";
  series: MetricPoint[];
  /** 内存告警上限（%）：单机覆盖原值，null = 未设置（没有全局口径，图上不画线） */
  mem_limit: number | null;
  /** 磁盘告警上限（%）：**有效值**（单机覆盖 ?? 全局 85），画线用 */
  disk_limit: number;
  /** 磁盘单机覆盖原值：null = 未自定义——编辑表单要区分「没设过」与「设成 85」 */
  disk_limit_raw: number | null;
};

/** v4 机队筛选 / 排序参数，与后端 `deps.host_filter_params` 一一对应。
 *
 * 一律用字符串而不是 boolean：布尔位走三态（空 = 不筛），把 JS 真值直接塞进
 * query 会造出 `?online=false` 这种「看着在筛、其实筛的是反的」的请求——
 * 后端 `_tri()` 认 1/true/yes/on 与 0/false/no/off，其余一律当不筛。
 *
 * `docker_unhealthy` 暂无页面入口：内置 Agent 的 status 帧不带 docker 摘要
 * （协议 §3.1.1 里它是预留字段），筛出来恒为空表（QR-S38）。
 */
export type HostFilters = {
  group?: string;
  host_type?: string;
  docker_unhealthy?: string;
  online?: string;
  proto_ver?: string;
  keyword?: string;
  alert?: string;
  sort?: string;
  order?: string;
};

export const listHosts = (
  view: "full" | "summary" = "summary",
  filters: HostFilters = {}
) => {
  return http.request<HostListResult>("get", "/api/containers", {
    params: { view, ...filters }
  });
};

export const getHost = (pod: string) => {
  return http.request<HostDetail>(
    "get",
    `/api/containers/${encodeURIComponent(pod)}`
  );
};

export const getHostMetrics = (pod: string, hours = 24) => {
  return http.request<MetricsResult>(
    "get",
    `/api/containers/${encodeURIComponent(pod)}/metrics`,
    {
      params: { hours }
    }
  );
};

/** 单机告警上限（百分比 1–100）；null = 清除自定义、回落全局口径。
 *  总览页 disk_alert 的全局口径不受影响——这里只服务趋势页的告警线。 */
export const setHostLimits = (
  pod: string,
  body: { mem_limit: number | null; disk_limit: number | null }
) => {
  return http.request<{
    ok: boolean;
    mem_limit: number | null;
    disk_limit: number | null;
  }>("put", `/api/containers/${encodeURIComponent(pod)}/limits`, {
    data: body
  });
};
