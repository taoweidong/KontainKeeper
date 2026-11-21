/** QR1121-W12 的回归锁：buildTrendOption 的 colors 注入与回落兼容。
 *
 *  暗色适配（QR1121）：echarts 不解析 CSS 变量，配色由调用方运行时读
 *  --kk-alert 等注入。锁两条：① 传 colors 时 markLine/轴文字/tooltip 全走
 *  注入值（两处硬编码 #f56c6c 消灭）；② 不传 colors 回落 EP danger 默认
 *  字面量——既有 hook.spec / opt1120-trend.spec 不传 colors，输出逐字不变。
 */
import { describe, expect, it, vi } from "vitest";

// 与 hook.spec.ts 同一套 mock：./hook 模块级 import echarts，纯函数用例也要挡掉
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

describe("W12：colors 注入", () => {
  it("传 colors：markLine 线与标签、轴文字、tooltip 全走注入值", () => {
    const dark = {
      ...DEFAULT_CHART_COLORS,
      text: "#cfd3dc",
      axisLine: "#4c4d4f",
      splitLine: "#414243",
      tooltipBg: "#1d1e1f",
      tooltipBorder: "#414243",
      alert: "#f89898"
    };
    const opt = buildTrendOption([pt()], { ...base, colors: dark }) as any;
    const ml = opt.series[0].markLine;
    expect(ml.lineStyle.color).toBe("#f89898");
    expect(ml.label.color).toBe("#f89898");
    expect(opt.xAxis.axisLabel.color).toBe("#cfd3dc");
    expect(opt.xAxis.axisLine.lineStyle.color).toBe("#4c4d4f");
    expect(opt.yAxis.splitLine.lineStyle.color).toBe("#414243");
    expect(opt.tooltip.backgroundColor).toBe("#1d1e1f");
    expect(opt.tooltip.textStyle.color).toBe("#cfd3dc");
  });

  it("不传 colors：回落 EP danger 默认 #f56c6c（既有纯函数锁的兼容前提）", () => {
    const opt = buildTrendOption([pt()], base) as any;
    const ml = opt.series[0].markLine;
    expect(ml.lineStyle.color).toBe("#f56c6c");
    expect(ml.label.color).toBe("#f56c6c");
    expect(opt.xAxis.axisLabel.color).toBe(DEFAULT_CHART_COLORS.text);
  });

  it("colors 注入不碰 markLine.data 语义（limit=null 仍显式空 data，QR1120-W3）", () => {
    const withLine = buildTrendOption([pt()], {
      ...base,
      colors: DEFAULT_CHART_COLORS
    }) as any;
    const noLine = buildTrendOption([pt()], {
      ...base,
      limit: null,
      colors: DEFAULT_CHART_COLORS
    }) as any;
    expect(withLine.series[0].markLine.data).toEqual([{ yAxis: 85 }]);
    expect(noLine.series[0].markLine.data).toEqual([]);
  });
});
