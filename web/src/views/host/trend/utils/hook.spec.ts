/** 单机趋势页纯函数回归锁：上限校验、越限判定、图表 option 组装。
 *
 *  语义锚点：
 *  - limitError 的「null = 清除自定义」必须合法——那是回落全局口径的入口；
 *  - isBreach 对「没读到」恒 false（QR-W16：没读到不是告警）；
 *  - buildTrendOption 的上限线只在 limit 非 null 时出现（内存未设置不画线），
 *    序列 null 值原样透传（ECharts 画断线而不是 0）。
 */
import { describe, expect, it, vi } from "vitest";

// 纯函数 + option 组装，不实例化 hook；mock 掉 import 链上的真实模块
// （api/* 引真实 http 会连带拉起 @/router 的模块级初始化，update 页 spec 成例）
// routeState 供 hook 用例注入 pod（vi.mock 工厂被提升，须经 vi.hoisted 共享）
const routeState = vi.hoisted(() => ({ params: {} as Record<string, string> }));
vi.mock("vue-router", () => ({ useRoute: () => ({ params: routeState.params }) }));
vi.mock("@vueuse/core", () => ({
  useResizeObserver: () => undefined
}));
vi.mock("echarts/core", () => ({ use: vi.fn(), init: vi.fn() }));
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
import {
  HOUR_OPTIONS,
  buildTrendOption,
  isBreach,
  limitError,
  useHostTrend
} from "./hook";
import type { MetricPoint, MetricsResult } from "@/api/containers";

const pt = (over: Partial<MetricPoint> = {}): MetricPoint => ({
  ts: 1700000000,
  cpu: 10,
  mem_mb: 500,
  mem_pct: null,
  disk_pct: null,
  ...over
});

describe("limitError：上限取值校验（null = 清除自定义，必须合法）", () => {
  it("null / undefined = 清除，合法", () => {
    expect(limitError(null)).toBeNull();
    expect(limitError(undefined)).toBeNull();
  });

  it("1–100 的整数合法", () => {
    expect(limitError(1)).toBeNull();
    expect(limitError(85)).toBeNull();
    expect(limitError(100)).toBeNull();
  });

  it("越界与非整数给可读文案", () => {
    expect(limitError(0)).toContain("1–100");
    expect(limitError(101)).toContain("1–100");
    expect(limitError(62.5)).toContain("1–100");
  });
});

describe("isBreach：越限判定（任一侧没数据都不算）", () => {
  it("达到或超过上限即越限", () => {
    expect(isBreach(90, 85)).toBe(true);
    expect(isBreach(85, 85)).toBe(true);
    expect(isBreach(84, 85)).toBe(false);
  });

  it("没读到 / 未设置上限恒 false：没读到不是告警（QR-W16）", () => {
    expect(isBreach(null, 85)).toBe(false);
    expect(isBreach(undefined, 85)).toBe(false);
    expect(isBreach(90, null)).toBe(false);
    expect(isBreach(null, null)).toBe(false);
  });
});

describe("buildTrendOption：图表 option 组装", () => {
  const fmt = (ts: number) => `t${ts}`;

  it("数据按 key 取值、x 轴走注入的格式化器", () => {
    const opt = buildTrendOption([pt({ mem_pct: 40 }), pt({ mem_pct: 62 })], {
      key: "mem_pct",
      lineName: "内存使用率",
      limit: null,
      fmt
    });
    const s = opt.series[0] as { data: number[] };
    expect(s.data).toEqual([40, 62]);
    expect((opt.xAxis as { data: string[] }).data).toEqual([
      "t1700000000",
      "t1700000000"
    ]);
  });

  it("上限线只在 limit 非 null 时出现：内存未设置不画线", () => {
    const withLine = buildTrendOption([pt({ disk_pct: 72 })], {
      key: "disk_pct",
      lineName: "磁盘",
      limit: 85,
      fmt
    });
    const ml = (withLine.series[0] as { markLine?: { data: unknown[] } })
      .markLine;
    expect(ml?.data).toEqual([{ yAxis: 85 }]);

    const noLine = buildTrendOption([pt({ mem_pct: 40 })], {
      key: "mem_pct",
      lineName: "内存",
      limit: null,
      fmt
    });
    expect(
      (noLine.series[0] as { markLine?: unknown }).markLine
    ).toBeUndefined();
  });

  it("序列里的 null 原样透传（ECharts 断线，而不是当 0 画）", () => {
    const opt = buildTrendOption(
      [pt({ disk_pct: 30 }), pt({ disk_pct: null }), pt({ disk_pct: 72 })],
      { key: "disk_pct", lineName: "磁盘", limit: 85, fmt }
    );
    expect((opt.series[0] as { data: (number | null)[] }).data).toEqual([
      30,
      null,
      72
    ]);
  });

  it("时间窗档位：6h/24h 原始帧、7d 切小时聚合（后端 metrics_series 语义的前端镜像）", () => {
    expect(HOUR_OPTIONS.map(o => o.value)).toEqual([6, 24, 168]);
  });
});

// ---- QR-W17：hook 行为锁（输入框不被静默轮询冲掉、失败恒变冷）----

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

describe("useHostTrend：QR-W17 行为锁", () => {
  it("静默轮询不回填上限输入框，手动加载才同步", async () => {
    routeState.params.pod = "h1";
    vi.mocked(getHostMetrics).mockResolvedValue(
      metricsData({ mem_limit: 85, disk_limit_raw: 90 })
    );
    const h = useHostTrend();
    await h.load();
    expect(h.memInput.value).toBe(85);
    expect(h.diskInput.value).toBe(90);

    h.memInput.value = 70; // 运维编辑中
    vi.mocked(getHostMetrics).mockResolvedValue(
      metricsData({ mem_limit: 85, disk_limit_raw: 90 })
    );
    await h.load(true); // 30s 静默轮询到达
    expect(h.memInput.value).toBe(70); // 不被打回原值
    expect(h.pollFailed.value).toBe(false);

    await h.load(); // 手动重载（保存成功后的同步时机）仍会回填
    expect(h.memInput.value).toBe(85);
  });

  it("静默与手动失败都把页头置冷（pollFailed 恒 true）", async () => {
    routeState.params.pod = "h2";
    vi.mocked(getHostMetrics).mockRejectedValueOnce(new Error("boom"));
    const h = useHostTrend();
    await h.load(true);
    expect(h.pollFailed.value).toBe(true);
    vi.mocked(getHostMetrics).mockRejectedValueOnce(new Error("boom"));
    await h.load();
    expect(h.pollFailed.value).toBe(true);
  });
});
