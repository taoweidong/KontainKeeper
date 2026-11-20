// @vitest-environment jsdom
/** QR1120 主机总览回归锁（W2/W7/W14）：
 *  - W2：轮询 keyed 合并——未变化行保留旧对象引用，age_sec 就地补新；
 *  - W7：整行点击进详情，但拖选文本（复制 IP/主机名）松手不跳转；
 *  - W14：CPU/磁盘计量条数字统一带 %（没读到仍是 -，不渲染 "-%"）。
 *
 *  隔离手法与 update 页 spec 成例一致：mock 掉 import 链上的真实模块，
 *  经 mount/unmount 驱动 hook（watch/轮询随作用域销毁）。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { defineComponent, nextTick } from "vue";
import { mount } from "@vue/test-utils";

import { clearPolls } from "@/utils/kkPoll";
import type { HostSummary } from "@/api/containers";

const h = vi.hoisted(() => ({
  push: vi.fn(),
  message: vi.fn(),
  listHosts: vi.fn()
}));

vi.mock("vue-router", () => ({ useRouter: () => ({ push: h.push }) }));
vi.mock("@/utils/http", () => ({ http: { request: vi.fn() } }));
vi.mock("@/utils/message", () => ({ message: h.message }));
vi.mock("@/api/containers", () => ({
  listHosts: (...a: any[]) => h.listHosts(...a)
}));
vi.mock("@/api/commands", () => ({
  createCommand: vi.fn(),
  listCollectItems: vi.fn(),
  DEFAULT_COLLECT_ITEMS: ["cpu", "mem"]
}));
vi.mock("@/api/exporting", () => ({ exportHosts: vi.fn() }));

import { useMonitor } from "./hook";

const host = (pod: string, over: Partial<HostSummary> = {}): HostSummary =>
  ({
    pod,
    image: "",
    agent_ver: "0.1.0",
    latest_agent_ver: "",
    agent_outdated: false,
    online: true,
    age_sec: 5,
    cpu: 10,
    mem_mb: 512,
    disk_pct: 40,
    disk_alert: false,
    status_reason: "",
    host_type: "container",
    os_name: "",
    ip: "10.0.0.1",
    group_name: "",
    proto_ver: 4,
    docker_total: 0,
    docker_running: 0,
    docker_unhealthy: 0,
    caps: { shell: true },
    ...over
  }) as HostSummary;

const hostsOf = (items: HostSummary[]) => ({
  items,
  total: items.length,
  online: items.filter(x => x.online).length,
  alerts: 0,
  outdated: 0,
  latest_agent_ver: ""
});

function withMonitor() {
  let api!: ReturnType<typeof useMonitor>;
  const Comp = defineComponent({
    setup() {
      api = useMonitor();
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

beforeEach(() => {
  h.push.mockReset();
  h.message.mockReset();
  h.listHosts.mockReset();
  h.listHosts.mockResolvedValue(hostsOf([host("a")]));
});

afterEach(() => {
  clearPolls();
  vi.restoreAllMocks();
});

describe("QR1120-W2：轮询 keyed 合并", () => {
  it("实质未变的行保留旧对象引用，age_sec 就地补新值", async () => {
    const { api, wrapper } = withMonitor();
    await flush();
    const first = api.paged.value[0];
    expect(first.age_sec).toBe(5);

    // 第二轮轮询：主机数据没变，只有时钟读数走了 10s
    h.listHosts.mockResolvedValue(hostsOf([host("a", { age_sec: 15 })]));
    await api.load(true);

    expect(api.paged.value[0]).toBe(first); // 引用不变 → 富单元格不重渲染
    expect(api.paged.value[0].age_sec).toBe(15); // 心跳读数仍新鲜
    wrapper.unmount();
  });

  it("实质字段变化的行换新对象", async () => {
    const { api, wrapper } = withMonitor();
    await flush();
    const first = api.paged.value[0];

    h.listHosts.mockResolvedValue(hostsOf([host("a", { cpu: 90 })]));
    await api.load(true);

    expect(api.paged.value[0]).not.toBe(first);
    expect(api.paged.value[0].cpu).toBe(90);
    wrapper.unmount();
  });
});

describe("QR1120-W7：整行点击与文本选择", () => {
  it("selection 列点击不跳转；无选区时整行点击进详情", async () => {
    const { api, wrapper } = withMonitor();
    await flush();
    const row = api.paged.value[0];

    api.onRowClick(row, { type: "selection" });
    expect(h.push).not.toHaveBeenCalled();

    api.onRowClick(row, { prop: "pod" });
    expect(h.push).toHaveBeenCalledWith({
      name: "HostDetail",
      params: { pod: "a" }
    });
    wrapper.unmount();
  });

  it("存在非空选区（拖选复制 IP/主机名）时不跳转", async () => {
    const { api, wrapper } = withMonitor();
    await flush();
    const row = api.paged.value[0];

    vi.spyOn(window, "getSelection").mockReturnValue({
      isCollapsed: false,
      toString: () => "10.0.0.1"
    } as any);
    api.onRowClick(row, { prop: "ip" });
    expect(h.push).not.toHaveBeenCalled();

    // 选区折叠（单击未拖选）恢复正常跳转
    vi.spyOn(window, "getSelection").mockReturnValue({
      isCollapsed: true,
      toString: () => ""
    } as any);
    api.onRowClick(row, { prop: "ip" });
    expect(h.push).toHaveBeenCalledTimes(1);
    wrapper.unmount();
  });
});

describe("QR1120-W14：计量条数字带单位", () => {
  it("CPU/磁盘列渲染 pctText：有限值带 %，没读到显 -", async () => {
    const { api, wrapper } = withMonitor();
    await flush();
    const cpuCol = api.columns.find((c: any) => c.label === "CPU") as any;
    const diskCol = api.columns.find((c: any) => c.label === "磁盘") as any;
    // JSX 文本子节点可能被编译成数组，统一拼回字符串
    const numText = (v: any) => {
      const t = v.children[1].children;
      return Array.isArray(t) ? t.join("") : t;
    };

    expect(numText(cpuCol.cellRenderer({ row: host("a", { cpu: 33.45 }) }))).toBe(
      "33.5%"
    );
    expect(numText(cpuCol.cellRenderer({ row: host("a", { cpu: null }) }))).toBe(
      "-"
    );
    expect(
      numText(diskCol.cellRenderer({ row: host("a", { disk_pct: 87 }) }))
    ).toBe("87%");
    wrapper.unmount();
  });
});