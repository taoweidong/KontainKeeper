/** api/exporting.ts 的单元测试：四类 CSV 导出。
 *
 *  锁住的语义（文件头注释里那两个「必须显式设置」的点）：
 *  - `responseType: "blob"`：缺了它 axios 会按文本解码，中文与 BOM 一起坏掉；
 *  - `timeout: 0`：http 默认 10s，导出千行必然超时；
 *  - 参数与列表页同源（QR-S36），`exportHosts` 不再带那个从不生效的 `view`（QR29-S16）。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";

const httpMock = vi.hoisted(() => ({ request: vi.fn() }));
vi.mock("@/utils/http", () => ({ http: httpMock }));

import { exportAudit, exportCommands, exportHosts, exportMetrics } from "./exporting";

beforeEach(() => {
  httpMock.request.mockReset();
  httpMock.request.mockResolvedValue(new Blob(["id\n"], { type: "text/csv" }));
});

describe("导出端点公共形状", () => {
  it("四条路径都是 GET /api/export/*，一律 responseType=blob + timeout=0，Blob 原样透传", async () => {
    const blob = new Blob(["中文,BOM\n"], { type: "text/csv" });
    httpMock.request.mockResolvedValue(blob);

    await exportCommands({});
    await exportAudit({});
    await exportHosts();
    await exportMetrics("host-a");

    expect(httpMock.request.mock.calls.map(c => [c[0], c[1]])).toEqual([
      ["get", "/api/export/commands"],
      ["get", "/api/export/audit"],
      ["get", "/api/export/hosts"],
      ["get", "/api/export/metrics"]
    ]);
    for (const call of httpMock.request.mock.calls) {
      expect(call[2].responseType).toBe("blob");   // 缺了它中文与 BOM 一起坏
      expect(call[2].timeout).toBe(0);             // 默认 10s 导出千行必超时
    }
    await expect(exportAudit()).resolves.toBe(blob);   // 文件名由前端 fileStamp 拼
  });
});

describe("参数下推", () => {
  it("命令导出：筛选 + since/until + include_tail 全进 params，0 不被吃掉", async () => {
    await exportCommands({
      pod: "host-a", status: "failed", keyword: "df", batch: "b-1",
      since: 1, until: 2, limit: 1000, include_tail: 1
    });
    expect(httpMock.request.mock.calls[0][2].params).toEqual({
      pod: "host-a", status: "failed", keyword: "df", batch: "b-1",
      since: 1, until: 2, limit: 1000, include_tail: 1
    });

    await exportCommands({ include_tail: 0 });   // 0 = 明确不含末段输出
    expect(httpMock.request.mock.calls[1][2].params).toEqual({ include_tail: 0 });
  });

  it("审计导出只带四键；主机导出与 /api/containers 同源且不传 view（默认全量是空对象）", async () => {
    await exportAudit({ actor: "admin", action: "logout", keyword: "k", limit: 500 });
    expect(httpMock.request.mock.calls[0][2].params).toEqual({
      actor: "admin", action: "logout", keyword: "k", limit: 500
    });

    await exportHosts({ online: "1", group: "prod", sort: "mem" });
    const params = httpMock.request.mock.calls[1][2].params;
    expect(params).toEqual({ online: "1", group: "prod", sort: "mem" });
    expect(params).not.toHaveProperty("view");   // QR29-S16：那个参数从不生效，已删

    await exportHosts();
    expect(httpMock.request.mock.calls[2][2].params).toEqual({});
  });

  it("指标导出把 pod 放 query（与 /api/containers/{pod}/metrics 的 path 风格不同，现状锁死）", async () => {
    await exportMetrics("host-a");
    expect(httpMock.request.mock.calls[0][2].params).toEqual({ pod: "host-a", hours: 24 });

    await exportMetrics("a b", 1);
    expect(httpMock.request.mock.calls[1][1]).toBe("/api/export/metrics");
    expect(httpMock.request.mock.calls[1][2].params).toEqual({ pod: "a b", hours: 1 });
  });
});
