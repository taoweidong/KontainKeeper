/** 系统可观测接口：健康检查与运维面板统计。

`/api/system/stats` 回答三个问题：链路活不活（broker + 最近一帧消息）、
命令有没有积压（按状态分布 + 发布失败计数）、库有没有涨（各表行数）。
*/
import { http } from "@/utils/http";

export type HealthResult = {
  ok: boolean;
  version: string;
  proto_ver: number;
  agents_online: number;
  broker: "connected" | "disconnected";
  bridge: Record<string, number> | null;
};

export type StatsResult = {
  ok: boolean;
  uptime_sec: number;
  hosts: { total: number; online: number };
  commands: Record<string, number>;
  storage: { heartbeats: number; hourly: number };
  broker: {
    connected: boolean;
    stats: Record<string, number> | null;
    last_msg_age_sec: number | null;
  };
};

export const getHealth = () => {
  return http.request<HealthResult>("get", "/api/health");
};

export const getStats = () => {
  return http.request<StatsResult>("get", "/api/system/stats");
};
