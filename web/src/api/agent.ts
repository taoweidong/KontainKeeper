/** Agent 自升级：当前版本、落后台数、批量升级、升级台账、待分发版本的上传与回滚。

对应后端 `server/src/kk_server/controllers/agent_update.py`：
- POST /api/system/agent           管理员上传新版本二进制（multipart: file + version）
- POST /api/system/agent/rollback  待分发二进制回滚到上一版（当前 ↔ 上一版互换）
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

/** 上传/回滚接口的回执：与后端 agent_update.py 的返回体一致（清单字段原样回显）。 */
export type AgentVersionInfo = {
  ok: boolean;
  version: string;
  sha256: string;
  size: number;
  uploaded_at: number;
  /** 配了 HMAC 密钥时清单带 sig；未配则缺省 */
  sig?: string;
};

/** 上传新版本二进制（multipart: file + version）。
 *  版本号首字符必须是数字——后端只校验 `version[0].isdigit()`，前端 rules 逐字镜像这条契约，
 *  不另发明更严或更松的规则（`v1.2.3` 这类前缀后端会 400）。 */
export const uploadAgent = (file: File, version: string) => {
  const form = new FormData();
  form.append("file", file);
  form.append("version", version);
  return http.request<AgentVersionInfo>("post", "/api/system/agent", {
    data: form
  });
};

/** 待分发版本回滚到上一版（当前 ↔ 上一版**互换**，再调一次即撤销）。
 *  语义边界：只影响新上线/重启的 Agent，已在跑的不会因此降级（只升不降）。 */
export const rollbackAgent = () => {
  return http.request<AgentVersionInfo>("post", "/api/system/agent/rollback");
};

export const getAgentCurrent = () => {
  return http.request<AgentCurrent>("get", "/api/system/agent/current");
};

/** 选机升级：hosts 为空数组返回 400（前端守住，不再重复发请求） */
export const upgradeHosts = (hosts: string[]) => {
  return http.request<UpgradeResult>("post", "/api/system/agent/upgrade", {
    data: { hosts }
  });
};

export const listUpdates = (limit = 50) => {
  return http.request<UpdatesResult>("get", "/api/system/updates", {
    params: { limit }
  });
};
