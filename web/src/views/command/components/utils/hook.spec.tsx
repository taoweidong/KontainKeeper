/** views/command/components/utils/hook.tsx 的单元测试：命令历史（shell/collect 两页共用）。
 *
 *  锁住的语义：
 *  - 轮询 key 带实例序号（QR29-W8）：shell 与 collect 两页 keep-alive 下共存，同名 key
 *    会被 setPoll 的「按 key 覆盖」静默掐掉另一页的轮询。
 *  - 自适应节奏：有未终态命令 3s，全终态 10s；`autoRefresh` 关掉就彻底停表。
 *  - 静默轮询失败只置 `pollFailed`，不刷 toast（W5：3s 一轮会把噪音做成刷屏）；
 *    手动刷新失败才给可读错误。
 *  - `read` 与 `pollFailed` 共同支撑「读到过 / 确实为空 / 接口不可达」三态（QR-W16）。
 *  - 抽屉输出走 useSeq 票据：连点两行时先点的慢响应不许填进后点的抽屉（QR-W4）；
 *    点已清理行必须复位 loading，否则抽屉永久转圈（QR29-W3）。
 *  - 分页只有一个状态源，offset 现场推导；筛选全部下推后端（QR-S36）。
 */
// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { defineComponent, nextTick } from "vue";
import { mount } from "@vue/test-utils";

import { clearPolls, pollKeys } from "@/utils/kkPoll";

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
vi.mock("@/api/exporting", () => ({ exportCommands: (...a: any[]) => h.exportCommands(...a) }));
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

/** 只填被测语义用到的字段，其余交给 any（CommandRow 有 15 个必填位，铺满会淹掉重点） */
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
  h.listBatches.mockResolvedValue({ items: [{ batch_id: "ug-1-2", total: 2, done: 2 }] });
  h.getCommandOut.mockResolvedValue("full output");
});

afterEach(() => {
  clearPolls();
  vi.useRealTimers();
});

describe("挂载读数与分页状态源", () => {
  it("挂载即拉列表与批次：默认 50 条、offset 0、无筛选下推 undefined，total 落到 pagination", async () => {
    withHook();
    await settle();

    expect(h.listCommands.mock.calls[0][0]).toEqual({
      status: undefined,
      keyword: undefined,
      batch: undefined,
      limit: 50,
      offset: 0
    });
    expect(h.listBatches).toHaveBeenCalledWith(20);
    expect(api.read.value).toBe(true);
    expect(api.pagination.total).toBe(1);
  });

  it("空表 read 仍为 true（「真的没有记录」与「没读到」分得开）", async () => {
    h.listCommands.mockResolvedValue(listOf([], 0));
    withHook();
    await settle();

    expect(api.read.value).toBe(true);
    expect(api.rows.value).toEqual([]);
  });

  it("翻页与改条数：offset = (currentPage-1)*pageSize，pageSize 变化归位第 1 页", async () => {
    withHook();
    await settle();

    api.pagination.pageSize = 100;
    await settle();
    expect(api.pagination.currentPage).toBe(1);

    api.pagination.currentPage = 3;
    await api.loadCommands();
    expect(h.listCommands.mock.calls.at(-1)[0]).toMatchObject({ limit: 100, offset: 200 });
  });

  it("手动加载失败：给可读错误、loading 复位、read 保持 false，pollFailed 不置位", async () => {
    h.listCommands.mockRejectedValue({ response: { data: { detail: "库连接失败" } } });
    withHook();
    await settle();

    expect(h.message).toHaveBeenCalledWith("加载命令历史失败：库连接失败", { type: "error" });
    expect(api.pollFailed.value).toBe(false);   // 那是静默轮询专用的读数
    expect(api.loading.value).toBe(false);
    expect(api.read.value).toBe(false);
  });
});

