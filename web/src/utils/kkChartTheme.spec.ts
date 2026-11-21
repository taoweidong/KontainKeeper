// @vitest-environment jsdom
/** useChartTheme 的 DOM 锁（QR1121-W18）：主题切换重绘信号。
 *
 *  裁决依据：不用 epTheme store watch——default 主题下切暗色 store 两字段
 *  可能都不变（useDataThemeChange.ts），watch 会漏帧。MutationObserver 观察
 *  documentElement 的 class（暗色切换）+ style（setEpThemeColor 内联改写
 *  --el-color-primary）是唯一全覆盖信号。这里锁：两种 mutation 都触发、
 *  同帧合帧只调一次、作用域销毁后断开。
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { effectScope, nextTick } from "vue";

import { readChartColors, useChartTheme } from "./kkChart";

/** MO 回调与 nextTick 都是微任务：两次 await 确保「MO 回调 → nextTick 合帧」
 *  整条链跑完（第一次 await 时 MO 回调刚把 onChange 排进 nextTick）。 */
async function flush() {
  await nextTick();
  await nextTick();
}

afterEach(() => {
  document.documentElement.className = "";
  document.documentElement.removeAttribute("style");
});

describe("useChartTheme：class/style 双信号（QR1121-W18）", () => {
  it("暗色类切换触发 onChange", async () => {
    const fn = vi.fn();
    const scope = effectScope();
    scope.run(() => useChartTheme(fn));
    document.documentElement.classList.add("dark");
    await flush();
    expect(fn).toHaveBeenCalledTimes(1);
    scope.stop();
  });

  it("内联 style 改写（主题色切换）也触发", async () => {
    const fn = vi.fn();
    const scope = effectScope();
    scope.run(() => useChartTheme(fn));
    document.documentElement.style.setProperty("--el-color-primary", "#ff0000");
    await flush();
    expect(fn).toHaveBeenCalledTimes(1);
    scope.stop();
  });

  it("同帧 class + style 两条 mutation 合帧只调一次", async () => {
    const fn = vi.fn();
    const scope = effectScope();
    scope.run(() => useChartTheme(fn));
    document.documentElement.classList.add("dark");
    document.documentElement.style.setProperty("--el-color-primary", "#ff0000");
    await flush();
    expect(fn).toHaveBeenCalledTimes(1);
    scope.stop();
  });

  it("作用域销毁后断开 observer（不再触发）", async () => {
    const fn = vi.fn();
    const scope = effectScope();
    scope.run(() => useChartTheme(fn));
    scope.stop();
    document.documentElement.classList.add("dark");
    await flush();
    expect(fn).not.toHaveBeenCalled();
  });
});

describe("readChartColors：jsdom 读内联变量", () => {
  it("documentElement 内联的 --kk-alert 被读到（markLine 跟随主题）", () => {
    document.documentElement.style.setProperty("--kk-alert", "#f89898");
    expect(readChartColors().alert).toBe("#f89898");
  });
});
