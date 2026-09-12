/** 命令下发与结果查询。

三种 kind：
- `shell`  cmdline + use_shell=true（前端命令面板恒此形态，经 sh -c 支持全部 shell 语法）/
  argv 数组直传 exec（不经 shell，含空格路径不会被拆坏）
- `collect` 按项采集，items 取自后端白名单 `/api/collect/items`
- `plugin_reload` 让 Agent 重扫采集插件目录

输出分两档：列表给 `out_tail`（末 2KB），完整输出单独走 `getCommandOut`。
`out_purged=1` 表示输出已按保留策略清理，状态行仍在。
*/
import { http } from "@/utils/http";

export type CommandRow = {
  id: string;
  pod: string;
  kind: string;
  argv: string[] | Record<string, any> | null;
  timeout: number;
  status: "pending" | "sent" | "running" | "done" | "failed" | "timeout" | "lost";
  created_by: string;
  created_at: number;
  sent_at: number | null;
  finished_at: number | null;
  rc: number | null;
  timed_out: number;
  truncated: number;
  elapsed_ms: number | null;
  out_chunks: number;
  out_purged: number;
  /** 一次批量下发的批次号（A3） */
  batch_id?: string;
  out_tail?: string;
};

export type CommandCreateBody = {
  pods: string[];
  kind?: "shell" | "collect" | "plugin_reload";
  argv?: string[];
  cmdline?: string;
  items?: string[];
  use_shell?: boolean;
  timeout?: number;
};

export type CommandCreateResult = {
  items: Array<{ id: string; pod: string; status: string }>;
  /** 一批次一号：下发后据此聚合核验（A3） */
  batch_id: string;
};

/** 列表查询：筛选条件全部下推后端，导出与所见才一致 */
export type CommandListParams = {
  pod?: string;
  batch?: string;
  status?: string;
  kind?: string;
  keyword?: string;
  limit?: number;
  offset?: number;
};

export type CommandListResult = {
  items: CommandRow[];
  total: number;
  offset: number;
  limit: number;
};

/** 批次内各状态计数（GET /api/commands/batches） */
export type BatchSummary = {
  batch_id: string;
  total: number;
  created_at: number;
  [status: string]: number | string;
};

export type BatchListResult = { items: BatchSummary[] };

export const listCollectItems = () => {
  return http.request<{ items: string[] }>("get", "/api/collect/items");
};

export const createCommand = (data: CommandCreateBody) => {
  return http.request<CommandCreateResult>("post", "/api/commands", { data });
};

export const listCommands = (params?: CommandListParams) => {
  return http.request<CommandListResult>("get", "/api/commands", { params });
};

export const listBatches = (limit = 20) => {
  return http.request<BatchListResult>("get", "/api/commands/batches", {
    params: { limit }
  });
};

export const getCommand = (id: string) => {
  return http.request<CommandRow>("get", `/api/commands/${id}`);
};

/** 完整输出（纯文本）。列表里的 out_tail 只是末 2KB，点开看全量走这里。 */
export const getCommandOut = (id: string) => {
  return http.request<string>("get", `/api/commands/${id}/out`, {
    params: { format: "text" }
  });
};
