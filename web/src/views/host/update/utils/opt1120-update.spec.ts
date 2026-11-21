// @vitest-environment jsdom
/** QR1120 版本与更新页回归锁（W1/W2）：
 *  - W1：表格回填只在勾选的 pod 集合真的变化时做——row-key + reserve-selection
 *    本来就按 key 保勾，每轮 clearSelection + 逐台 toggle 是 500 台 × 每 10s
 *    的 selection-change 事件链；升级成功清空勾选时表格必须同步清（不能只清模型）。
 *  - W2：落后清单 keyed 合并，未变化行保留旧对象引用。
 *
 *  mock 骨架与既有 hook.spec.tsx 成例一致；表格实例用 spy 假件注入 tableRef，
 *  直接数 clearSelection/toggleRowSelection 的调用次数。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { defineComponent, nextTick } from "vue";
import { mount } from "@vue/test-utils";

import { clearPolls } from "@/utils/kkPoll";

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

// useRouter 随 QR1121 主机列下钻进入 hook，mock 同步补全（锁语义不变）
vi.mock("vue-router", () => ({
  useRoute: () => h.route,
  useRouter: () => ({ push: vi.fn() })
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

import { useHostUpdate } from "./hook";

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

const flush = async () => {
  await nextTick();
  await new Promise(r => setTimeout(r, 0));
  await nextTick();
};

/** 假表格实例：只关心回填动作的调用次数 */
function fakeTable() {
  const spies = {
    clearSelection: vi.fn(),
    toggleRowSelection: vi.fn()
  };
  return {
    spies,
    ref: { getTableRef: () => spies, setAdaptive: vi.fn() } as any
  };
}

beforeEach(() => {
  clearPolls();
  h.route.query = {};
  for (const fn of [
    h.message,
    h.listHosts,
    h.getAgentCurrent,
    h.listUpdates,
    h.upgradeHosts,
    h.confirm,
    h.alert
  ])
    fn.mockReset();
  h.listHosts.mockResolvedValue(hostsOf([host("a"), host("b")]));
  h.getAgentCurrent.mockResolvedValue({
    version: "1.1.0",
    hosts_total: 2,
    hosts_outdated: 2,
    sha256: "s",
    size: 1,
    uploaded_at: 0
  });
  h.listUpdates.mockResolvedValue({ items: [], summary: {}, limit: 50 });
  h.confirm.mockResolvedValue(undefined);
  h.upgradeHosts.mockResolvedValue({
    ok: true,
    batch_id: "ug-1-1",
    accepted: [],
    skipped: []
  });
  h.alert.mockResolvedValue(undefined);
});

afterEach(() => {
  clearPolls();
});

describe("QR1120-W1：勾选回填只在 pod 集合变化时做", () => {
  it("静默轮询 pod 集合未变 → 不再 clearSelection + 逐台 toggle", async () => {
    const { api, wrapper } = withUpdate();
    await flush();
    const t = fakeTable();
    api.tableRef.value = t.ref;

    api.selectAllOutdated(); // 显式全选：回填一次（clear + 2 台 toggle）
    expect(t.spies.clearSelection).toHaveBeenCalledTimes(1);
    expect(t.spies.toggleRowSelection).toHaveBeenCalledTimes(2);
    t.spies.clearSelection.mockClear();
    t.spies.toggleRowSelection.mockClear();

    // 轮询到达：主机集合没变（只有时钟读数走了）
    h.listHosts.mockResolvedValue(
      hostsOf([host("a", { age_sec: 22 }), host("b", { age_sec: 22 })])
    );
    await api.load(true);

    expect(api.selection.value.map(x => x.pod)).toEqual(["a", "b"]);
    expect(t.spies.clearSelection).not.toHaveBeenCalled();
    expect(t.spies.toggleRowSelection).not.toHaveBeenCalled();
    wrapper.unmount();
  });

  it("勾选的主机离开落后清单（pod 集合变化）→ 回填对齐", async () => {
    const { api, wrapper } = withUpdate();
    await flush();
    const t = fakeTable();
    api.tableRef.value = t.ref;
    api.selectAllOutdated();
    t.spies.clearSelection.mockClear();
    t.spies.toggleRowSelection.mockClear();

    h.listHosts.mockResolvedValue(hostsOf([host("a")])); // b 升完了
    await api.load(true);

    expect(api.selection.value.map(x => x.pod)).toEqual(["a"]);
    expect(t.spies.clearSelection).toHaveBeenCalledTimes(1);
    expect(t.spies.toggleRowSelection).toHaveBeenCalledTimes(1);
    expect(t.spies.toggleRowSelection.mock.calls[0][0].pod).toBe("a");
    wrapper.unmount();
  });

  it("升级成功清空勾选时表格同步清（模型清空不再依赖 load 的无条件回填）", async () => {
    const { api, wrapper } = withUpdate();
    await flush();
    const t = fakeTable();
    api.tableRef.value = t.ref;
    api.selection.value = [host("a") as any];

    await api.onUpgrade();

    expect(api.selection.value).toEqual([]);
    expect(t.spies.clearSelection).toHaveBeenCalled();
    wrapper.unmount();
  });
});

describe("QR1120-W2：落后清单 keyed 合并", () => {
  it("实质未变的行保留旧对象引用，age_sec 就地补新", async () => {
    const { api, wrapper } = withUpdate();
    await flush();
    const first = api.outdated.value[0];

    h.listHosts.mockResolvedValue(
      hostsOf([host("a", { age_sec: 22 }), host("b", { age_sec: 22 })])
    );
    await api.load(true);

    expect(api.outdated.value[0]).toBe(first);
    expect(api.outdated.value[0].age_sec).toBe(22);
    wrapper.unmount();
  });
});
