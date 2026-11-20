/** 刷新图标 spin 的状态机（QR1120-W27，从 bar.tsx 抽出以便单测）。
 *
 *  旧行为：onReFresh 固定 delay(500) 熄灭转圈，与真实请求毫无关系——慢请求
 *  500ms 后圈停了数据还没回，快请求早就回了圈还在空转。
 *
 *  新行为（additive，不破坏未接线页面）：
 *  - 外部没传 loading（undefined）→ 保持旧的固定 500ms 假转圈；
 *  - 外部传了 loading → spin 完全跟随它，内部不再另起熄灭定时器。
 *
 *  抽成纯 composable 的原因：bar.tsx 的 ~icons / svg?component 导入依赖 vite
 *  插件解析，vitest 环境装不了（vitest.config 不归本轮改），挂载整组件测不了；
 *  状态机本身与 DOM 无关，node 环境即可锁死语义。
 */
import { computed, ref } from "vue";

export function useRefreshSpin(
  /** 读外部 loading prop 的当前值（undefined = 页面未接线） */
  getExternal: () => boolean | undefined,
  /** 点击刷新时向页面 emit("refresh") */
  emitRefresh: () => void,
  /** 延时函数（生产传 @pureadmin/utils 的 delay；测试可注入 fake timers 下的实现） */
  delayFn: (ms: number) => Promise<unknown>
) {
  /** 未接线页面用的内部假转圈标记 */
  const internal = ref(false);

  /** spin 的唯一来源：外部接了真实 loading 就用它，没接才退回内部那份 */
  const spin = computed(() => getExternal() ?? internal.value);

  function onReFresh() {
    internal.value = true;
    emitRefresh();
    // 固定 500ms 熄灭只服务于未接线的旧页面；接了 loading 的页面由真实请求收圈
    if (getExternal() === undefined) {
      delayFn(500).then(() => (internal.value = false));
    }
  }

  return { spin, onReFresh };
}