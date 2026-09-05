/** 命令下发与结果查询。

三种 kind：
- `shell`  argv 数组直传（推荐，含空格路径不会被拆坏）/ cmdline + use_shell
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
};

export const listCollectItems = () => {
  return http.request<{ items: string[] }>("get", "/api/collect/items");
};

export const createCommand = (data: CommandCreateBody) => {
  return http.request<CommandCreateResult>("post", "/api/commands", { data });
};

export const listCommands = (params?: { pod?: string; limit?: number }) => {
  return http.request<{ items: CommandRow[] }>("get", "/api/commands", { params });
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
