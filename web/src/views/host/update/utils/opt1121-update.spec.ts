// @vitest-environment jsdom
/** QR1121-W13 的回归锁：升级汇总读数与停档。
 *
 *  - summaryRows：UpdatesResult.summary 取回后此前从未渲染；按生命周期固定序
 *    过滤有值键（0 值不占版面），纯函数直接驱动。
 *  - 台账失败 → summary 置 {} 且 ledgerRead=false（读数区不渲染而不是补 0），
 *    不连带主表谎报（QR-W16 分路记账口径保持）。
 *  - 间隔停档（与 monitor 对齐）：interval=0 → setPoll(key,fn,0) 只清不建。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { defineComponent, nextTick } from "vue";
import { mount } from "@vue/test-utils";

import { clearPolls } from "@/utils/kkPoll";

const h = vi.hoisted(() => ({
  route: { query: {} as Record<string, any> },
  message: vi.fn(),
  listHosts: vi.fn(),
  getAgentCurrent: vi.fn(),
  listUpdates: vi.fn(),
  upgradeHosts: vi.fn(),
  push: vi.fn(),
  confirm: vi.fn(),
  alert: vi.fn()
}));

vi.mock("vue-router", () => ({
  useRoute: () => h.route,
  useRouter: () => ({ push: h.push })
}));
vi.mock("element-plus", () => ({
  ElMessageBox: { confirm: h.confirm, alert: h.alert }
}));
vi.mock("@/utils/http", () => ({ http: { request: vi.fn() } }));
vi.mock("@/utils/message", () => ({ message: h.message }));
vi.mock("@/api/containers", () => ({
  listHosts: (...a: any[]) => h.listHosts(...a)
}));
vi.mock("@/api/agent", () => ({
  getAgentCurrent: () => h.getAgentCurrent(),
  listUpdates: (...a: any[]) => h.listUpdates(...a),
  upgradeHosts: (...a: any[]) => h.upgradeHosts(...a)
}));

import { summaryRows, useHostUpdate } from "./hook";

const host = (pod: string) => ({
  pod,
  agent_ver: "1.0.0",
  latest_agent_ver: "1.1.0",
  agent_outdated: true,
  online: true,
  age_sec: 12,
  status_reason: ""
});

function withUpdate() {
  let api!: ReturnType<typeof useHostUpdate>;
  const Comp = defineComponent({
    setup() {
      api = useHostUpdate();
      return () => null;
    }
  });
  const wrapper = mount(Comp);
  return { api, wrapper };
}

const settle = async () => {
  await nextTick();
  await vi.advanceTimersByTimeAsync(0);
  await nextTick();
};

beforeEach(() => {
  vi.useFakeTimers();
  clearPolls();
  h.route.query = {};
  for (const fn of [
    h.message,
    h.listHosts,
    h.getAgentCurrent,
    h.listUpdates,
    h.upgradeHosts,
    h.push
  ])
    fn.mockReset();
  h.listHosts.mockResolvedValue({
    items: [host("a")],
    total: 1,
    online: 1,
    alerts: 0,
    outdated: 1
  });
  h.getAgentCurrent.mockResolvedValue({
    version: "1.1.0",
    hosts_total: 1,
    hosts_outdated: 1,
    sha256: "s",
    size: 1,
    uploaded_at: 0
  });
  h.listUpdates.mockResolvedValue({ items: [], summary: {}, limit: 50 });
});

afterEach(() => {
  clearPolls();
  vi.useRealTimers();
});

describe("W13：summaryRows 纯函数", () => {
  it("按生命周期固定序过滤有值键（0 值与未知键不渲染）", () => {
    expect(
      summaryRows({ done: 3, pending: 1, failed: 2, queued: 0, weird: 9 })
    ).toEqual([
      { status: "pending", count: 1 },
      { status: "done", count: 3 },
      { status: "failed", count: 2 }
    ]);
  });

  it("空 summary → 空数组（没发生的事不占版面）", () => {
    expect(summaryRows({})).toEqual([]);
    expect(summaryRows({ done: 0 })).toEqual([]);
  });
});

describe("W13：台账汇总记账", () => {
  it("台账成功 → updatesSummary 落位、ledgerRead=true", async () => {
    h.listUpdates.mockResolvedValue({
      items: [],
      summary: { done: 5, failed: 1 },
      limit: 50
    });
    const { api, wrapper } = withUpdate();
    await settle();
    expect(api.updatesSummary.value).toEqual({ done: 5, failed: 1 });
    expect(api.ledgerRead.value).toBe(true);
    wrapper.unmount();
  });

  it("台账失败 → summary 置 {}、ledgerRead=false，主表不受连带（分路记账）", async () => {
    h.listUpdates.mockRejectedValue(new Error("ledger down"));
    const { api, wrapper } = withUpdate();
    await settle();
    expect(api.updatesSummary.value).toEqual({});
    expect(api.ledgerRead.value).toBe(false);
    expect(api.read.value).toBe(true); // 主表仍读到
    expect(api.outdated.value.map(x => x.pod)).toEqual(["a"]);
    wrapper.unmount();
  });
});

describe("W13：间隔停档（与 monitor 对齐）", () => {
  it("interval=0 → 定时器清掉不重建；改回 5 → 重建", async () => {
    const { api, wrapper } = withUpdate();
    await settle();
    expect(vi.getTimerCount()).toBeGreaterThan(0); // 默认 10s 轮询在跑

    api.interval.value = 0;
    await settle();
    expect(vi.getTimerCount()).toBe(0); // setPoll(key,fn,0) 只清不建

    api.interval.value = 5;
    await settle();
    expect(vi.getTimerCount()).toBeGreaterThan(0);
    wrapper.unmount();
  });
});