describe("自适应轮询（QR29-W8 / W5）", () => {
  it("轮询 key 带实例序号：两页共存互不掐掉，卸载只清自己那路", async () => {
    const first = withHook();
    await settle();
    const key1 = api.POLL_KEY;
    const second = withHook();
    await settle();
    const key2 = api.POLL_KEY;

    expect(key2).not.toBe(key1);              // 同 key 会被 setPoll 覆盖 → 另一页静默停摆
    expect(pollKeys().sort()).toEqual([key1, key2].sort());

    second.unmount();
    expect(pollKeys()).toEqual([key1]);
    first.unmount();
    expect(pollKeys()).toEqual([]);
  });

  it("全终态 10s 一轮；出现未终态 3s 一轮（只按翻转重建定时器）", async () => {
    withHook();
    await settle();
    h.listCommands.mockClear();

    vi.advanceTimersByTime(9000);
    await settle();
    expect(h.listCommands).not.toHaveBeenCalled();

    vi.advanceTimersByTime(1000);
    await settle();
    expect(h.listCommands).toHaveBeenCalledTimes(1);   // 10s 档

    api.rows.value = [row({ status: "running" })];     // 未终态翻转
    await settle();
    h.listCommands.mockClear();
    vi.advanceTimersByTime(3000);
    await settle();
    expect(h.listCommands).toHaveBeenCalledTimes(1);   // 3s 档
  });

  it("autoRefresh=false → restartTimer 停表，一个 tick 都不打", async () => {
    withHook();
    await settle();

    api.autoRefresh.value = false;
    api.restartTimer();
    expect(pollKeys()).toEqual([]);

    h.listCommands.mockClear();
    vi.advanceTimersByTime(30000);
    await settle();
    expect(h.listCommands).not.toHaveBeenCalled();
  });

  it("静默轮询失败只置 pollFailed 且保留旧数据；恢复成功后归位（W5）", async () => {
    withHook();
    await settle();
    h.listCommands.mockRejectedValue(new Error("boom"));

    vi.advanceTimersByTime(10000);
    await settle();

    expect(api.pollFailed.value).toBe(true);
    expect(h.message).not.toHaveBeenCalled();
    expect(api.rows.value).toHaveLength(1);   // 谎报「没有记录」才是事故

    h.listCommands.mockResolvedValue(listOf([], 0));
    await api.loadCommands(true);
    expect(api.pollFailed.value).toBe(false);
  });
});

