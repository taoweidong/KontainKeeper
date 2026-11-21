/** QR1123-W1 回归锁：趋势图线色走 EP 主色（8 套主题预设跟随）。
 *
 *  series.color 一键管线色/areaStyle/legend 与 tooltip marker；
 *  注入生效、不传回落 EP primary 默认 #409eff（既有逐键锁零破坏）。 */
import { describe, expect, it, vi } from "vitest";

// 与 hook.spec.ts 同一套 mock：./hook 模块级 import echarts
vi.mock("vue-router", () => ({ useRoute: () => ({ params: {} }) }));
vi.mock("@vueuse/core", () => ({ useResizeObserver: () => undefined }));
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

import { buildTrendOption } from "./hook";
import { DEFAULT_CHART_COLORS } from "@/utils/kkChart";
import type { MetricPoint } from "@/api/containers";

const pt = (over: Partial<MetricPoint> = {}): MetricPoint => ({
  ts: 1700000000,
  cpu: 10,
  mem_mb: 500,
  mem_pct: 40,
  disk_pct: 60,
  ...over
});
const fmt = (ts: number) => `t${ts}`;
const base = {
  key: "mem_pct" as const,
  lineName: "内存使用率",
  limit: 85,
  fmt
};

describe("W1：series 线色 EP 派生", () => {
  it("注入 colors.line 生效（主题预设跟随）", () => {
    const themed = { ...DEFAULT_CHART_COLORS, line: "#8b5cf6" };
    const opt = buildTrendOption([pt()], { ...base, colors: themed }) as any;
    expect(opt.series[0].color).toBe("#8b5cf6");
  });

  it("不传 colors 回落 EP primary 默认 #409eff", () => {
    const opt = buildTrendOption([pt()], base) as any;
    expect(opt.series[0].color).toBe("#409eff");
    expect(opt.series[0].color).toBe(DEFAULT_CHART_COLORS.line);
  });

  it("线色不碰 markLine 告警色语义（两者独立）", () => {
    const themed = {
      ...DEFAULT_CHART_COLORS,
      line: "#8b5cf6",
      alert: "#f89898"
    };
    const opt = buildTrendOption([pt()], { ...base, colors: themed }) as any;
    expect(opt.series[0].color).toBe("#8b5cf6");
    expect(opt.series[0].markLine.lineStyle.color).toBe("#f89898");
  });
});
