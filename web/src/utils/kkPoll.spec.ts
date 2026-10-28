/** utils/kkPoll.ts 的单元测试：轮询统一管理（A5.8/B4）与一次性请求竞态守卫。
 *
 *  锁四条地基语义（它们是 6 个业务页轮询的公共依赖，改坏一条就是全站行为漂移）：
 *  - 同 key 覆盖不叠加（resetTimer 语义）
 *  - interval <= 0 只清不建（暂停语义）
 *  - usePolls 作用域卸载只清本作用域注册的 key（QR-W 的「误清别的页」红线）
 *  - useSeq 只放行「最新一次」（迟到包丢弃，QR-W4）
 *  全程 fake timers，无真实等待。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { effectScope } from "vue";

import { clearPoll, clearPolls, pollKeys, setPoll, usePolls, useSeq } from "./kkPoll";

const flushMicrotasks = async () => {
  for (let i = 0; i < 6; i++) await Promise.resolve();
};

beforeEach(() => {
  vi.useFakeTimers();
  clearPolls();
});

afterEach(() => {
  clearPolls();
  vi.useRealTimers();
});

describe("setPoll / clearPoll：注册与覆盖", () => {
  it("到点触发回调，覆盖同名 key 后旧定时器不再触发（不叠加）", async () => {
    const calls: string[] = [];
    setPoll(
      "k",
      () => {
        calls.push("old");
      },
      100
    );
    vi.advanceTimersByTime(100);
    await flushMicrotasks();   // runGuarded 把回调放进微任务，先冲刷再断言
    expect(calls).toEqual(["old"]);

    setPoll(
      "k",
      () => {
        calls.push("new");
      },
      100
    );   // 覆盖：旧的被 clear
    // 同一次 advanceTimersByTime 的多个 tick 之间不插微任务，而在途标记要等
    // 微任务才复位——生产里 3~10s 的间隔永远留足事件循环空隙，逐 tick 冲刷
    // 才是与真实行为同构的写法
    for (let i = 0; i < 5; i++) {
      vi.advanceTimersByTime(100);
      await flushMicrotasks();
    }
    expect(calls).toEqual(["old", "new", "new", "new", "new", "new"]);
  });

  it("interval <= 0 只清不建（暂停语义），key 从登记表移除", () => {
    const fn = vi.fn();
    setPoll("k", fn, 100);
    expect(pollKeys()).toEqual(["k"]);
    setPoll("k", fn, 0);
    expect(pollKeys()).toEqual([]);
    vi.advanceTimersByTime(1000);
    expect(fn).not.toHaveBeenCalled();
  });

  it("immediate=true 注册即先跑一次（且与定时器 tick 共用在途标记）", async () => {
    const gate = { resolve: () => {} };
    let calls = 0;
    const release = new Promise<void>(r => (gate.resolve = r));
    setPoll(
      "k",
      async () => {
        calls++;
        await release;
      },
      100,
      true
    );
    await flushMicrotasks();
    expect(calls).toBe(1);
    // immediate 那一路还在途：随后的 tick 必须被跳过（防重入不区分两条路）
    vi.advanceTimersByTime(300);
    await flushMicrotasks();
    expect(calls).toBe(1);
    gate.resolve();
    await flushMicrotasks();
  });

  it("在途防重入：上一轮 Promise 未 settle 前跳过本轮 tick", async () => {
    let calls = 0;
    let resolveFn!: () => void;
    const gate = new Promise<void>(r => (resolveFn = r));
    setPoll("k", async () => {
      calls++;
      await gate;
    }, 100);

    vi.advanceTimersByTime(100);   // 第一轮开始，挂在 gate 上
    await flushMicrotasks();
    vi.advanceTimersByTime(500);   // 5 个 tick 全部跳过
    await flushMicrotasks();
    expect(calls).toBe(1);

    resolveFn();
    await flushMicrotasks();
    vi.advanceTimersByTime(100);   // settle 后恢复
    await flushMicrotasks();
    expect(calls).toBe(2);
  });

  it("回调抛异常不影响下一轮（定时器不许静默停摆）", async () => {
    let calls = 0;
    setPoll("k", () => {
      calls++;
      throw new Error("boom");
    }, 100);
    for (let i = 0; i < 3; i++) {
      vi.advanceTimersByTime(100);
      await flushMicrotasks();
    }
    expect(calls).toBe(3);
  });
});

describe("clearPolls / pollKeys：全局兜底", () => {
  it("clearPolls 清全部，clearPoll 只清指定 key（不存在时无副作用）", () => {
    setPoll("a", () => {}, 100);
    setPoll("b", () => {}, 100);
    clearPoll("a");
    clearPoll("a");   // 重复清不抛
    expect(pollKeys()).toEqual(["b"]);
    clearPolls();
    expect(pollKeys()).toEqual([]);
  });
});

describe("usePolls：作用域化清理", () => {
  it("scope 停止只清本作用域注册的 key，别页轮询存活", async () => {
    const outside = vi.fn();
    setPoll("other-page", outside, 100);

    const scope = effectScope();
    scope.run(() => {
      const { setPoll } = usePolls();
      setPoll("mine-a", () => {}, 100);
      setPoll("mine-b", () => {}, 100);
    });
    expect(pollKeys().sort()).toEqual(["mine-a", "mine-b", "other-page"]);

    scope.stop();
    expect(pollKeys()).toEqual(["other-page"]);   // 只清自己注册的
    vi.advanceTimersByTime(100);
    await flushMicrotasks();
    expect(outside).toHaveBeenCalled();           // 别页还在跑
  });

  it("同 key 二次注册不产生作用域泄漏，scopedClearPoll 只清登记过的", () => {
    const scope = effectScope();
    scope.run(() => {
      const { setPoll, clearPoll } = usePolls();
      setPoll("k", () => {}, 100);
      setPoll("k", () => {}, 100);   // 覆盖同名 key
      clearPoll("k");
      expect(pollKeys()).toEqual([]);
      setPoll("k2", () => {}, 100);
    });
    scope.stop();
    expect(pollKeys()).toEqual([]);
  });
});

describe("useSeq：一次性请求的竞态守卫", () => {
  it("只有最新一次发起的响应允许写回（迟到包丢弃）", () => {
    const begin = useSeq();
    const first = begin();
    const second = begin();
    expect(first()).toBe(false);    // 第二次发起让第一次的票据作废
    expect(second()).toBe(true);
  });

  it("没有后续发起时票据持续有效", () => {
    const begin = useSeq();
    const mine = begin();
    expect(mine()).toBe(true);
    expect(mine()).toBe(true);      // 可重复判（finally 里用）
  });
});
