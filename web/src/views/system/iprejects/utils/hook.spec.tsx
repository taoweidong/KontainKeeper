/** QR1124 拦截记录 hook 回归锁：
 *  - ip 筛选 trim 下推 listIpRejects 与 exportIpRejects 同参（导出=所见），空值 undefined。
 *  - reloadFirstPage 第 N 页归位 offset 0 单发；ip 输入 300ms 防抖；resetFields 任何路径单发。
 *  - cellRenderer 结构：拦截次数 kk-num kk-bad（告警读数）、IP kk-num、帧类型中文映射。
 *  - 轮询：autoRefresh=false 停表；静默失败 pollFailed 置位不刷 toast。
 */
// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { defineComponent, nextTick } from "vue";
import { mount } from "@vue/test-utils";
import { clearPolls } from "@/utils/kkPoll";

const h = vi.hoisted(() => ({
  listIpRejects: vi.fn(),
  exportIpRejects: vi.fn(),
  message: vi.fn(),
  downloadBlob: vi.fn()
}));

vi.mock("@/utils/http", () => ({ http: { request: vi.fn() } }));
vi.mock("@/utils/message", () => ({ message: h.message }));
vi.mock("@/api/exporting", () => ({ exportIpRejects: h.exportIpRejects }));
vi.mock("@/api/ipwhitelist", () => ({ listIpRejects: h.listIpRejects }));
vi.mock("@/utils/kk", async () => {
  const actual = await vi.importActual<any>("@/utils/kk");
  return { ...actual, downloadBlob: h.downloadBlob };
});

import { rejectKindText, useIpRejects } from "./hook";

type Api = ReturnType<typeof useIpRejects>;
let api: Api;

const settle = async () => {
  await nextTick();
  await vi.advanceTimersByTimeAsync(0);
  await nextTick();
};

function withHook() {
  const Comp = defineComponent({
    setup() {
      api = useIpRejects();
      return () => null;
    }
  });
  return mount(Comp);
}

const pageOf = (items: any[], total = items.length) => ({
  items,
  total,
  offset: 0,
  limit: 100
});
const row = (over: Record<string, any> = {}) => ({
  ip: "192.0.2.1",
  hits: 3,
  pod: "h1",
  kind: "hb",
  first_seen: 1690000000,
  last_seen: 1690000100,
  ...over
});

beforeEach(() => {
  vi.useFakeTimers();
  clearPolls();
  h.listIpRejects.mockReset();
  h.exportIpRejects.mockReset();
  h.message.mockReset();
  h.downloadBlob.mockReset();
  h.listIpRejects.mockResolvedValue(pageOf([row()]));
  h.exportIpRejects.mockResolvedValue(new Blob(["x"]));
});

afterEach(() => {
  clearPolls();
  vi.useRealTimers();
});

describe("筛选下推与导出同参", () => {
  it("ip trim 下推，空值 undefined", async () => {
    withHook();
    await settle();
    h.listIpRejects.mockClear();
    api.form.ip = "  192.0.2  ";
    api.reloadFirstPage();
    await settle();
    expect(h.listIpRejects.mock.calls[0][0]).toMatchObject({
      ip: "192.0.2",
      offset: 0
    });

    api.form.ip = "";
    api.reloadFirstPage();
    await settle();
    expect(h.listIpRejects.mock.calls.at(-1)[0].ip).toBeUndefined();
  });

  it("导出带当前 ip 筛选（所见即所得）", async () => {
    withHook();
    await settle();
    api.form.ip = "192.0.2";
    await api.onExport();
    expect(h.exportIpRejects).toHaveBeenCalledWith(
      expect.objectContaining({ ip: "192.0.2" })
    );
    expect(h.downloadBlob).toHaveBeenCalled();
  });
});

describe("归位与防抖", () => {
  it("reloadFirstPage 第 N 页 → offset 0 单发", async () => {
    withHook();
    await settle();
    api.pagination.currentPage = 5;
    h.listIpRejects.mockClear();
    api.reloadFirstPage();
    await settle();
    expect(api.pagination.currentPage).toBe(1);
    expect(h.listIpRejects).toHaveBeenCalledTimes(1);
    expect(h.listIpRejects.mock.calls[0][0].offset).toBe(0);
  });

  it("ip 输入 300ms 防抖归位", async () => {
    withHook();
    await settle();
    h.listIpRejects.mockClear();
    api.form.ip = "10";
    await nextTick();
    expect(h.listIpRejects).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(300);
    await settle();
    expect(h.listIpRejects).toHaveBeenCalledTimes(1);
  });

  it("resetFields：有值走防抖单发，全空直接归位单发", async () => {
    withHook();
    await settle();
    api.form.ip = "10";
    await vi.advanceTimersByTimeAsync(300);
    await settle();
    h.listIpRejects.mockClear();
    api.resetFields();
    await vi.advanceTimersByTimeAsync(300);
    await settle();
    expect(h.listIpRejects).toHaveBeenCalledTimes(1);
    expect(api.form.ip).toBe("");

    h.listIpRejects.mockClear();
    api.resetFields();
    await settle();
    expect(h.listIpRejects).toHaveBeenCalledTimes(1);
  });
});

describe("列渲染结构", () => {
  it("拦截次数 kk-num kk-bad（告警读数）；IP kk-num", async () => {
    withHook();
    await settle();
    const hits = (
      api.columns.find((c: any) => c.label === "拦截次数") as any
    ).cellRenderer({ row: row({ hits: 7 }) });
    expect(hits.props.class).toBe("kk-num kk-bad");
    expect(hits.children[0]).toBe(7);
    const ip = (
      api.columns.find((c: any) => c.label === "IP") as any
    ).cellRenderer({ row: row() });
    expect(ip.props.class).toBe("kk-num");
  });

  it("帧类型中文映射", async () => {
    withHook();
    await settle();
    const kind = (api.columns.find((c: any) => c.label === "帧类型") as any)
      .cellRenderer;
    expect(kind({ row: row({ kind: "status" }) })).toBe("状态帧");
    expect(kind({ row: row({ kind: "hb" }) })).toBe("心跳帧");
    expect(kind({ row: row({ kind: "result" }) })).toBe("结果帧");
    expect(kind({ row: row({ kind: "x" }) })).toBe("x");
  });
});

describe("rejectKindText 纯函数", () => {
  it("三键 + 空值兜底", () => {
    expect(rejectKindText("status")).toBe("状态帧");
    expect(rejectKindText("hb")).toBe("心跳帧");
    expect(rejectKindText("result")).toBe("结果帧");
    expect(rejectKindText("")).toBe("-");
    expect(rejectKindText("unknown")).toBe("unknown");
  });
});

describe("轮询与失败变冷", () => {
  it("autoRefresh=false 停表", async () => {
    withHook();
    await settle();
    api.autoRefresh.value = false;
    api.restartTimer();
    h.listIpRejects.mockClear();
    await vi.advanceTimersByTimeAsync(60000);
    expect(h.listIpRejects).not.toHaveBeenCalled();
  });

  it("静默失败 pollFailed 置位不刷 toast", async () => {
    withHook();
    await settle();
    h.listIpRejects.mockRejectedValue(new Error("down"));
    await api.onSearch(true);
    expect(api.pollFailed.value).toBe(true);
    expect(h.message).not.toHaveBeenCalled();
  });
});
