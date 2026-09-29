/** 审计日志。后端 detail 字段是 JSON 字符串，展示前解析成对象。 */
import { http } from "@/utils/http";

export type AuditRow = {
  id: number;
  actor: string;
  action: string;
  detail: string;
  ts: number;
};

export type AuditListResult = {
  items: AuditRow[];
  total: number;
  offset: number;
  limit: number;
};

/** keyword/actor/action 由后端 store._audit_filters 下推过滤，与导出同一套语义。 */
export const listAudit = (params?: {
  limit?: number;
  offset?: number;
  keyword?: string;
  actor?: string;
  action?: string;
}) => {
  return http.request<AuditListResult>("get", "/api/audit", { params });
};

export const parseDetail = (raw?: string): Record<string, any> => {
  if (!raw) return {};
  try {
    const v = JSON.parse(raw);
    return v && typeof v === "object" ? v : { 值: v };
  } catch {
    return { 原文: raw };
  }
};
