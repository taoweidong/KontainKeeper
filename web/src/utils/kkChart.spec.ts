/** kkChart 纯函数锁（QR1121-W6）：CSS 变量值表 → ECharts 字面量配色。
 *
 *  echarts 不解析 CSS 变量，暗色适配靠运行时读变量注入字面量；
 *  这里锁「trim + 逐键回落 DEFAULT」——一个变量缺失不拖垮整张图，
 *  且 node 环境（无 document）readChartColors 必须回兜底而不是抛错。
 *  主题切换重绘信号（useChartTheme）的 DOM 锁在 kkChartTheme.spec.ts（jsdom）。
 */
import { describe, expect, it } from "vitest";

import {
  chartColorsFromVars,
  DEFAULT_CHART_COLORS,
  readChartColors
} from "./kkChart";

describe("chartColorsFromVars：变量值表组装配色（QR1121-W6）", () => {
  it("空对象全回落 DEFAULT（亮色字面量与 EP 默认逐字一致）", () => {
    expect(chartColorsFromVars({})).toEqual(DEFAULT_CHART_COLORS);
    expect(DEFAULT_CHART_COLORS.alert).toBe("#f56c6c");
  });

  it("getPropertyValue 的前导空格必须 trim", () => {
    const c = chartColorsFromVars({
      "--el-text-color-regular": "  #cbd5e1  ",
      "--kk-alert": " #ff0000 "
    });
    expect(c.text).toBe("#cbd5e1");
    expect(c.alert).toBe("#ff0000");
    // 未提供的键仍回落
    expect(c.axisLine).toBe(DEFAULT_CHART_COLORS.axisLine);
  });

  it("空串/纯空白值逐键回落，不注入空颜色", () => {
    const c = chartColorsFromVars({
      "--el-text-color-regular": "",
      "--el-border-color": "   ",
      "--kk-alert": "#123456"
    });
    expect(c.text).toBe(DEFAULT_CHART_COLORS.text);
    expect(c.axisLine).toBe(DEFAULT_CHART_COLORS.axisLine);
    expect(c.alert).toBe("#123456");
  });

  it("六键全给时逐键生效（暗色场景）", () => {
    const dark = {
      "--el-text-color-regular": "#cfd3dc",
      "--el-border-color": "#4c4d4f",
      "--el-border-color-lighter": "#414243",
      "--el-bg-color-overlay": "#1d1e1f",
      "--el-border-color-light": "#414243",
      "--kk-alert": "#f89898"
    };
    const c = chartColorsFromVars(dark);
    expect(c.text).toBe("#cfd3dc");
    expect(c.tooltipBg).toBe("#1d1e1f");
    expect(c.alert).toBe("#f89898");
  });
});

describe("readChartColors：node 环境兜底", () => {
  it("无 document 回 DEFAULT 副本（调用方不必判环境）", () => {
    const c = readChartColors();
    expect(c).toEqual(DEFAULT_CHART_COLORS);
    // 返回副本：改返回值不污染兜底常量
    c.alert = "#000";
    expect(DEFAULT_CHART_COLORS.alert).toBe("#f56c6c");
  });
});
