/** QR1121-W14 / W15 / W16 的回归锁：审计页筛选下推、重置单发、长值截断。
 *
 *  - W14：actor/action 独立筛选下推 listAudit 与 exportAudit 同参（导出=所见）；
 *    action 下拉变化立即归第 1 页只打一发，actor 逐字输入走 300ms 防抖。
 *  - W15：resetFields 三字段任何路径只发一发（文本有值走防抖、全空/仅 action
 *    直接归位），resetting 闸压住 action 的同步 watch 不叠加。
 *  - W16：明细长值（>120 字符）截断 + tooltip 展开，短值渲染结构逐字不变
 *    （QR1120-W26 锁的前提）；动作列中文映射不碰判级类名与根节点结构。
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

const pageOf = (items: any[], total = items.length) => ({
  items,
  total,
  offset: 0,
  limit: 200
});
const detailRow = (id: number, detail: string) => ({
  id,
  actor: "admin",
  action: "command_create",
  detail,
  ts: id * 10
});

const detailRenderer = () =>
  (api.columns.find((c: any) => c.label === "明细") as any).cellRenderer;
const actionRenderer = () =>
  (api.columns.find((c: any) => c.label === "动作") as any).cellRenderer;
const kvSpans = (vnode: any): any[] =>
  (vnode.children as any[]).flat(Infinity).filter(Boolean);

beforeEach(() => {
  vi.useFakeTimers();
  h.listAudit.mockReset();
  h.exportAudit.mockReset();
  h.message.mockReset();
  h.downloadBlob.mockReset();
  h.listAudit.mockResolvedValue(pageOf([]));
  h.exportAudit.mockResolvedValue(new Blob(["x"]));
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("W14：actor/action 下推，列表与导出同参", () => {
  it("筛选值 trim 后下推 listAudit；空值下推 undefined", async () => {
    withHook();
    await settle();
    h.listAudit.mockClear();

    api.form.actor = "  admin  ";
    api.form.action = "login_ok";
    api.reloadFirstPage();
    await settle();

    expect(h.listAudit.mock.calls[0][0]).toMatchObject({
      actor: "admin",
      action: "login_ok",
      offset: 0
    });
  });

  it("导出带同一套筛选（导出=所见）", async () => {
    withHook();
    await settle();
    api.form.actor = "admin";
    api.form.action = "login_ok";
    api.form.keyword = " kw ";
    await api.onExport();
    expect(h.exportAudit).toHaveBeenCalledWith({
      keyword: "kw",
      actor: "admin",
      action: "login_ok",
      limit: 200
    });
  });

  it("action 下拉变化立即归第 1 页只打一发（不走防抖）", async () => {
    withHook();
    await settle();
    api.pagination.currentPage = 3;
    h.listAudit.mockClear();

    api.form.action = "login_ok"; // flush:sync watch 立即归位
    await settle();

    expect(api.pagination.currentPage).toBe(1);
    expect(h.listAudit).toHaveBeenCalledTimes(1);
    expect(h.listAudit.mock.calls[0][0]).toMatchObject({
      action: "login_ok",
      offset: 0
    });
  });

  it("actor 逐字输入走 300ms 防抖归位（与 keyword 同族）", async () => {
    withHook();
    await settle();
    h.listAudit.mockClear();

    api.form.actor = "ops";
    await nextTick();
    expect(h.listAudit).not.toHaveBeenCalled(); // 防抖窗口内不打
    await vi.advanceTimersByTimeAsync(300);
    await settle();
    expect(h.listAudit).toHaveBeenCalledTimes(1);
    expect(h.listAudit.mock.calls[0][0]).toMatchObject({
      actor: "ops",
      offset: 0
    });
  });

  it("hasFilter 纳入三字段（空态 filtered 分支的数据源）", async () => {
    withHook();
    await settle();
    expect(api.hasFilter.value).toBe(false);
    api.form.action = "login_ok";
    expect(api.hasFilter.value).toBe(true);
  });
});

describe("W15：resetFields 任何路径只发一发", () => {
  it("三字段都有值 → 清空后走防抖统一重载一发（不叠加 action 同步 watch）", async () => {
    withHook();
    await settle();
    api.form.keyword = "kw";
    api.form.actor = "admin";
    api.form.action = "login_ok";
    await settle(); // action 的立即重载先发生
    h.listAudit.mockClear();

    api.resetFields();
    expect(api.form.keyword).toBe("");
    expect(api.form.actor).toBe("");
    expect(api.form.action).toBe("");
    expect(h.listAudit).not.toHaveBeenCalled(); // 交给防抖
    await vi.advanceTimersByTimeAsync(300);
    await settle();
    expect(h.listAudit).toHaveBeenCalledTimes(1);
    expect(h.listAudit.mock.calls[0][0]).toMatchObject({
      keyword: undefined,
      actor: undefined,
      action: undefined,
      offset: 0
    });
  });

  it("全空 → 直接归位重载一发", async () => {
    withHook();
    await settle();
    h.listAudit.mockClear();
    api.resetFields();
    await settle();
    expect(h.listAudit).toHaveBeenCalledTimes(1);
  });

  it("仅 action 有值 → 清空后直接归位一发（文本防抖不触发）", async () => {
    withHook();
    await settle();
    api.form.action = "login_ok";
    await settle();
    h.listAudit.mockClear();

    api.resetFields();
    await settle();
    expect(h.listAudit).toHaveBeenCalledTimes(1);
    expect(api.form.action).toBe("");
  });
});

describe("W16：明细长值截断与动作中文（结构锁兼容）", () => {
  it("短值渲染结构逐字不变（W26 锁的前提）：键名节点 + 纯文本值", async () => {
    h.listAudit.mockResolvedValue(
      pageOf([detailRow(1, '{"pods":2,"kind":"shell"}')])
    );
    withHook();
    await settle();
    const spans = kvSpans(detailRenderer()({ row: api.dataList.value[0] }));
    expect(spans[0].children[0].children[0]).toBe("pods");
    // 短值：值节点是纯文本（末子节点），整段没有 tooltip 对象节点
    expect(spans[0].children.at(-1)).toBe("2");
    expect(
      spans[0].children.some(
        (c: any) => c && typeof c === "object" && c.props?.content !== undefined
      )
    ).toBe(false);
  });

  it("长值（>120 字符）截断加 …，tooltip 带完整值；键名节点不动", async () => {
    const long = "x".repeat(150);
    h.listAudit.mockResolvedValue(
      pageOf([detailRow(1, JSON.stringify({ cmdline: long }))])
    );
    withHook();
    await settle();
    const spans = kvSpans(detailRenderer()({ row: api.dataList.value[0] }));
    expect(spans[0].children[0].children[0]).toBe("cmdline");
    // JSX 的 ":" 与 {" "} 是两个文本节点，tooltip 位置不写死下标，按 content 找
    const valNode = spans[0].children.find(
      (c: any) => c && typeof c === "object" && c.props && "content" in c.props
    );
    expect(valNode.props.content).toBe(long); // tooltip 完整值
    const kids = Array.isArray(valNode.children)
      ? valNode.children
      : valNode.children.default();
    const text = Array.isArray(kids[0].children)
      ? kids[0].children[0]
      : kids[0].children;
    expect(text).toBe("x".repeat(120) + "…");
  });

  it("动作列：中文映射 + 原串 title，判级类名与根节点结构不变", async () => {
    withHook();
    await settle();
    const v = actionRenderer()({ row: { action: "login_ok" } });
    expect(v.props.class).toBe("kk-state"); // ok → 绿点基类（判级不碰）
    expect(v.children[0]).toBe("登录成功");
    expect(v.props.title).toBe("login_ok");

    const bad = actionRenderer()({ row: { action: "login_locked" } });
    expect(bad.props.class).toBe("kk-state kk-state--bad kk-bad");
    expect(bad.children[0]).toBe("登录锁定");
  });

  it("未知动作原样回显、无 title（兜底不编文案）", async () => {
    withHook();
    await settle();
    const v = actionRenderer()({ row: { action: "future_action" } });
    expect(v.children[0]).toBe("future_action");
    expect(v.props.title).toBeUndefined();
  });
});