describe("筛选下推与表头读数", () => {
  it("状态筛选变化 → 归位第 1 页并把 status 下推后端", async () => {
    withHook();
    await settle();

    api.statusFilter.value = "failed";
    await settle();

    expect(h.listCommands.mock.calls.at(-1)[0]).toMatchObject({ status: "failed", offset: 0 });
    expect(api.pagination.currentPage).toBe(1);
  });

  it("关键字防抖 300ms 后带 trim 值下推；纯空格按 undefined", async () => {
    withHook();
    await settle();
    h.listCommands.mockClear();

    api.keyword.value = "  df -h ";
    await nextTick();
    await vi.advanceTimersByTimeAsync(299);
    expect(h.listCommands).not.toHaveBeenCalled();

    await vi.advanceTimersByTimeAsync(300);
    await settle();
    expect(h.listCommands.mock.calls[0][0].keyword).toBe("df -h");

    api.keyword.value = "   ";
    await nextTick();
    await vi.advanceTimersByTimeAsync(300);
    await settle();
    expect(h.listCommands.mock.calls.at(-1)[0].keyword).toBeUndefined();
  });

  it("hasFilter 任一处生效；batchStat 只认已加载批次，未知号返回 null", async () => {
    withHook();
    await settle();
    expect(api.hasFilter.value).toBe(false);
    expect(api.batchStat.value).toBeNull();

    api.statusFilter.value = "done";
    await nextTick();
    expect(api.hasFilter.value).toBe(true);

    api.statusFilter.value = "";
    api.batchFilter.value = "ug-1-2";
    await nextTick();
    expect(api.hasFilter.value).toBe(true);
    expect(api.batchStat.value).toMatchObject({ batch_id: "ug-1-2", total: 2 });

    api.batchFilter.value = "no-such-batch";
    await nextTick();
    expect(api.batchStat.value).toBeNull();
  });

  it("批次列表接口挂掉时清空下拉而不是抛错（不连带掐掉命令列表）", async () => {
    h.listBatches.mockRejectedValue(new Error("500"));
    withHook();
    await settle();

    expect(api.batches.value).toEqual([]);
    expect(api.rows.value).toHaveLength(1);
  });

  it("statNum 把 string|number 联合值统一成数字（缺省 0）；shortBatch 截 8 位、缺号给 -", async () => {
    withHook();
    await settle();

    expect(api.statNum(undefined)).toBe(0);
    expect(api.statNum("3")).toBe(3);
    expect(api.statNum(0)).toBe(0);
    expect(api.shortBatch("ug-1761-500")).toBe("ug-1761-");
    expect(api.shortBatch()).toBe("-");
  });

  it("focusBatch 高亮本次下发的行并把列表收窄到该批次", async () => {
    withHook();
    await settle();

    api.focusBatch("ug-9-2", ["c1", "c2"]);
    await settle();

    expect(api.rowClass({ row: row({ id: "c1" }) })).toBe("kk-row-fresh");
    expect(api.rowClass({ row: row({ id: "zz" }) })).toBe("");
    expect(api.batchFilter.value).toBe("ug-9-2");
    expect(api.pagination.currentPage).toBe(1);
    expect(h.listCommands.mock.calls.at(-1)[0]).toMatchObject({ batch: "ug-9-2", offset: 0 });
    // 现状登记（不改生产码、不锁条数）：focusBatch 自己调 loadCommands()，而它刚改过的
    // batchFilter 又触发 watch([statusFilter, batchFilter]) → reloadFirstPage()，
    // 实测同参数连打两发（hook.tsx:251-258 与 194-197）。数据不会错（useSeq 让前一路失去
    // 写回权），但 500 台批量下发后每次点「定位批次」都多打一发列表请求。
  });
});

describe("输出抽屉（QR-W4 / QR29-W3）", () => {
  it("已清理的行不发请求：占位文案 + loading 必复位", async () => {
    withHook();
    await settle();

    await api.showOut(row({ out_purged: 1 }) as any);

    expect(h.getCommandOut).not.toHaveBeenCalled();
    expect(api.out.visible).toBe(true);
    expect(api.out.text).toContain("输出已按保留策略清理");
    expect(api.out.loading).toBe(false);   // 上一条在途时点已清理行，不许永久转圈
  });

  it("正常行回填标题与正文；空正文显示占位而不是空白抽屉", async () => {
    withHook();
    await settle();

    await api.showOut(row({ id: "c7", pod: "host-b" }) as any);
    expect(h.getCommandOut).toHaveBeenCalledWith("c7");
    expect(api.out.title).toBe("c7 · host-b");
    expect(api.out.text).toBe("full output");
    expect(api.out.loading).toBe(false);

    h.getCommandOut.mockResolvedValue("");
    await api.showOut(row({ id: "c8" }) as any);
    expect(api.out.text).toBe("（无输出）");
  });

  it("连点两行：后点的先回，先点的迟到包被丢弃（标题 B 不许填 A 的输出）", async () => {
    let resolveA!: (v: string) => void;
    h.getCommandOut
      .mockImplementationOnce(
        () =>
          new Promise(r => {
            resolveA = r;
          })
      )
      .mockResolvedValue("B 的输出");

    withHook();
    await settle();

    void api.showOut(row({ id: "A", pod: "p1" }) as any);
    await nextTick();
    await api.showOut(row({ id: "B", pod: "p2" }) as any);

    expect(api.out.text).toBe("B 的输出");
    expect(api.out.title).toBe("B · p2");
    expect(api.out.loading).toBe(false);

    resolveA("A 的输出");
    await settle();
    expect(api.out.text).toBe("B 的输出");   // 迟到包丢弃
    expect(api.out.loading).toBe(false);
  });

  it("读取失败：正文给可读错误且 loading 复位（不许卡在转圈）", async () => {
    h.getCommandOut.mockRejectedValue({ response: { data: { detail: "输出不存在" } } });
    withHook();
    await settle();

    await api.showOut(row({ id: "c9" }) as any);

    expect(api.out.text).toBe("读取输出失败：输出不存在");
    expect(api.out.loading).toBe(false);
  });

  it("下载单条输出走 {id}.txt；失败只提示不落盘", async () => {
    withHook();
    await settle();
    await api.showOut(row({ id: "d1" }) as any);

    await api.downloadOut();
    const [blob, name] = h.downloadBlob.mock.calls[0];
    expect(name).toBe("d1.txt");
    expect(blob).toBeInstanceOf(Blob);

    h.downloadBlob.mockClear();
    h.getCommandOut.mockRejectedValue(new Error("Network Error"));
    await api.downloadOut();
    expect(h.downloadBlob).not.toHaveBeenCalled();
    expect(h.message.mock.calls.at(-1)[0]).toBe("下载输出失败：Network Error");
  });
});

