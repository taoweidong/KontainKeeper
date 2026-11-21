import { getCurrentScope, nextTick, onScopeDispose } from "vue";

/** ECharts 配色适配（QR1121-W6/W18）：echarts 不解析 CSS 变量，暗色模式下
 *  库默认深灰轴文字（#6E7079/#333）在深底上对比度不足。这里在组装 option 时
 *  运行时读 EP 变量算出字面量注入，主题/暗色切换后重读重绘。
 *
 *  detail 与 trend 两页共用；纯函数 chartColorsFromVars 可 node 单测，
 *  运行时壳 readChartColors/useChartTheme 只在浏览器生效。
 */

export type ChartColors = {
  /** 轴刻度 / 图例文字 */
  text: string;
  /** 轴线 */
  axisLine: string;
  /** 网格线 */
  splitLine: string;
  /** tooltip 底 */
  tooltipBg: string;
  /** tooltip 边框 */
  tooltipBorder: string;
  /** 告警上限 markLine（--kk-alert） */
  alert: string;
  /** 主序列线色（QR1123-W1）：EP 主色，8 套主题预设跟随。
   *  series.color 一键管线色/areaStyle/legend 与 tooltip marker。 */
  line: string;
  /** 次序列线色（detail 双轴第二条）：EP 成功色。不用 primary-light-3——
   *  暗色下 light-N 实为 darken（useDataThemeChange），深底对比度更差。 */
  lineAlt: string;
};

/** 亮色字面量兜底：与 element-plus theme-chalk 默认值逐字一致
 *  （alert = EP danger 默认 #f56c6c，line/lineAlt = EP primary/success 默认）。
 *  node 环境 / 变量读不到时用，保证 buildTrendOption 不传 colors 的既有
 *  纯函数锁输出不变。 */
export const DEFAULT_CHART_COLORS: ChartColors = {
  text: "#606266",
  axisLine: "#dcdfe6",
  splitLine: "#ebeef5",
  tooltipBg: "#ffffff",
  tooltipBorder: "#e4e7ed",
  alert: "#f56c6c",
  line: "#409eff",
  lineAlt: "#67c23a"
};

/** CSS 变量名 → ChartColors 键 的映射（唯一真相，改口径只改这里） */
const VAR_KEYS: Record<keyof ChartColors, string> = {
  text: "--el-text-color-regular",
  axisLine: "--el-border-color",
  splitLine: "--el-border-color-lighter",
  tooltipBg: "--el-bg-color-overlay",
  tooltipBorder: "--el-border-color-light",
  alert: "--kk-alert",
  line: "--el-color-primary",
  lineAlt: "--el-color-success"
};

/** 纯函数：从 CSS 变量值表组装配色（QR1121-W6）。
 *  getPropertyValue 的返回值带前导空格，必须 trim；空值逐键回落 DEFAULT，
 *  一个变量缺失不拖垮整张图。 */
export function chartColorsFromVars(vars: Record<string, string>): ChartColors {
  const out = { ...DEFAULT_CHART_COLORS };
  for (const key of Object.keys(VAR_KEYS) as (keyof ChartColors)[]) {
    const v = (vars[VAR_KEYS[key]] || "").trim();
    if (v) out[key] = v;
  }
  return out;
}

/** 运行时壳：读 documentElement 的计算样式。node（单测默认环境）无
 *  document 即回 DEFAULT，调用方不必自己判环境。 */
export function readChartColors(): ChartColors {
  if (typeof document === "undefined") return { ...DEFAULT_CHART_COLORS };
  const cs = getComputedStyle(document.documentElement);
  const vars: Record<string, string> = {};
  for (const name of Object.values(VAR_KEYS)) {
    vars[name] = cs.getPropertyValue(name);
  }
  return chartColorsFromVars(vars);
}

/** 主题切换重绘信号（QR1121-W18）：MutationObserver 观察 documentElement 的
 *  class + style——暗色切换改 class、setEpThemeColor 改内联 --el-color-primary
 *  都命中。**不用 epTheme store watch**：default 主题下切暗色 store 两字段
 *  可能都不变（useDataThemeChange.ts），watch 会漏帧。
 *  回调经 nextTick 合帧（一次切换可能连写 class 与 style 两条 mutation），
 *  作用域销毁时断开。node / 无 MutationObserver 环境即 no-op。 */
export function useChartTheme(onChange: () => void): void {
  if (
    typeof document === "undefined" ||
    typeof MutationObserver === "undefined"
  ) {
    return;
  }
  let pending = false;
  const mo = new MutationObserver(() => {
    if (pending) return;
    pending = true;
    nextTick(() => {
      pending = false;
      onChange();
    });
  });
  mo.observe(document.documentElement, {
    attributes: true,
    attributeFilter: ["class", "style"]
  });
  if (getCurrentScope()) onScopeDispose(() => mo.disconnect());
}
