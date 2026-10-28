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
  metrics: Record<string, any>;
  custom: Record<string, any>;
  disk_alert: boolean;
  /** 自定义标签：`{k: v}`，超限时后端整对丢弃并留 `_truncated: true` */
  labels: Record<string, any>;
  commands: Array<Record<string, any>>;
};

export type MetricPoint = {
  ts: number;
  cpu: number | null;
  mem_mb: number | null;
};

export type MetricsResult = {
  pod: string;
  hours: number;
  /** raw = 24h 内的原始点；hourly = 超出后自动切到小时聚合 */
  source: "raw" | "hourly";
  series: MetricPoint[];
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
