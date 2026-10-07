/** 轮询统一管理（A5.8 / B4）。
 *
 * 背景：5 个业务页各自 `setInterval` + `onBeforeUnmount(() => clearInterval(...))`，
 * 写法重复三遍（建句柄、存句柄、卸载清理），漏一处就是「切页后仍在轮询」——
 * 命令历史页加分页后轮询逻辑更复杂（自适应间隔 + 保留 offset），统一管理更值。
 *
 * 用法（组件内一律走作用域版本）：
 *   const { setPoll, clearPoll } = usePolls();  // 卸载时只清本组件注册的 key
 *   setPoll("history", load, 3000);             // 覆盖同名 key，不会叠加
 *   clearPoll("history");                       // 暂停
 *
 * 两条地基保证：
 * - 在途防重入：上一轮回调的 Promise 未 settle 前跳过本轮 tick，后端慢时请求不堆积；
 * - 作用域化清理：usePolls() 只清本组件注册过的 key，不再误清其他页面在跑的轮询。
 *
 * 全局 setPoll/clearPoll/clearPolls 仍导出以兼容既有 import；业务代码不要直接用全局版。
 *
 * 同文件另导出 `useSeq()`：那是**手动请求**的竞态守卫（QR-W4），与这里的定时器
 * 在途防重入是互补的两件事——一个管「别叠请求」，一个管「迟到的包别写回界面」。
 */
import { onScopeDispose } from "vue";

type Timer = ReturnType<typeof setInterval>;

const _timers = new Map<string, Timer>();

/** 一路轮询的在途标记：每次注册独享一个，tick 与 immediate 共用 */
interface InFlight {
  current: boolean;
}

/** 回调统一包装：在途则跳过本轮（防慢请求堆积），异常吞掉（不能让定时器静默停摆） */
function runGuarded(fn: () => void | Promise<void>, inflight: InFlight): void {
  if (inflight.current) return;
  inflight.current = true;
  Promise.resolve()
    .then(fn)
    .catch(() => undefined)
    .finally(() => {
      inflight.current = false;
    });
}

/** 注册（或替换）一路轮询。interval <= 0 时只清不建，等价于暂停。 */
export function setPoll(
  key: string,
  fn: () => void | Promise<void>,
  interval: number,
  immediate = false
): void {
  clearPoll(key);
  if (interval <= 0) return;
  const inflight: InFlight = { current: false };
  _timers.set(
    key,
    setInterval(() => {
      runGuarded(fn, inflight);
    }, interval)
  );
  if (immediate) {
    runGuarded(fn, inflight);
  }
}

/** 停掉一路轮询；key 不存在时无副作用。 */
export function clearPoll(key: string): void {
  const t = _timers.get(key);
  if (t !== undefined) {
    clearInterval(t);
    _timers.delete(key);
  }
}

/** 停掉全部轮询：全局兜底（登出等场景）用；组件卸载请走 usePolls() 的作用域清理。 */
export function clearPolls(): void {
  _timers.forEach(t => clearInterval(t));
  _timers.clear();
}

/** 当前在跑的轮询 key（调试与测试用）。 */
export function pollKeys(): string[] {
  return [..._timers.keys()];
}

/** 组件内使用：返回绑定当前组件作用域的 setPoll/clearPoll。
 *  本作用域注册过的 key 在卸载时自动清理，且只清这些——
 *  不再把其他页面/组件仍在跑的轮询一起清掉。 */
export function usePolls() {
  const keys = new Set<string>();

  /** 作用域版 setPoll：登记 key，供卸载时定向清理。 */
  function scopedSetPoll(
    key: string,
    fn: () => void | Promise<void>,
    interval: number,
    immediate = false
  ): void {
    keys.add(key);
    setPoll(key, fn, interval, immediate);
  }

  /** 作用域版 clearPoll：只停本作用域注册过的 key。 */
  function scopedClearPoll(key: string): void {
    keys.delete(key);
    clearPoll(key);
  }

  onScopeDispose(() => {
    keys.forEach(k => clearPoll(k));
    keys.clear();
  });

  return { setPoll: scopedSetPoll, clearPoll: scopedClearPoll };
}

/** 一次性请求的竞态守卫（QR-W4）：只有「最新一次」发起的响应才允许写回状态。
 *
 * `usePolls` 的在途标记只管**定时器**那一路；手动触发的请求（点行看输出、
 * 切筛选翻页、下拉懒加载）没有守卫——慢响应会后到，把界面换成**另一个对象**的数据。
 * 命令历史页的实际形态：连点两行，第一行的输出比第二行晚到，抽屉标题是 B、
 * 正文是 A，运维按这个内容判断线上状态就是事故。
 *
 * 用法（在 setup 里取 `begin`，每次发请求前 `begin()` 拿票据）：
 *   const begin = useSeq();
 *   async function showOut(row: CommandRow) {
 *     const isLatest = begin();
 *     const text = await getCommandOut(row.id);
 *     if (!isLatest()) return;   // 期间又点了一次，本次结果作废
 *     out.text = text;
 *   }
 *
 * 只做「丢弃迟到包」，不取消在途请求：取消要把 AbortSignal 一路穿透到
 * @pureadmin/http 的封装，收益不比它带来的改动面更值。
 */
export function useSeq(): () => () => boolean {
  let seq = 0;
  return () => {
    const mine = ++seq;
    return () => mine === seq;
  };
}
