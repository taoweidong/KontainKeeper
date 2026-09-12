/** 轮询统一管理（A5.8 / B4）。
 *
 * 背景：5 个业务页各自 `setInterval` + `onBeforeUnmount(() => clearInterval(...))`，
 * 写法重复三遍（建句柄、存句柄、卸载清理），漏一处就是「切页后仍在轮询」——
 * 命令历史页加分页后轮询逻辑更复杂（自适应间隔 + 保留 offset），统一管理更值。
 *
 * 用法：
 *   const { setPoll } = usePolls();          // 自动在 onBeforeUnmount 清理
 *   setPoll("history", load, 3000);          // 覆盖同名 key，不会叠加
 *   clearPoll("history");                    // 暂停
 *
 * 为什么按 key 而不是返回一个句柄：页面往往有多路轮询（列表 + 统计），
 * 按 key 收纳后卸载时一次 clearPolls() 全清，不必逐个记住句柄。
 */
import { onBeforeUnmount } from "vue";

type Timer = ReturnType<typeof setInterval>;

const _timers = new Map<string, Timer>();

/** 注册（或替换）一路轮询。interval <= 0 时只清不建，等价于暂停。 */
export function setPoll(
  key: string,
  fn: () => void | Promise<void>,
  interval: number,
  immediate = false
): void {
  clearPoll(key);
  if (interval <= 0) return;
  _timers.set(
    key,
    setInterval(() => {
      // 回调里抛异常不能让定时器静默停摆，也不能污染页面
      Promise.resolve()
        .then(fn)
        .catch(() => undefined);
    }, interval)
  );
  if (immediate) {
    Promise.resolve()
      .then(fn)
      .catch(() => undefined);
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

/** 停掉全部轮询：切页/卸载时调用。 */
export function clearPolls(): void {
  _timers.forEach(t => clearInterval(t));
  _timers.clear();
}

/** 当前在跑的轮询 key（调试与测试用）。 */
export function pollKeys(): string[] {
  return [..._timers.keys()];
}

/** 组件内使用：卸载时自动 clearPolls()，杜绝漏清理。 */
export function usePolls() {
  onBeforeUnmount(() => clearPolls());
  return { setPoll, clearPoll, clearPolls, pollKeys };
}
