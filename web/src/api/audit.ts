/** 审计日志。后端 detail 字段是 JSON 字符串，展示前解析成对象。 */
import { http } from "@/utils/http";

export type AuditRow = {
  id: number;
  actor: string;
  action: string;
  detail: string;
  ts: number;
};

export const listAudit = (limit = 200) => {
  return http.request<{ items: AuditRow[] }>("get", "/api/audit", { params: { limit } });
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
