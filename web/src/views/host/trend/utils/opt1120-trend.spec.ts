// @vitest-environment jsdom
/** QR1120 趋势页回归锁（W3/W4/W12/W16）：
 *  - W12：切时间窗不回填未保存的上限编辑（静默路径的既有锁在 hook.spec.ts）；
 *  - W4：pod 变化立即清掉旧主机曲线与输入框并进入加载态；空态三态分开说；
 *  - W3：useTrendChart 走 merge（notMerge=false），不再每 30s 全量重建；
 *  - W16：tooltip valueFormatter 统一一位小数 + %，null 点显 -。
 *
 *  与 hook.spec.ts 同款隔离：mock 掉 import 链上的真实模块（api/* 引真实 http
 *  会连带拉起 @/router 的模块级初始化）。route params 用 reactive 包一层，
 *  pod computed 才能感知换主机；组件经 mount/unmount 驱动，watch 随作用域销毁，
 *  用例之间不残留监听器。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { defineComponent, nextTick } from "vue";
import { mount } from "@vue/test-utils";

const routeState = vi.hoisted(() => ({
  params: { pod: "h1" } as Record<string, string>,
  /** reactive 代理由 mock 工厂回填：改 pod 必须走代理才能触发依赖 */
  proxy: null as unknown as Record<string, string>
}));
const chartStub = vi.hoisted(() => ({
  setOption: vi.fn(),
  resize: vi.fn(),
  dispose: vi.fn()
}));

vi.mock("vue-router", async () => {
  const { reactive } = await import("vue");
  routeState.proxy = reactive(routeState.params);
  return { useRoute: () => ({ params: routeState.proxy }) };
});
vi.mock("@vueuse/core", () => ({ useResizeObserver: () => undefined }));
vi.mock("echarts/core", () => ({ use: vi.fn(), init: vi.fn(() => chartStub) }));
vi.mock("echarts/charts", () => ({ LineChart: {} }));
vi.mock("echarts/components", () => ({
  GridComponent: {},
  MarkLineComponent: {},
  TooltipComponent: {}
}));
vi.mock("echarts/renderers", () => ({ CanvasRenderer: {} }));
vi.mock("@/utils/http", () => ({ http: { request: vi.fn() } }));
vi.mock("@/utils/message", () => ({ message: vi.fn() }));
vi.mock("@/api/containers", () => ({
  getHostMetrics: vi.fn(),
  setHostLimits: vi.fn()
}));

import { getHostMetrics } from "@/api/containers";
import type { MetricPoint, MetricsResult } from "@/api/containers";
import { buildTrendOption, useHostTrend, useTrendChart } from "./hook";

const pt = (over: Partial<MetricPoint> = {}): MetricPoint => ({
  ts: 1700000000,
  cpu: 10,
  mem_mb: 500,
  mem_pct: null,
  disk_pct: null,
  ...over
});

const metricsData = (over: Partial<MetricsResult> = {}): MetricsResult => ({
  pod: "h1",
  hours: 24,
  source: "raw",
  series: [],
  mem_limit: null,
  disk_limit: 85,
  disk_limit_raw: null,
  ...over
});

function withTrend() {
  let api!: ReturnType<typeof useHostTrend>;
  const Comp = defineComponent({
    setup() {
      api = useHostTrend();
      return () => null;
    }
  });
  const wrapper = mount(Comp);
  return { api, wrapper };
}

/** onMounted(load) 与 watch 回调都在微任务里落定 */
const flush = async () => {
  await nextTick();
  await new Promise(r => setTimeout(r, 0));
  await nextTick();
};

beforeEach(() => {
  routeState.params.pod = "h1";
  vi.mocked(getHostMetrics).mockReset();
  vi.mocked(getHostMetrics).mockResolvedValue(metricsData());
  chartStub.setOption.mockClear();
  chartStub.resize.mockClear();
  chartStub.dispose.mockClear();
});