describe("导出与列渲染", () => {
  it("导出带上状态/批次/trim 关键字与 include_tail=1；失败路径 exporting 必复位", async () => {
    h.exportCommands.mockResolvedValue(new Blob(["id\n"]));
    withHook();
    await settle();

    api.statusFilter.value = "failed";
    api.batchFilter.value = "ug-1-2";
    api.keyword.value = " df";
    await nextTick();
    await api.onExport();

    expect(h.exportCommands).toHaveBeenCalledWith({
      status: "failed",
      batch: "ug-1-2",
      keyword: "df",
      include_tail: 1
    });
    expect(h.downloadBlob.mock.calls[0][1]).toMatch(/^命令历史_\d{8}-\d{6}\.csv$/);
    expect(api.exporting.value).toBe(false);

    h.exportCommands.mockRejectedValue({ response: { data: { detail: "导出超限" } } });
    await api.onExport();
    expect(api.exporting.value).toBe(false);
    expect(h.message.mock.calls.at(-1)[0]).toBe("导出失败：导出超限");
  });

  it("argv 预览：列表接口不解析 argv —— 字符串原样、数组空格连接、items 结构点名、其余 JSON", async () => {
    withHook();
    await settle();
    const render = (api.columns.find((c: any) => c.label === "命令 / 采集项") as any).cellRenderer;

    expect(render({ row: { argv: null } })).toBe("-");
    expect(render({ row: { argv: "df -h | sort" } })).toBe("df -h | sort");
    expect(render({ row: { argv: ["/bin/ls", "-l", "a b"] } })).toBe("/bin/ls -l a b");
    expect(render({ row: { argv: { items: ["cpu", "disk"] } } })).toBe("items: cpu, disk");
    expect(render({ row: { argv: { other: 1 } } })).toBe('{"other":1}');
  });

  it("状态列严重度逐档对齐 statusType；rc=null 显示 -（未结束没有 rc，不是 rc=0）", async () => {
    withHook();
    await settle();
    const statusCol = api.columns.find((c: any) => c.label === "状态") as any;
    const cls = (status: string) => statusCol.cellRenderer({ row: { status } }).props.class;
    const rcCol = api.columns.find((c: any) => c.label === "rc") as any;

    expect(cls("failed")).toBe("kk-state kk-state--bad kk-bad");
    expect(cls("timeout")).toBe("kk-state kk-state--bad kk-bad");
    expect(cls("lost")).toBe("kk-state kk-state--bad kk-bad");
    expect(cls("sent")).toBe("kk-state kk-state--warn");
    expect(cls("running")).toBe("kk-state kk-state--warn");   // 与 sent 同档：都是「等结果」
    expect(cls("done")).toBe("kk-state");
    expect(cls("pending")).toBe("kk-state kk-state--idle");
    expect(rcCol.cellRenderer({ row: { rc: null } }).children[0]).toBe("-");
    expect(rcCol.cellRenderer({ row: { rc: 0 } }).children[0]).toBe(0);
  });
});
