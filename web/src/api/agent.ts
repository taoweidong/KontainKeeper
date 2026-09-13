/** Agent 自升级：当前版本、落后台数、批量升级、升级台账。

对应后端 `server/src/kk_server/controllers/agent_update.py`：
- GET  /api/system/agent/current   管理员视图：当前版本 + 落后台数
- POST /api/system/agent/upgrade   管理员动作：选机升级（500 台不留 N+1）
- GET  /api/system/updates         升级台账 + 状态汇总（500 台的升级结果能逐台核验）
*/
import { http } from "@/utils/http";

/** 管理员看到的「当前待分发版本 + 落后台数」 */
export type AgentCurrent = {
  version: string;
  sha256: string;
  size: number;
  uploaded_at: number;
  hosts_total: number;
  hosts_outdated: number;
};

/** 选机升级接口的跳过原因；后端 controller 里已穷举，前端逐字渲染 */
export type UpgradeSkipReason =
  | "not_found"
  | "already_latest"
  | "in_flight"
  | "no_binary"
  | "bad_version"
  | "no_broker";

export type UpgradeAccepted = {
  host: string;
  from_version: string;
  to_version: string;
  /** 台账主键，与 Agent 回执里的 cmd id 对齐（A6.2） */
  ledger_id: string;
  /** true = 主机离线时受理，台账记 queued 等重连补投 */
  queued: boolean;
};

export type UpgradeSkipped = { host: string; reason: UpgradeSkipReason };

export type UpgradeResult = {
  ok: boolean;
  /** ug-<ts>-<count>；用于审计关联 */
  batch_id: string;
  accepted: UpgradeAccepted[];
  skipped: UpgradeSkipped[];
};

/** 台账单行：与 server/models/tables.py 里 kk_updates 一一对应 */
export type UpdateRow = {
  id: string;
  pod: string;
  from_version: string;
  to_version: string;
  status: "pending" | "queued" | "done" | "failed" | "timeout";
  reason: string;
  created_at: number;
};

export type UpdatesResult = {
  items: UpdateRow[];
  /** 各状态计数（不区分主机）；前端按数字大小排序展示 */
  summary: Record<string, number>;
  limit: number;
};

export const getAgentCurrent = () => {
  return http.request<AgentCurrent>("get", "/api/system/agent/current");
};

/** 选机升级：hosts 为空数组返回 400（前端守住，不再重复发请求） */
export const upgradeHosts = (hosts: string[]) => {
  return http.request<UpgradeResult>("post", "/api/system/agent/upgrade", { data: { hosts } });
};

export const listUpdates = (limit = 50) => {
  return http.request<UpdatesResult>("get", "/api/system/updates", { params: { limit } });
};
