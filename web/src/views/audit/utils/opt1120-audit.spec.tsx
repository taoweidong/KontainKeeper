/** QR1120-W26 / W33 的回归锁：审计页两项新语义。
 *
 *  - W26：明细 JSON 每行只解析一次——load 成功整批预解析挂 WeakMap，
 *    cellRenderer 重复渲染（翻页回来 / 密度切换 / 刷新重绘）不再碰 JSON.parse；
 *    未经 load 的行对象首次渲染解析一次并回填，之后同样查表。
 *  - W33：reloadFirstPage 对外导出，搜索按钮与关键字防抖共用同一条归位语义
 *    （在第 N 页点搜索必须回第 1 页，否则「查不到刚输的关键字」）。
 */
// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { defineComponent, nextTick } from "vue";
import { mount } from "@vue/test-utils";

const h = vi.hoisted(() => ({
  listAudit: vi.fn(),
  exportAudit: vi.fn(),
  message: vi.fn(),
  downloadBlob: vi.fn()
}));

vi.mock("@/utils/http", () => ({ http: { request: vi.fn() } }));
vi.mock("@/utils/message", () => ({ message: h.message }));
vi.mock("@/api/exporting", () => ({ exportAudit: h.exportAudit }));
// parseDetail 保持真实实现（W26 要数的就是它内部的 JSON.parse）
vi.mock("@/api/audit", async () => {
  const actual = await vi.importActual<any>("@/api/audit");
  return { ...actual, listAudit: h.listAudit };
});
vi.mock("@/utils/kk", async () => {
  const actual = await vi.importActual<any>("@/utils/kk");
  return { ...actual, downloadBlob: h.downloadBlob };
});

import { useAuditLog } from "./hook";

type AuditApi = ReturnType<typeof useAuditLog>;
let api: AuditApi;

const settle = async () => {
  await nextTick();
  await vi.advanceTimersByTimeAsync(0);
  await nextTick();
};

function withHook() {
  const Comp = defineComponent({
    setup() {
      api = useAuditLog();
      return () => null;
    }
  });
  return mount(Comp);
}

const detailRow = (id: number, detail: string) => ({
  id,
  actor: "admin",
  action: "cmd.dispatch",
  detail,
  ts: id * 10
});
const pageOf = (items: any[], total = items.length) => ({
  items,
  total,
  offset: 0,
  limit: 200
});

/** 明细列渲染器（columns 里 label=「明细」那列） */
const detailRenderer = () =>
  (api.columns.find((c: any) => c.label === "明细") as any).cellRenderer;

/** fragment 的 children 是嵌套数组（map 结果 + 条件占位），拍平并滤掉 false */
const kvSpans = (vnode: any): any[] =>
  (vnode.children as any[]).flat(Infinity).filter(Boolean);

beforeEach(() => {
  vi.useFakeTimers();
  h.listAudit.mockReset();
  h.exportAudit.mockReset();
  h.message.mockReset();
  h.downloadBlob.mockReset();
  h.listAudit.mockResolvedValue(pageOf([]));
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("W26：明细每行只解析一次", () => {
  it("load 成功整批预解析；渲染器重复调用不再触发 JSON.parse", async () => {
    const rows = [
      detailRow(1, '{"pods":2,"kind":"shell"}'),
      detailRow(2, '{"batch":"ug-1"}'),
      detailRow(3, "") // 空明细：parseDetail 直接返回 {}，不碰 JSON.parse
    ];
    h.listAudit.mockResolvedValue(pageOf(rows));
    const parseSpy = vi.spyOn(JSON, "parse");

    withHook();
    await settle();

    // 预解析发生在 load 那一路：两条非空明细各解析一次
    expect(parseSpy).toHaveBeenCalledTimes(2);
    parseSpy.mockClear();

    // 模拟翻页回来 / 密度切换 / 刷新重绘：同一批行对象渲染 5 遍
    const render = detailRenderer();
    for (let i = 0; i < 5; i++) {
      for (const r of rows) render({ row: r });
    }
    expect(parseSpy).not.toHaveBeenCalled(); // 全部查表，零重复解析
  });

  it("渲染结果仍是解析后的键值对（缓存没有改变展示语义）", async () => {
    const r = detailRow(1, '{"pods":2,"kind":"shell"}');
    h.listAudit.mockResolvedValue(pageOf([r]));
    withHook();
    await settle();

    const spans = kvSpans(detailRenderer()({ row: r }));
    // kk-kv span 列表：b 文本 = 键名
    expect(spans).toHaveLength(2);
    expect(spans[0].children[0].children[0]).toBe("pods");
    expect(spans[1].children[0].children[0]).toBe("kind");
  });

  it("未经 load 的行对象：首次渲染解析一次并回填，第二次起查表", async () => {
    withHook();
    await settle();
    const parseSpy = vi.spyOn(JSON, "parse");

    const stray = detailRow(99, '{"x":1}');
    const render = detailRenderer();
    render({ row: stray });
    expect(parseSpy).toHaveBeenCalledTimes(1);

    render({ row: stray });
    render({ row: stray });
    expect(parseSpy).toHaveBeenCalledTimes(1); // 至多一次
  });

  it("坏 JSON 走 parseDetail 的「原文」回落，且同样只解析一次", async () => {
    const r = detailRow(1, "{不是 JSON");
    h.listAudit.mockResolvedValue(pageOf([r]));
    const parseSpy = vi.spyOn(JSON, "parse");

    withHook();
    await settle();
    expect(parseSpy).toHaveBeenCalledTimes(1); // 尝试解析失败 → 回落 {原文: ...}
    parseSpy.mockClear();

    const spans = kvSpans(detailRenderer()({ row: r }));
    expect(spans[0].children[0].children[0]).toBe("原文");
    expect(parseSpy).not.toHaveBeenCalled();
  });
});

describe("W33：搜索与防抖共用归位语义", () => {
  it("reloadFirstPage 对外导出：第 N 页调用 → 归第 1 页、offset 0 只打一发", async () => {
    withHook();
    await settle();
    h.listAudit.mockClear();

    api.pagination.currentPage = 5;
    api.reloadFirstPage();
    await settle();

    expect(api.pagination.currentPage).toBe(1);
    expect(h.listAudit).toHaveBeenCalledTimes(1);
    expect(h.listAudit.mock.calls[0][0]).toMatchObject({ offset: 0 });
  });

  it("带关键字在第 N 页归位：下推 trim 后的关键字（搜索按钮的新行为）", async () => {
    withHook();
    await settle();
    api.form.keyword = "  login ";
    await nextTick();
    await vi.advanceTimersByTimeAsync(300); // 防抖那路先归位一次
    await settle();
    h.listAudit.mockClear();

    api.pagination.currentPage = 3; // 用户又翻到第 3 页
    api.reloadFirstPage(); // 等价于点「搜索」按钮
    await settle();

    expect(api.pagination.currentPage).toBe(1);
    expect(h.listAudit.mock.calls[0][0]).toEqual({
      limit: 200,
      offset: 0,
      keyword: "login"
    });
  });
});