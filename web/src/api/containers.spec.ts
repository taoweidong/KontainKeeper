/** api/containers.ts 的单元测试：主机列表/详情/指标序列三个端点的请求形状。
 *
 *  锁住的语义：
 *  - 列表默认 `view=summary`（列表页 10s 轮询只取标量，500 台 × 4KB 的完整 JSON 是事故）；
 *  - 筛选参数一律进 query `params`，三态用字符串承载（QR-S36 要求列表与导出同源，
 *    参数放置方式必须一致）；空筛选不许产生多余键；
 *  - 主机名进 URL path 前做 `encodeURIComponent`（pod 名可能含 `/`、空格、中文）。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";

const httpMock = vi.hoisted(() => ({ request: vi.fn() }));
vi.mock("@/utils/http", () => ({ http: httpMock }));

import { getHost, getHostMetrics, listHosts } from "./containers";

beforeEach(() => {
  httpMock.request.mockReset();
  httpMock.request.mockResolvedValue({
    items: [],
    total: 0,
    online: 0,
    alerts: 0,
    outdated: 0
  });
});

describe("listHosts", () => {
  it("默认 summary 视图且空筛选不产生多余键；full + 筛选合并进同一 params；三态字符串原样下推；响应不做兜底改写", async () => {
    httpMock.request.mockResolvedValue({
      items: [],
      total: 0,
      online: 0,
      alerts: 0,
      outdated: 0
    });

    const res = await listHosts();
    expect(httpMock.request).toHaveBeenCalledWith("get", "/api/containers", {
      params: { view: "summary" }
    });
    expect(Object.keys(httpMock.request.mock.calls[0][2].params)).toEqual([
      "view"
    ]);
    expect(res).toEqual({
      items: [],
      total: 0,
      online: 0,
      alerts: 0,
      outdated: 0
    }); // 空表是真读数

    await listHosts("full", {
      online: "1",
      keyword: "node 1",
      sort: "cpu",
      order: "desc"
    });
    const [, url, config] = httpMock.request.mock.calls[1];
    expect(url).toBe("/api/containers");
    expect(config.params).toEqual({
      view: "full",
      online: "1",
      keyword: "node 1",
      sort: "cpu",
      order: "desc"
    });

    await listHosts("summary", { online: "0", alert: "false" }); // 布尔三态用字符串，0/false 也是有效筛选
    expect(httpMock.request.mock.calls[2][2].params).toMatchObject({
      online: "0",
      alert: "false"
    });
  });
});

describe("getHost：主机名进 path 前必须编码", () => {
  it("常规名字逐字进 path；含 / 空格中文都被编码，不会撑破别人的路由", async () => {
    await getHost("vscode-server-abc");
    expect(httpMock.request).toHaveBeenCalledWith(
      "get",
      "/api/containers/vscode-server-abc"
    );

    await getHost("a/b c");
    expect(httpMock.request.mock.calls[1][1]).toBe("/api/containers/a%2Fb%20c");

    await getHost("节点一");
    expect(httpMock.request.mock.calls[2][1]).toBe(
      `/api/containers/${encodeURIComponent("节点一")}`
    );
  });

  it("空主机名拼出 /api/containers/（现状登记：api 层不校验空 pod，会命中列表路由）", async () => {
    await getHost("");
    expect(httpMock.request.mock.calls[0][1]).toBe("/api/containers/");
  });
});

describe("getHostMetrics", () => {
  it("hours 进 query 而不是 path：默认 24，0 也原样下推；path 编码与 getHost 同源", async () => {
    await getHostMetrics("host-a");
    expect(httpMock.request).toHaveBeenCalledWith(
      "get",
      "/api/containers/host-a/metrics",
      { params: { hours: 24 } }
    );

    await getHostMetrics("host-a", 0); // 0 = 空窗口，不是「用默认值」
    expect(httpMock.request.mock.calls[1][2]).toEqual({ params: { hours: 0 } });

    await getHostMetrics("x y/z", 6);
    expect(httpMock.request.mock.calls[2][1]).toBe(
      "/api/containers/x%20y%2Fz/metrics"
    );
    expect(httpMock.request.mock.calls[2][2]).toEqual({ params: { hours: 6 } });
  });
});
