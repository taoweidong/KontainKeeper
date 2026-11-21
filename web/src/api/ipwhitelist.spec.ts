/** IP 白名单 API 层契约锁（QR1124）：URL/method 逐条对齐后端
 *  controllers/ipwhitelist.py；拦截记录列表 limit/offset/ip 下推；导出 blob+timeout:0。 */
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/utils/http", () => ({ http: { request: vi.fn() } }));

import { http } from "@/utils/http";
import {
  createIpRule,
  deleteIpRule,
  listIpRejects,
  listIpRules,
  updateIpRule
} from "@/api/ipwhitelist";
import { exportIpRejects } from "@/api/exporting";

const req = vi.mocked(http.request);

beforeEach(() => {
  req.mockReset();
  req.mockResolvedValue({} as never);
});

describe("IP 白名单 API：URL/method 契约", () => {
  it("规则 CRUD 四条路径", async () => {
    await listIpRules();
    expect(req).toHaveBeenCalledWith("get", "/api/system/ip-whitelist");
    await createIpRule({ cidr: "10.0.0.0/24", remark: "dev" });
    expect(req).toHaveBeenCalledWith("post", "/api/system/ip-whitelist", {
      data: { cidr: "10.0.0.0/24", remark: "dev" }
    });
    await updateIpRule(7, { enabled: false });
    expect(req).toHaveBeenCalledWith("put", "/api/system/ip-whitelist/7", {
      data: { enabled: false }
    });
    await deleteIpRule(7);
    expect(req).toHaveBeenCalledWith("delete", "/api/system/ip-whitelist/7");
  });

  it("拦截记录列表：limit/offset/ip 下推", async () => {
    await listIpRejects({ limit: 100, offset: 200, ip: "192.0.2" });
    expect(req).toHaveBeenCalledWith("get", "/api/ip-rejects", {
      params: { limit: 100, offset: 200, ip: "192.0.2" }
    });
  });

  it("拦截记录导出：blob + timeout:0（千行不超时）", async () => {
    await exportIpRejects({ ip: "192.0.2", limit: 5000 });
    expect(req).toHaveBeenCalledWith("get", "/api/export/ip-rejects", {
      params: { ip: "192.0.2", limit: 5000 },
      responseType: "blob",
      timeout: 0
    });
  });
});
