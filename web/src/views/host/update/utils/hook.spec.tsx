/** views/host/update/utils/hook.tsx 的单元测试：版本与更新页的数据流（直接驱动 hook）。
 *
 *  锁住的语义：
 *  - `fleetInfo` 的三态（QR-W16 同源）：没读到 → 全「—」；读到了但**还没有待分发版本** →
 *    「落后 N 台」必须是「—」而不是 0（后端注释写明「没上传过版本时 0 表示无从定义」）；
 *    `hosts_total` 不受此限，它始终是真读数，0 就该显示 0。
 *  - 升级台账是独立一路：`listUpdates` 失败只让 `ledgerRead` 落 false，
 *    不许连带把主表谎称「没有落后主机」。
 *  - 前端分页只有一个状态源：`total` 取清单长度、切片是 computed、翻页不打后端；
 *    清单变短后当前页越界要归位（clampPage）。
 *  - 勾选模型跨数据刷新对齐（丢掉已不再落后的主机），`?pods=` 预填只应用一次。
 *  - `onUpgrade` 的三道门禁：没勾选 / 没待分发版本 / 用户取消 —— 都不许发请求；
 *    成功路径 `upgrading` 必复位，离线台数走 queued 文案（B6）。
 *  - 跳过明细超过 10 条按原因聚合（QR-W9），主机名走文本节点不拼 HTML。
 */
// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { defineComponent, nextTick } from "vue";
import { mount } from "@vue/test-utils";

import { clearPolls, pollKeys } from "@/utils/kkPoll";

const h = vi.hoisted(() => ({
  route: { query: {} as Record<string, string> },
  message: vi.fn(),
  listHosts: vi.fn(),
  getAgentCurrent: vi.fn(),
  listUpdates: vi.fn(),
  upgradeHosts: vi.fn(),
  confirm: vi.fn(),
  alert: vi.fn()
}));

vi.mock("vue-router", () => ({ useRoute: () => h.route }));
vi.mock("element-plus", () => ({ ElMessageBox: { confirm: h.confirm, alert: h.alert } }));
vi.mock("@/utils/http", () => ({ http: { request: vi.fn() } }));
vi.mock("@/utils/message", () => ({ message: h.message }));
vi.mock("@/api/containers", () => ({ listHosts: (...a: any[]) => h.listHosts(...a) }));
vi.mock("@/api/agent", () => ({
  getAgentCurrent: () => h.getAgentCurrent(),
  listUpdates: (...a: any[]) => h.listUpdates(...a),
  upgradeHosts: (...a: any[]) => h.upgradeHosts(...a)
}));

import { useHostUpdate } from "./hook";

type UpdateApi = ReturnType<typeof useHostUpdate>;
let api: UpdateApi;

const settle = async () => {
  await nextTick();
  await vi.advanceTimersByTimeAsync(0);
  await nextTick();
};

function withHook() {
  const Comp = defineComponent({
    setup() {
      api = useHostUpdate();
      return () => null;
    }
  });
  return mount(Comp);
}

const host = (pod: string, over: Record<string, any> = {}) => ({
  pod,
  agent_ver: "1.0.0",
  latest_agent_ver: "1.1.0",
  agent_outdated: true,
  online: true,
  age_sec: 12,
  status_reason: "",
  ...over
});
const hostsOf = (items: any[]) => ({
  items,
  total: items.length,
  online: items.filter(x => x.online).length,
  alerts: 0,
  outdated: items.filter(x => x.agent_outdated).length
});
const many = (n: number) => Array.from({ length: n }, (_, i) => host(`h${i}`));

