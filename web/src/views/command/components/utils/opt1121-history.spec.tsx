/** QR1121-W7 的回归锁：输出抽屉的行快照元信息。
 *
 *  抽屉顶部元信息（下发者/创建/下发/完成/截断/分片）来自列表行快照 out.row，
 *  不另发请求。锁两条语义：
 *  ① showOut 开头即快照——已清理行（早退路径）也有元信息；
 *  ② 连点两行时迟到包守卫下 out.row 仍随最新行，先点的慢响应不会把
 *     标题为 B 的抽屉回填成 A 的行（与 QR-W4 的输出文本守卫同族）。
 *  另锁类型列中文化（QR1121-W19 的列渲染侧，映射本体锁在 utils/kk.spec.ts）。
 */
// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { defineComponent, nextTick, toRaw } from "vue";
import { mount } from "@vue/test-utils";

import { clearPolls } from "@/utils/kkPoll";

const h = vi.hoisted(() => ({
  listCommands: vi.fn(),
  listBatches: vi.fn(),
  getCommandOut: vi.fn(),
  exportCommands: vi.fn(),
  message: vi.fn(),
  downloadBlob: vi.fn()
}));

vi.mock("@/utils/http", () => ({ http: { request: vi.fn() } }));
vi.mock("@/utils/message", () => ({ message: h.message }));
vi.mock("@/api/commands", () => ({
  listCommands: (...a: any[]) => h.listCommands(...a),
  listBatches: (...a: any[]) => h.listBatches(...a),
  getCommandOut: (...a: any[]) => h.getCommandOut(...a)
}));
vi.mock("@/api/exporting", () => ({
  exportCommands: (...a: any[]) => h.exportCommands(...a)
}));
vi.mock("@/utils/kk", async () => {
  const actual = await vi.importActual<any>("@/utils/kk");
  return { ...actual, downloadBlob: h.downloadBlob };
});

import { useCommandHistory } from "./hook";

type HistoryApi = ReturnType<typeof useCommandHistory>;
let api: HistoryApi;

const settle = async () => {
  await nextTick();
  await vi.advanceTimersByTimeAsync(0);
  await nextTick();
};

function withHook() {
  const Comp = defineComponent({
    setup() {
      api = useCommandHistory();
      return () => null;
    }
  });
  return mount(Comp);
}

const row = (over: Record<string, any> = {}): any => ({
  id: "c1",
  pod: "host-a",
  kind: "shell",
  argv: "id",
  status: "done",
  rc: 0,
  elapsed_ms: 10,
  out_purged: 0,
  out_tail: "uid=0",
  created_by: "admin",
  created_at: 1690000000,
  sent_at: 1690000001,
  finished_at: 1690000002,
  truncated: 0,
  out_chunks: 1,
  ...over
});

beforeEach(() => {
  vi.useFakeTimers();
  clearPolls();
  h.listCommands.mockReset();
  h.listBatches.mockReset();
  h.getCommandOut.mockReset();
  h.exportCommands.mockReset();
  h.message.mockReset();
  h.downloadBlob.mockReset();
  h.listCommands.mockResolvedValue({ items: [row()], total: 1 });
  h.listBatches.mockResolvedValue({ items: [] });
  h.getCommandOut.mockResolvedValue("full output");
});

afterEach(() => {
  clearPolls();
  vi.useRealTimers();
});

describe("W7：out.row 快照随最新行", () => {
  it("showOut 开头即快照：元信息字段完整可渲染", async () => {
    withHook();
    await settle();
    const r = row({ id: "meta-1", created_by: "ops", out_chunks: 3 });
    await api.showOut(r);
    // out 是 reactive：读回的是代理，toRaw 解包后断言原始引用（快照本体）
    expect(toRaw(api.out.row)).toBe(r);
    expect(api.out.row!.created_by).toBe("ops");
    expect(api.out.row!.sent_at).toBe(1690000001);
    expect(api.out.row!.finished_at).toBe(1690000002);
    expect(api.out.row!.out_chunks).toBe(3);
  });

  it("已清理行的早退路径也有快照（元信息不依赖输出请求）", async () => {
    withHook();
    await settle();
    const r = row({ id: "purged-1", out_purged: 1 });
    await api.showOut(r);
    expect(toRaw(api.out.row)).toBe(r);
    expect(api.out.text).toContain("输出已按保留策略清理");
    expect(h.getCommandOut).not.toHaveBeenCalled();
  });

  it("连点两行：先点的慢响应迟到，out.row 与 out.text 都随最新行（不回填旧行）", async () => {
    withHook();
    await settle();
    let resolveA!: (v: string) => void;
    h.getCommandOut.mockImplementationOnce(
      () => new Promise<string>(r => (resolveA = r))
    );
    h.getCommandOut.mockResolvedValueOnce("B output");

    const rowA = row({ id: "A", pod: "host-a" });
    const rowB = row({ id: "B", pod: "host-b" });
    const pA = api.showOut(rowA); // A 的请求挂起
    const pB = api.showOut(rowB); // 紧接着点 B
    await pB;
    expect(api.out.row!.id).toBe("B");
    expect(api.out.text).toBe("B output");

    resolveA("A output"); // A 的慢响应此刻才到
    await pA;
    expect(api.out.row!.id).toBe("B"); // 行快照不被迟到包回填
    expect(api.out.text).toBe("B output");
    expect(api.out.title).toContain("B");
  });
});

describe("W19：类型列中文（列渲染侧）", () => {
  it("shell/collect/plugin_reload 中文，未知类型原串兜底", async () => {
    withHook();
    await settle();
    const kindCol = api.columns.find((c: any) => c.label === "类型") as any;
    expect(kindCol.cellRenderer({ row: { kind: "shell" } })).toBe("命令");
    expect(kindCol.cellRenderer({ row: { kind: "collect" } })).toBe("采集");
    expect(kindCol.cellRenderer({ row: { kind: "plugin_reload" } })).toBe(
      "插件重载"
    );
    expect(kindCol.cellRenderer({ row: { kind: "future" } })).toBe("future");
  });
});
