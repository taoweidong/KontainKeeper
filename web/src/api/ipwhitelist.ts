/** 动态 IP 白名单管理 + 拦截记录（QR1124）。
 *
 * 服务端要求 admin 角色（AdminUser），前端只做展示控制（403 由 http 拦截器统一
 * 提示，不登出）。白名单 CRUD 仿 rbac.ts（无分页全量 items）；拦截记录仿 audit.ts
 * （limit/offset + ip 筛选下推，与导出同语义）。env KK_AGENT_IPS 为启动种子，
 * DB 为运行时真相源，变更即时生效无需重启。
 */
import { http } from "@/utils/http";

export type IpRule = {
  id: number;
  cidr: string;
  remark: string;
  enabled: boolean;
  created: number;
};

export const listIpRules = () =>
  http.request<{ items: IpRule[] }>("get", "/api/system/ip-whitelist");

export const createIpRule = (data: { cidr: string; remark?: string }) =>
  http.request<{ ok: boolean; cidr: string }>(
    "post",
    "/api/system/ip-whitelist",
    { data }
  );

export const updateIpRule = (
  id: number,
  data: { enabled?: boolean; remark?: string }
) =>
  http.request<{ ok: boolean }>("put", `/api/system/ip-whitelist/${id}`, {
    data
  });

export const deleteIpRule = (id: number) =>
  http.request<{ ok: boolean }>("delete", `/api/system/ip-whitelist/${id}`);

// ---- 拦截记录（按 IP 聚合）----

export type IpRejectRow = {
  ip: string;
  hits: number;
  pod: string;
  kind: string;
  first_seen: number;
  last_seen: number;
};

export type IpRejectListResult = {
  items: IpRejectRow[];
  total: number;
  offset: number;
  limit: number;
};

export const listIpRejects = (params?: {
  limit?: number;
  offset?: number;
  ip?: string;
}) => http.request<IpRejectListResult>("get", "/api/ip-rejects", { params });