beforeEach(() => {
  vi.useFakeTimers();
  clearPolls();
  h.route.query = {};
  for (const fn of [h.message, h.listHosts, h.getAgentCurrent, h.listUpdates, h.upgradeHosts, h.confirm, h.alert])
    fn.mockReset();
  h.listHosts.mockResolvedValue(hostsOf([host("a"), host("b", { agent_outdated: false })]));
  h.getAgentCurrent.mockResolvedValue({
    version: "1.1.0", hosts_total: 2, hosts_outdated: 1, sha256: "s", size: 1, uploaded_at: 0
  });
  h.listUpdates.mockResolvedValue({ items: [], summary: {}, limit: 50 });
  h.confirm.mockResolvedValue(undefined);
  h.upgradeHosts.mockResolvedValue({ ok: true, batch_id: "ug-1-1", accepted: [], skipped: [] });
  h.alert.mockResolvedValue(undefined);
});

afterEach(() => {
  clearPolls();
  vi.useRealTimers();
});

describe("fleetInfo 三态与两条读数的独立性", () => {
  it("没读到之前一切是 —（渲染 0 就是编数据）", async () => {
    withHook();
    expect(api.fleetInfo.value).toEqual({ version: "—", outdated: "—", total: "—", cls: "" });
    expect(api.read.value).toBe(false);
  });

  it("读到但没有待分发版本：outdated 仍是 —，total 用真读数 0", async () => {
    h.getAgentCurrent.mockResolvedValue({ version: "", hosts_total: 0, hosts_outdated: 0 });
    withHook();
    await settle();

    expect(api.read.value).toBe(true);
    expect(api.hasLatest.value).toBe(false);
    expect(api.fleetInfo.value).toEqual({ version: "（未上传）", outdated: "—", total: 0, cls: "" });
  });

  it("有版本时 outdated 用真值：落后 >0 → kk-bad，0 台 → kk-ok", async () => {
    withHook();
    await settle();
    expect(api.fleetInfo.value).toEqual({ version: "1.1.0", outdated: 1, total: 2, cls: "kk-bad" });

    h.getAgentCurrent.mockResolvedValue({ version: "1.1.0", hosts_total: 5, hosts_outdated: 0 });
    await api.load();
    expect(api.fleetInfo.value).toEqual({ version: "1.1.0", outdated: 0, total: 5, cls: "kk-ok" });
  });

  it("台账接口失败只让 ledgerRead 落 false；主表只留 agent_outdated 的行", async () => {
    h.listUpdates.mockRejectedValue(new Error("500"));
    withHook();
    await settle();

    expect(api.read.value).toBe(true);
    expect(api.ledgerRead.value).toBe(false);
    expect(api.outdated.value.map(x => x.pod)).toEqual(["a"]);
  });

  it("主表接口失败：read 保持 false、非静默路径给可读错误、loading 复位", async () => {
    h.getAgentCurrent.mockRejectedValue({ response: { data: { detail: "未上传版本" } } });
    withHook();
    await settle();

    expect(api.read.value).toBe(false);
    expect(api.loading.value).toBe(false);
    expect(h.message).toHaveBeenCalledWith("加载升级信息失败：未上传版本", { type: "error" });
  });
});

describe("前端分页与轮询", () => {
  it("翻页只切片不打后端；total 来自清单长度；清单变短后当前页归位最后一页", async () => {
    h.listHosts.mockResolvedValue(hostsOf(many(60)));
    withHook();
    await settle();
    expect(api.pagination.total).toBe(60);

    h.listHosts.mockClear();
    api.pagination.currentPage = 2;
    await settle();

    expect(api.pagedOutdated.value).toHaveLength(10);   // 60 - 50
    expect(api.pagedOutdated.value[0].pod).toBe("h50");
    expect(h.listHosts).not.toHaveBeenCalled();         // 翻页纯前端
  });

  it("数据变少后页码越界 → clamp 到有内容的那一页而不是空表", async () => {
    h.listHosts.mockResolvedValue(hostsOf(many(120)));
    withHook();
    await settle();
    api.pagination.currentPage = 3;
    await settle();

    h.listHosts.mockResolvedValue(hostsOf([host("only")]));
    await api.load();

    expect(api.pagination.currentPage).toBe(1);
    expect(api.pagedOutdated.value.map(x => x.pod)).toEqual(["only"]);
  });

  it("轮询间隔 = interval*1000；静默轮询失败只置 pollFailed 不刷 toast（W5）", async () => {
    withHook();
    await settle();
    expect(pollKeys()).toEqual(["host-update"]);

    api.interval.value = 30;
    await settle();
    expect(pollKeys()).toEqual(["host-update"]);   // 只重建这一路

    h.listHosts.mockClear();
    vi.advanceTimersByTime(29000);
    await settle();
    expect(h.listHosts).not.toHaveBeenCalled();
    vi.advanceTimersByTime(1000);
    await settle();
    expect(h.listHosts).toHaveBeenCalledTimes(1);

    h.getAgentCurrent.mockRejectedValue(new Error("boom"));
    vi.advanceTimersByTime(30000);
    await settle();
    expect(api.pollFailed.value).toBe(true);
    expect(h.message).not.toHaveBeenCalled();
  });
});

