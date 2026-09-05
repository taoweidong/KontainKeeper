/** 主机（容器）列表 / 详情 / 指标序列。

字段与后端 `server/src/kk_server/controllers/containers.py` 一一对应：
- `?view=summary` 只回标量（列表页 10s 轮询走这条）
- 详情走全量视图，带完整 metrics 与最近命令
*/
import { http } from "@/utils/http";

/** 列表摘要行：不含完整指标，避免 500 台 × 4KB 的 JSON 解析 */
export type HostSummary = {
  pod: string;
  image: string;
  agent_ver: string;
  online: boolean;
  age_sec: number;
  cpu: number | null;
  mem_mb: number | null;
  disk_pct: number;
  disk_alert: boolean;
};

export type HostListResult = {
  items: HostSummary[];
  total: number;
  online: number;
  alerts: number;
};

/** 详情视图：metrics 是最近一帧完整指标（含 disks / procs_top / users） */
export type HostDetail = {
  pod: string;
  image: string;
  agent_ver: string;
  hb_interval: number;
  first_seen: number;
  last_seen: number;
  online: boolean;
  age_sec: number;
  metrics: Record<string, any>;
  custom: Record<string, any>;
  disk_alert: boolean;
  commands: Array<Record<string, any>>;
};

export type MetricPoint = { ts: number; cpu: number | null; mem_mb: number | null };

export type MetricsResult = {
  pod: string;
  hours: number;
  /** raw = 24h 内的原始点；hourly = 超出后自动切到小时聚合 */
  source: "raw" | "hourly";
  series: MetricPoint[];
};

export const listHosts = (view: "full" | "summary" = "summary") => {
  return http.request<HostListResult>("get", "/api/containers", { params: { view } });
};

export const getHost = (pod: string) => {
  return http.request<HostDetail>("get", `/api/containers/${encodeURIComponent(pod)}`);
};

export const getHostMetrics = (pod: string, hours = 24) => {
  return http.request<MetricsResult>("get", `/api/containers/${encodeURIComponent(pod)}/metrics`, {
    params: { hours }
  });
};
