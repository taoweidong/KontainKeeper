/** 数据导出（A1 / P0-1）：四类 CSV 下载。
 *
 * 两个必须显式设置的点：
 * - `responseType: "blob"`：否则 axios 会把 CSV 当文本解码，中文与 BOM 一起坏掉
 * - `timeout: 0`：http 默认超时 10s（utils/http/index.ts:19），导出千行必然超时
 *
 * 文件名由前端拼（utils/kk.ts 的 fileStamp）：响应拦截器只返回 response.data，
 * 拿不到 Content-Disposition。
 */
import { http } from "@/utils/http";

export type CommandExportParams = {
  pod?: string;
  kind?: string;
  status?: string;
  batch?: string;
  since?: number;
  until?: number;
  keyword?: string;
  limit?: number;
  include_tail?: 0 | 1;
};

export type AuditExportParams = {
  actor?: string;
  action?: string;
  keyword?: string;
  limit?: number;
};

/** 命令历史导出：参数与页面筛选同源，导出的就是看到的。 */
export const exportCommands = (params?: CommandExportParams) => {
  return http.request<Blob>("get", "/api/export/commands", {
    params,
    responseType: "blob",
    timeout: 0
  });
};

export const exportAudit = (params?: AuditExportParams) => {
  return http.request<Blob>("get", "/api/export/audit", {
    params,
    responseType: "blob",
    timeout: 0
  });
};

/** 主机清单导出：资产盘点场景，一次性导出全量摘要列。 */
export const exportHosts = () => {
  return http.request<Blob>("get", "/api/export/hosts", {
    params: { view: "summary" },
    responseType: "blob",
    timeout: 0
  });
};

export const exportMetrics = (pod: string, hours = 24) => {
  return http.request<Blob>("get", "/api/export/metrics", {
    params: { pod, hours },
    responseType: "blob",
    timeout: 0
  });
};