describe("勾选模型与 ?pods= 预填", () => {
  it("全选 → 模型与读数同步；数据刷新后丢掉已不再落后的主机；clearSelection 清干净", async () => {
    h.listHosts.mockResolvedValue(hostsOf([host("a"), host("b", { online: false })]));
    withHook();
    await settle();

    api.selectAllOutdated();
    expect(api.selection.value.map(x => x.pod)).toEqual(["a", "b"]);
    expect(api.offlineSelected.value).toBe(1);
    expect(h.message.mock.calls.at(-1)[0]).toContain("已选 2 台落后主机");

    h.listHosts.mockResolvedValue(hostsOf([host("a")]));
    await api.load();
    expect(api.selection.value.map(x => x.pod)).toEqual(["a"]);   // 展示与提交都不引用过期数据

    api.clearSelection();
    expect(api.selection.value).toEqual([]);
    expect(api.offlineSelected.value).toBe(0);
  });

  it("?pods= 预填只认当前落后清单里的主机，且只应用一次（轮询不冲掉手改勾选）", async () => {
    h.route.query = { pods: " a ,ghost, b" };
    h.listHosts.mockResolvedValue(hostsOf([host("a"), host("b")]));
    withHook();
    await settle();

    expect(api.selection.value.map(x => x.pod)).toEqual(["a", "b"]);   // ghost 被忽略

    api.clearSelection();
    await api.load();
    expect(api.selection.value).toEqual([]);
  });
});

describe("批量升级的门禁与回执", () => {
  it("三道门禁都不发请求：没勾选 / 没有待分发版本 / 用户取消确认", async () => {
    withHook();
    await settle();

    await api.onUpgrade();
    expect(h.message).toHaveBeenCalledWith("请先勾选要升级的主机", { type: "warning" });

    h.getAgentCurrent.mockResolvedValue({ version: "", hosts_total: 3, hosts_outdated: 0 });
    await api.load();
    api.selection.value = [host("a") as any];
    h.message.mockClear();
    await api.onUpgrade();
    expect(h.message).toHaveBeenCalledWith("服务端没有待分发版本，先去上传", { type: "warning" });

    h.getAgentCurrent.mockResolvedValue({ version: "1.1.0", hosts_total: 3, hosts_outdated: 1 });
    await api.load();
    h.confirm.mockRejectedValue("cancel");
    api.selection.value = [host("a") as any];
    await api.onUpgrade();

    expect(h.upgradeHosts).not.toHaveBeenCalled();
    expect(api.upgrading.value).toBe(false);
  });

  it("确认文案带上离线台数（会标 queued 等重连补投，不是失败）", async () => {
    h.listHosts.mockResolvedValue(hostsOf([host("a"), host("b", { online: false })]));
    withHook();
    await settle();

    api.selection.value = [host("a") as any, host("b", { online: false }) as any];
    await api.onUpgrade();

    const warn = h.confirm.mock.calls[0][0] as string;
    expect(warn).toContain("将把 2 台主机升到 1.1.0");
    expect(warn).toContain("其中 1 台离线");
  });

  it("成功后：回执分档（受理/离线补投/跳过）、清空勾选、刷数据、弹明细，upgrading 复位", async () => {
    withHook();
    await settle();
    h.upgradeHosts.mockResolvedValue({
      ok: true,
      batch_id: "ug-2-2",
      accepted: [
        { host: "a", from_version: "1.0.0", to_version: "1.1.0", ledger_id: "l1", queued: false },
        { host: "b", from_version: "1.0.0", to_version: "1.1.0", ledger_id: "l2", queued: true }
      ],
      skipped: [{ host: "c", reason: "already_latest" }]
    });
    api.selection.value = [host("a") as any, host("b") as any];

    await api.onUpgrade();

    expect(h.upgradeHosts).toHaveBeenCalledWith(["a", "b"]);
    expect(h.message).toHaveBeenCalledWith(
      "已受理 2 台，其中 1 台离线，会在重连时自动补投，跳过 1 台（见下方明细）",
      { type: "success" }
    );
    expect(api.selection.value).toEqual([]);
    expect(api.upgrading.value).toBe(false);
    expect(h.alert).toHaveBeenCalled();
  });

  it("失败路径 upgrading 必复位并给可读错误", async () => {
    h.upgradeHosts.mockRejectedValue({ response: { data: { detail: "服务端未连 Broker" } } });
    withHook();
    await settle();

    api.selection.value = [host("a") as any];
    await api.onUpgrade();

    expect(api.upgrading.value).toBe(false);
    expect(h.message).toHaveBeenCalledWith("升级失败：服务端未连 Broker", { type: "error" });
  });
});

