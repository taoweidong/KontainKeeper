/** api/agent.ts 的单元测试：自升级五类端点的请求形状。
 *
 *  锁住的语义：
 *  - 上传是 multipart（FormData），不能让 http 默认的 Content-Type: application/json
 *    把二进制当 JSON 序列化——这条一旦改坏就是「上传成功但二进制是坏的」；
 *  - 回滚是无 body 的 POST（服务端做当前↔上一版互换，再调一次即撤销）；
 *  - 升级只带 hosts 数组，受理/跳过全由后端裁决；台账 limit 默认 50；
 *  - 空 version 这类「服务端还没配好」的读数原样透传，不被兜成默认值（QR-W16）。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";

const httpMock = vi.hoisted(() => ({ request: vi.fn() }));
vi.mock("@/utils/http", () => ({ http: httpMock }));

import {
  getAgentCurrent,
  listUpdates,
  rollbackAgent,
  upgradeHosts,
  uploadAgent
} from "./agent";

beforeEach(() => {
  httpMock.request.mockReset();
  httpMock.request.mockResolvedValue({ ok: true });
});

describe("uploadAgent / rollbackAgent", () => {
  it("上传走 FormData（file + version），版本号不做任何改写或补默认", async () => {
    const file = new File(["abc"], "kk-agent", { type: "application/octet-stream" });

    await uploadAgent(file, "1.2.3");

    const [method, url, config] = httpMock.request.mock.calls[0];
    expect(method).toBe("post");
    expect(url).toBe("/api/system/agent");
    expect(config.data).toBeInstanceOf(FormData);
    expect(config.data.get("file")).toBe(file);
    expect(config.data.get("version")).toBe("1.2.3");

    // 后端只校验 version[0].isdigit()，v1.2.3 该由后端 400；空串同理（bad_version 兜住）
    await uploadAgent(new File(["x"], "f"), "v1.2.3");
    expect(httpMock.request.mock.calls[1][2].data.get("version")).toBe("v1.2.3");
    await uploadAgent(new File(["x"], "f"), "");
    expect(httpMock.request.mock.calls[2][2].data.get("version")).toBe("");
  });

  it("回滚：POST /api/system/agent/rollback，不传 body", async () => {
    await rollbackAgent();
    expect(httpMock.request).toHaveBeenCalledWith("post", "/api/system/agent/rollback");
  });
});

describe("getAgentCurrent / upgradeHosts / listUpdates", () => {
  it("当前版本视图：GET /api/system/agent/current；空 version 原样透传", async () => {
    httpMock.request.mockResolvedValue({ version: "", hosts_total: 12, hosts_outdated: 0 });

    const res = await getAgentCurrent();

    expect(httpMock.request).toHaveBeenCalledWith("get", "/api/system/agent/current");
    expect(res.version).toBe("");   // 空串 = 还没上传过版本，「落后」无从定义
  });

  it("选机升级：hosts 进 data，单台也保持数组；空数组照发；回执字段原样透传（queued=true 是离线补投的判据）", async () => {
    await upgradeHosts(["host-a"]);
    expect(httpMock.request).toHaveBeenCalledWith("post", "/api/system/agent/upgrade", {
      data: { hosts: ["host-a"] }
    });

    await upgradeHosts([]);   // 400 由后端裁决，门禁在 hook 不在 api 层
    expect(httpMock.request.mock.calls[1][2]).toEqual({ data: { hosts: [] } });

    const payload = {
      ok: true,
      batch_id: "ug-2-2",
      accepted: [
        { host: "a", from_version: "1.0.0", to_version: "1.1.0", ledger_id: "l1", queued: false },
        { host: "b", from_version: "1.0.0", to_version: "1.1.0", ledger_id: "l2", queued: true }
      ],
      skipped: [{ host: "c", reason: "no_broker" }]
    };
    httpMock.request.mockResolvedValue(payload);
    const r = await upgradeHosts(["a", "b", "c"]);
    expect(r.accepted.filter(x => x.queued)).toHaveLength(1);
    expect(r.skipped[0].reason).toBe("no_broker");
  });

  it("台账默认 limit=50 可覆盖；summary 缺省不补 0", async () => {
    await listUpdates();
    expect(httpMock.request).toHaveBeenCalledWith("get", "/api/system/updates", {
      params: { limit: 50 }
    });

    await listUpdates(200);
    expect(httpMock.request.mock.calls[1][2]).toEqual({ params: { limit: 200 } });

    httpMock.request.mockResolvedValue({ items: [], limit: 50 });
    expect((await listUpdates()).summary).toBeUndefined();
  });
});
