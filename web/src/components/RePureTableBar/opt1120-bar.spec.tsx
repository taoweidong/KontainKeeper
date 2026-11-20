/** RePureTableBar 刷新 spin 状态机的单元测试（QR1120-W27）。
 *
 *  锁住的语义：
 *  - additive 可选 `loading` prop：不传（undefined）保持旧行为——点击后固定 500ms
 *    假转圈，未接线的页面（host 侧等）一个像素都不变；
 *  - 传入 `loading` 后 spin 完全跟随真实请求态：慢请求转到底、快请求立刻停，
 *    内部不再另起 500ms 定时器去「熄灭」一个由外部驱动的圈。
 *
 *  被测对象是从 bar.tsx 抽出的 useRefreshSpin（bar 本体依赖 ~icons / svg?component
 *  的 vite 插件解析，vitest 环境无法挂载整组件）：bar 里只剩一行接线
 *  `spin.value ? "animate-spin" : ""`，语义全部收在这个 composable 里。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { nextTick, ref } from "vue";
import { delay } from "@pureadmin/utils";

import { useRefreshSpin } from "./src/refreshSpin";

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
});

describe("未接线（loading 缺省 undefined）：旧行为一个像素不变", () => {
  it("点击刷新 → emit refresh + 转圈，固定 500ms 后熄灭（与真实请求无关）", async () => {
    const emitRefresh = vi.fn();
    const { spin, onReFresh } = useRefreshSpin(() => undefined, emitRefresh, delay);

    expect(spin.value).toBe(false);

    onReFresh();
    expect(emitRefresh).toHaveBeenCalledTimes(1);
    expect(spin.value).toBe(true);

    vi.advanceTimersByTime(499);
    await nextTick();
    expect(spin.value).toBe(true);

    vi.advanceTimersByTime(1);
    await nextTick();
    expect(spin.value).toBe(false);
  });

});

describe("接线后（QR1120-W27）：spin 跟随真实请求态", () => {
  it("请求在途一直转；请求结束立刻停——不再固定 500ms 熄灭", async () => {
    // 用 ref 模拟 props.loading 的响应性（bar 里读的是 reactive props）
    const loading = ref(false);
    const emitRefresh = vi.fn();
    const { spin, onReFresh } = useRefreshSpin(
      () => loading.value,
      emitRefresh,
      delay
    );

    loading.value = true; // 页面接线：@refresh 把 loading 置 true
    onReFresh();
    expect(emitRefresh).toHaveBeenCalledTimes(1);
    expect(spin.value).toBe(true);

    // 慢请求：2s 还在途，圈必须还在（旧行为 500ms 就假熄灭了）
    vi.advanceTimersByTime(2000);
    await nextTick();
    expect(spin.value).toBe(true);

    loading.value = false; // 请求回来
    await nextTick();
    expect(spin.value).toBe(false);
  });

  it("快请求：外部 loading 恒 false 时不转圈（外部态压过内部标记）", async () => {
    const { spin, onReFresh } = useRefreshSpin(() => false, vi.fn(), delay);

    onReFresh();
    await nextTick();
    expect(spin.value).toBe(false);

    vi.advanceTimersByTime(1000);
    await nextTick();
    expect(spin.value).toBe(false);
  });

  it("接线后内部不再起 500ms 定时器：外部恒 true 时圈不会被偷偷熄灭", async () => {
    const delayFn = vi.fn(delay);
    const { spin, onReFresh } = useRefreshSpin(() => true, vi.fn(), delayFn);

    onReFresh();
    expect(delayFn).not.toHaveBeenCalled(); // 熄灭定时器根本没建

    vi.advanceTimersByTime(5000);
    await nextTick();
    expect(spin.value).toBe(true);
  });

  it("外部从 true 翻回 false 再点刷新：spin 仍只认外部当前值", async () => {
    const loading = ref(true);
    const { spin, onReFresh } = useRefreshSpin(
      () => loading.value,
      vi.fn(),
      delay
    );

    onReFresh();
    expect(spin.value).toBe(true);

    loading.value = false;
    await nextTick();
    expect(spin.value).toBe(false);

    onReFresh(); // 第二次点击时外部已是 false
    await nextTick();
    expect(spin.value).toBe(false);
  });
});