describe("跳过明细的聚合（QR-W9）", () => {
  it("10 条以内逐条列出，原因走 upgradeSkipText 取词；空列表不弹空窗", async () => {
    withHook();
    await settle();

    await api.showSkipped([]);
    expect(h.alert).not.toHaveBeenCalled();

    await api.showSkipped([{ host: "c", reason: "no_binary" }]);
    const html = h.alert.mock.calls[0][0] as any;
    expect(h.alert.mock.calls[0][1]).toBe("跳过 1 台");   // alert(message, title, options)
    expect(h.alert.mock.calls[0][2]).toMatchObject({ type: "warning", confirmButtonText: "知道了" });
    expect(html.children.map((p: any) => p.children)).toEqual(["c —— 未上传任何版本"]);
  });

  it("超过 10 条只列前 10 条，其余按原因聚合报数（被跳过的台数不进台账，只能自己归并）", async () => {
    withHook();
    await settle();

    await api.showSkipped(
      Array.from({ length: 13 }, (_, i) => ({
        host: `h${i}`,
        reason: i < 10 ? "already_latest" : "in_flight"
      }))
    );

    const lines = (h.alert.mock.calls[0][0] as any).children.map((p: any) => p.children);
    expect(lines).toHaveLength(11);
    expect(lines[0]).toBe("h0 —— 已是最新");
    expect(lines[9]).toBe("h9 —— 已是最新");
    expect(lines[10]).toBe("另有 3 台：已有升级在途 3 台");
    expect(h.alert.mock.calls[0][1]).toBe("跳过 13 台");
  });

  it("用户点掉弹窗不影响主流程", async () => {
    h.alert.mockRejectedValue("close");
    withHook();
    await settle();
    await expect(api.showSkipped([{ host: "x", reason: "weird" }])).resolves.toBeUndefined();
  });
});

describe("主机状态列三档", () => {
  it("在线绿点 / 更新中方块（B6 唯一合法用处） / 其余空心点", async () => {
    withHook();
    await settle();
    const col = api.columns.find((c: any) => c.label === "状态") as any;
    const render = (over: Record<string, any>) => col.cellRenderer({ row: over });

    expect(render({ online: true, status_reason: "" }).props.class).toBe("kk-state");
    expect(render({ online: false, status_reason: "updating" }).props.class).toBe(
      "kk-state kk-state--updating"
    );
    expect(render({ online: false, status_reason: "updating" }).children[0]).toBe("更新中");
    expect(render({ online: false, status_reason: "stopped" }).props.class).toBe("kk-state kk-state--idle");
  });
});