describe("QR1120-W12：切时间窗不冲掉未保存的上限编辑", () => {
  it("hours 变化走非静默加载但不回填输入框；请求带上新窗口", async () => {
    vi.mocked(getHostMetrics).mockResolvedValue(
      metricsData({ mem_limit: 85, disk_limit_raw: 90 })
    );
    const { api, wrapper } = withTrend();
    await flush();
    expect(api.memInput.value).toBe(85);
    expect(api.diskInput.value).toBe(90);

    api.memInput.value = 70; // 运维编辑中，尚未保存
    api.hours.value = 6;
    await flush();

    expect(getHostMetrics).toHaveBeenLastCalledWith("h1", 6);
    expect(api.memInput.value).toBe(70); // 编辑不被打回原值
    expect(api.diskInput.value).toBe(90);
    wrapper.unmount();
  });

  it("保存成功后的重载（load()）仍回填——既有同步时机不变", async () => {
    vi.mocked(getHostMetrics).mockResolvedValue(
      metricsData({ mem_limit: 85, disk_limit_raw: 90 })
    );
    const { api, wrapper } = withTrend();
    await flush();
    api.memInput.value = 70;
    await api.load();
    expect(api.memInput.value).toBe(85);
    wrapper.unmount();
  });
});

describe("QR1120-W4：换主机与初始加载的状态", () => {
  it("pod 变化立即清掉旧曲线/输入框并进入加载态", async () => {
    vi.mocked(getHostMetrics).mockResolvedValue(
      metricsData({ series: [pt({ mem_pct: 40 })], mem_limit: 85 })
    );
    const { api, wrapper } = withTrend();
    await flush();
    expect(api.metrics.value).not.toBeNull();
    expect(api.memInput.value).toBe(85);

    // 第二路请求挂起，才能观察「旧数据已退场、新数据未到达」的中间态
    let resolveSecond!: (v: MetricsResult) => void;
    vi.mocked(getHostMetrics).mockImplementationOnce(
      () =>
        new Promise<MetricsResult>(r => {
          resolveSecond = r;
        })
    );
    routeState.proxy.pod = "h2";
    await flush(); // watch(pod) 落定、新 load 在途

    expect(api.metrics.value).toBeNull(); // 旧主机曲线立即退场
    expect(api.memInput.value).toBeNull();
    expect(api.diskInput.value).toBeNull();
    expect(api.loading.value).toBe(true);
    expect(api.emptyText.value).toBe("加载中…");

    resolveSecond(metricsData({ pod: "h2" }));
    await flush();
    expect(getHostMetrics).toHaveBeenLastCalledWith("h2", 24);
    expect(api.loading.value).toBe(false);
    wrapper.unmount();
  });

  it("空态三态：加载中 / 没读到 / 读到了但没点", async () => {
    vi.mocked(getHostMetrics).mockRejectedValueOnce(new Error("boom"));
    const { api, wrapper } = withTrend();
    expect(api.emptyText.value).toBe("加载中…"); // 初始加载不谎报「不可达」
    await flush();
    expect(api.emptyText.value).toBe("没读到（指标接口不可达）");

    vi.mocked(getHostMetrics).mockResolvedValueOnce(metricsData());
    await api.load();
    expect(api.emptyText.value).toContain("没有序列数据");
    wrapper.unmount();
  });
});

describe("QR1120-W3：useTrendChart 走 merge", () => {
  it("setOption 第二参为 false（不再 notMerge 全量重建），卸载 dispose", () => {
    const opt = { series: [{ type: "line", data: [1] }] };
    let api!: ReturnType<typeof useTrendChart>;
    const Comp = defineComponent({
      setup() {
        api = useTrendChart(() => opt);
        return () => null;
      }
    });
    const wrapper = mount(Comp);
    api.el.value = document.createElement("div");
    api.render();
    expect(chartStub.setOption).toHaveBeenCalledWith(opt, false);
    wrapper.unmount();
    expect(chartStub.dispose).toHaveBeenCalled();
  });
});

describe("QR1120-W16：tooltip 百分比口径", () => {
  it("valueFormatter 一位小数 + %；null/坏值显 - 不画 0", () => {
    const opt = buildTrendOption([pt({ mem_pct: 33.45 })], {
      key: "mem_pct",
      lineName: "内存使用率",
      limit: null,
      fmt: ts => `t${ts}`
    });
    const vf = (opt.tooltip as { valueFormatter: (v: unknown) => string })
      .valueFormatter;
    expect(vf(33.45)).toBe("33.5%");
    expect(vf(0)).toBe("0.0%");
    expect(vf(null)).toBe("-");
    expect(vf(Number.NaN)).toBe("-");
  });
});