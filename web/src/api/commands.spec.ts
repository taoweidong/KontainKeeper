/** api/commands.ts 的单元测试：命令下发与结果查询的端点形状。
 *
 *  锁住的语义：
 *  - 下发体走 `data`（argv 直传与 cmdline 由后端裁决，前端这层不预判、不补字段）；
 *  - 列表筛选与分页全部下推 query（QR-S36：导出与所见一致的前提），0 是合法分页值；
 *  - `getCommandOut` 带 `format=text` 且返回纯文本；批次列表 limit 默认 20。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";

const httpMock = vi.hoisted(() => ({ request: vi.fn() }));
vi.mock("@/utils/http", () => ({ http: httpMock }));

import {
  createCommand,
  getCommand,
  getCommandOut,
  listBatches,
  listCommands,
  listCollectItems
} from "./commands";

beforeEach(() => {
  httpMock.request.mockReset();
  httpMock.request.mockResolvedValue({});
});

describe("只读端点", () => {
  it("采集项白名单 / 单条命令 / 批次列表：GET 形状与默认 limit=20", async () => {
    httpMock.request.mockResolvedValue({ items: ["cpu", "mem"] });
    await expect(listCollectItems()).resolves.toEqual({
      items: ["cpu", "mem"]
    });
    expect(httpMock.request).toHaveBeenCalledWith("get", "/api/collect/items");

    await getCommand("cmd-0001");
    expect(httpMock.request).toHaveBeenCalledWith(
      "get",
      "/api/commands/cmd-0001"
    );

    await listBatches();
    expect(httpMock.request).toHaveBeenCalledWith(
      "get",
      "/api/commands/batches",
      {
        params: { limit: 20 }
      }
    );

    await listBatches(5);
    expect(httpMock.request.mock.calls[3][2]).toEqual({ params: { limit: 5 } });
  });

  it("命令 id 未做 encodeURIComponent（现状登记：与 api/containers.ts 相反）", async () => {
    await getCommand("a b/c");
    expect(httpMock.request.mock.calls[0][1]).toBe("/api/commands/a b/c");
    // 命令 id 由服务端生成（十六进制串），线上不含空格/斜杠，所以这里只锁「当前形态」，
    // 不给它背书：一旦 id 改为外部可控，这里就是路径注入面，应与 containers 同源编码。
  });

  it("完整输出：GET /api/commands/{id}/out?format=text；空输出回空串而不是 undefined", async () => {
    httpMock.request.mockResolvedValue("total 8\ndrwxr-xr-x");
    await expect(getCommandOut("cmd-1")).resolves.toBe("total 8\ndrwxr-xr-x");
    expect(httpMock.request).toHaveBeenCalledWith(
      "get",
      "/api/commands/cmd-1/out",
      {
        params: { format: "text" }
      }
    );

    httpMock.request.mockResolvedValue("");
    await expect(getCommandOut("cmd-1")).resolves.toBe(""); // 抽屉靠它显示「（无输出）」
  });
});

describe("createCommand：body 形状", () => {
  it("shell + use_shell=true 整条命令进 data，不进 query", async () => {
    httpMock.request.mockResolvedValue({ items: [], batch_id: "b-1" });

    await createCommand({
      pods: ["host-a"],
      kind: "shell",
      cmdline: "df -h | grep /",
      use_shell: true,
      timeout: 30
    });

    expect(httpMock.request).toHaveBeenCalledWith("post", "/api/commands", {
      data: {
        pods: ["host-a"],
        kind: "shell",
        cmdline: "df -h | grep /",
        use_shell: true,
        timeout: 30
      }
    });
  });

  it("argv 直 exec 不补 cmdline；collect/plugin_reload 各带自己的字段；空 pods 照发（门禁不在 api 层）", async () => {
    await createCommand({
      pods: ["host-a"],
      argv: ["/bin/ls", "-l", "/tmp/a b"],
      use_shell: false
    });
    const body = httpMock.request.mock.calls[0][2].data;
    expect(body.argv).toEqual(["/bin/ls", "-l", "/tmp/a b"]);
    expect(body.use_shell).toBe(false);
    expect(body.cmdline).toBeUndefined(); // 不替调用方补 cmdline

    await createCommand({
      pods: ["h"],
      kind: "collect",
      items: ["cpu", "disk"]
    });
    expect(httpMock.request.mock.calls[1][2].data).toEqual({
      pods: ["h"],
      kind: "collect",
      items: ["cpu", "disk"]
    });

    await createCommand({ pods: ["h"], kind: "plugin_reload" });
    expect(httpMock.request.mock.calls[2][2].data).toEqual({
      pods: ["h"],
      kind: "plugin_reload"
    });

    await createCommand({ pods: [], kind: "shell", cmdline: "id" }); // 400 由后端裁决
    expect(httpMock.request.mock.calls[3][2].data.pods).toEqual([]);
  });
});

describe("listCommands：筛选与分页下推", () => {
  it("无参数时 params 位存在但为 undefined；筛选与分页一起进 query，0 不被真值判断吃掉", async () => {
    await listCommands();
    expect(httpMock.request).toHaveBeenCalledWith("get", "/api/commands", {
      params: undefined
    });

    await listCommands({
      pod: "host-a",
      status: "failed",
      keyword: "df",
      batch: "ug-1-3",
      limit: 50,
      offset: 100
    });
    expect(httpMock.request.mock.calls[1][2]).toEqual({
      params: {
        pod: "host-a",
        status: "failed",
        keyword: "df",
        batch: "ug-1-3",
        limit: 50,
        offset: 100
      }
    });

    await listCommands({ limit: 0, offset: 0 });
    expect(httpMock.request.mock.calls[2][2].params).toEqual({
      limit: 0,
      offset: 0
    });
  });
});
