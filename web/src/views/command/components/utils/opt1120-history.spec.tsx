/** QR1120-W22 / W24 / W30 的回归锁：命令历史 hook 的三项新语义。
 *
 *  - W24：大输出切片渲染——超 256K 字符只把头 128K + 尾 64K 交给 DOM，
 *    out.text 仍是全文（下载不受影响）；不超阈值 outView 为 null（照旧全文渲染）。
 *  - W30：hasActive 对外导出，是表头「3 秒一轮 / 10 秒一轮」档位文案的数据源。
 *  - W22：手动刷新失败 pollFailed 恒置位（全站口径），恢复成功后归位——
 *    既有 hook.spec.tsx 已锁失败置位本身，这里补「手动失败 → 恢复」的完整回路。
 */
// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { defineComponent, nextTick } from "vue";
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

import {
  OUT_HEAD_LEN,
  OUT_RENDER_LIMIT,
  OUT_TAIL_LEN,
  sliceOutText,
  useCommandHistory
} from "./hook";

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
  created_at: 1690000000,
  ...over
});
const listOf = (items: any[] = [row()], total = items.length) => ({
  items,
  total,
  offset: 0,
  limit: 50
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
  h.listCommands.mockResolvedValue(listOf());
  h.listBatches.mockResolvedValue({ items: [] });
  h.getCommandOut.mockResolvedValue("full output");
});

afterEach(() => {
  clearPolls();
  vi.useRealTimers();
});

describe("W24：sliceOutText 纯函数边界", () => {
  it("不超阈值（含恰好等于）返回 null：照旧全文渲染，不多一层切片", () => {
    expect(sliceOutText("")).toBeNull();
    expect(sliceOutText("short output")).toBeNull();
    expect(sliceOutText("x".repeat(OUT_RENDER_LIMIT))).toBeNull();
  });

  it("超阈值 1 字符即切：头 128K + 尾 64K，省略数 = 全长 - 头 - 尾", () => {
    const text = "x".repeat(OUT_RENDER_LIMIT + 1);
    const v = sliceOutText(text)!;
    expect(v.head).toHaveLength(OUT_HEAD_LEN);
    expect(v.tail).toHaveLength(OUT_TAIL_LEN);
    expect(v.omitted).toBe(OUT_RENDER_LIMIT + 1 - OUT_HEAD_LEN - OUT_TAIL_LEN);
  });

  it("切片内容正确：头取最前、尾取最后，中段一个字符都不进 DOM", () => {
    const midLen = OUT_RENDER_LIMIT; // 总量 = 128K + 256K + 64K，必超阈值
    const text =
      "H".repeat(OUT_HEAD_LEN) + "M".repeat(midLen) + "T".repeat(OUT_TAIL_LEN);
    const v = sliceOutText(text)!;
    expect(v.head).toBe("H".repeat(OUT_HEAD_LEN));
    expect(v.tail).toBe("T".repeat(OUT_TAIL_LEN));
    expect(v.omitted).toBe(midLen);
    expect(v.head + v.tail).not.toContain("M");
  });
});

describe("W24：抽屉 outView 与全文 out.text 分离", () => {
  it("大输出：out.text 保留全文（下载不受影响），outView 只给头尾", async () => {
    const big = "a".repeat(OUT_RENDER_LIMIT + 100);
    h.getCommandOut.mockResolvedValue(big);
    withHook();
    await settle();

    await api.showOut(row({ id: "big-1" }) as any);

    expect(api.out.text).toBe(big); // 全文仍在，downloadOut 重新拉取不依赖切片
    expect(api.outView.value).not.toBeNull();
    expect(api.outView.value!.head).toHaveLength(OUT_HEAD_LEN);
    expect(api.outView.value!.tail).toHaveLength(OUT_TAIL_LEN);
    expect(api.outView.value!.omitted).toBe(100 + OUT_RENDER_LIMIT - OUT_HEAD_LEN - OUT_TAIL_LEN);
  });

  it("小输出 / 已清理占位 / 读取失败文案：outView 为 null，照旧单 pre 渲染", async () => {
    withHook();
    await settle();

    await api.showOut(row() as any);
    expect(api.outView.value).toBeNull();

    await api.showOut(row({ out_purged: 1 }) as any);
    expect(api.outView.value).toBeNull();

    h.getCommandOut.mockRejectedValue(new Error("boom"));
    await api.showOut(row({ id: "c-err" }) as any);
    expect(api.out.text).toContain("读取输出失败");
    expect(api.outView.value).toBeNull();
  });
});

describe("W30：hasActive 是档位文案的数据源", () => {
  it("全终态 false；任一未终态（pending/sent/running）即 true", async () => {
    withHook();
    await settle();
    expect(api.hasActive.value).toBe(false); // 默认行 status=done

    for (const status of ["pending", "sent", "running"]) {
      api.rows.value = [row({ status })];
      await nextTick();
      expect(api.hasActive.value, status).toBe(true);
    }

    api.rows.value = [row({ status: "failed" }), row({ id: "c2", status: "timeout" })];
    await nextTick();
    expect(api.hasActive.value).toBe(false); // 失败/超时也是终态
  });
});

describe("W22：手动失败 → 恢复的完整回路", () => {
  it("手动失败置位 pollFailed（页头变冷）+ toast；下一次成功（含静默）归位", async () => {
    withHook();
    await settle();
    expect(api.pollFailed.value).toBe(false);

    h.listCommands.mockRejectedValue(new Error("网络抖动"));
    await api.loadCommands(); // 手动
    expect(api.pollFailed.value).toBe(true);
    expect(h.message).toHaveBeenCalledTimes(1);

    // 恢复走静默轮询那路：不弹 toast，但读数必须回暖
    h.message.mockClear();
    h.listCommands.mockResolvedValue(listOf());
    await api.loadCommands(true);
    expect(api.pollFailed.value).toBe(false);
    expect(h.message).not.toHaveBeenCalled();
  });
});