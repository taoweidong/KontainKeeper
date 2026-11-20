/** QR1120-W25 的回归锁：shell 页勾选同步 URL 的阈值与残留清理。
 *
 *  锁住的语义：
 *  - 少量勾选（≤20 台且序列化 ≤1500 字符）维持落 URL 的现状（分享兼容）；
 *  - 超阈值不落 URL：选择态存 sessionStorage（刷新可恢复），且 query 不变时
 *    不再触发 router.replace（500 台勾选不再每次触发路由替换 + multiTags 重算）；
 *  - `?cmdline=` 是一次性入口参数：读进表单后不再回写地址栏（命令体不留 URL）；
 *  - 刷新恢复：URL 带 pods 以 URL 为准，否则从 sessionStorage 恢复。
 *
 *  子组件（CommandHistory / CommandWorkbench / HostPicker）与 API 全部桩掉：
 *  本锁只测 URL 同步策略，不测下发与历史（各有其锁）。
 */
// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { defineComponent, h, nextTick, reactive } from "vue";
import { mount } from "@vue/test-utils";

const hMocks = vi.hoisted(() => ({
  replace: vi.fn(),
  route: { query: {} as Record<string, any> },
  listHosts: vi.fn(),
  createCommand: vi.fn(),
  confirmDispatch: vi.fn()
}));

vi.mock("vue-router", () => ({
  useRoute: () => hMocks.route,
  useRouter: () => ({
    // 模拟真实路由：replace 成功后 route.query 随之更新（sameQuery 比较的基准）
    replace: (to: any) => {
      hMocks.replace(to);
      hMocks.route.query = { ...to.query };
    }
  })
}));
vi.mock("@/api/containers", () => ({
  listHosts: (...a: any[]) => hMocks.listHosts(...a)
}));
vi.mock("@/api/commands", () => ({
  createCommand: (...a: any[]) => hMocks.createCommand(...a)
}));
vi.mock("@/utils/kkConfirm", () => ({
  confirmDispatch: (...a: any[]) => hMocks.confirmDispatch(...a)
}));
vi.mock("../components/CommandHistory.vue", () => ({
  default: defineComponent({ name: "CommandHistory", render: () => h("div") })
}));
vi.mock("../components/CommandWorkbench.vue", () => ({
  default: defineComponent({
    name: "CommandWorkbench",
    setup(_, { slots }) {
      return () => h("div", [slots.form?.(), slots.result?.()]);
    }
  })
}));
vi.mock("../components/HostPicker.vue", () => ({
  default: defineComponent({
    name: "HostPicker",
    props: { pods: { type: Array, default: () => [] } },
    emits: ["update:pods"],
    render: () => h("div")
  })
}));

import ShellIndex from "./index.vue";

const STORAGE_KEY = "kk-shell-pods";

/** 挂载选项：EP 组件未注册（本锁不测渲染），压掉解析警告 */
const mountOpts = { global: { config: { warnHandler: () => {} } } };

/** 模拟 HostPicker 的 v-model:pods 回写 */
async function pick(w: ReturnType<typeof mount>, pods: string[]) {
  w.findComponent({ name: "HostPicker" }).vm.$emit("update:pods", pods);
  await nextTick();
  await nextTick();
}

const podsOf = (n: number, prefix = "host-") =>
  Array.from({ length: n }, (_, i) => `${prefix}${i}`);

beforeEach(() => {
  hMocks.replace.mockReset();
  hMocks.route.query = {};
  hMocks.listHosts.mockReset();
  hMocks.listHosts.mockResolvedValue({ items: [] });
  hMocks.createCommand.mockReset();
  hMocks.confirmDispatch.mockReset();
  hMocks.confirmDispatch.mockResolvedValue(true);
  sessionStorage.clear();
});

afterEach(() => {
  sessionStorage.clear();
});

describe("少量勾选：维持落 URL 的现状", () => {
  it("2 台勾选 → query.pods 逗号连接；sessionStorage 不留残留", async () => {
    const w = mount(ShellIndex, mountOpts);
    await nextTick();

    await pick(w, ["host-a", "host-b"]);

    expect(hMocks.replace).toHaveBeenCalled();
    const last = hMocks.replace.mock.calls.at(-1)[0];
    expect(last.query.pods).toBe("host-a,host-b");
    expect(sessionStorage.getItem(STORAGE_KEY)).toBeNull();
  });

  it("恰好 20 台仍落 URL（阈值是 >20，不是 ≥20）", async () => {
    const w = mount(ShellIndex, mountOpts);
    await nextTick();
    const pods = podsOf(20);

    await pick(w, pods);

    const last = hMocks.replace.mock.calls.at(-1)[0];
    expect(last.query.pods).toBe(pods.join(","));
    expect(sessionStorage.getItem(STORAGE_KEY)).toBeNull();
  });

  it("清空勾选 → query.pods 移除", async () => {
    const w = mount(ShellIndex, mountOpts);
    await nextTick();
    await pick(w, ["host-a"]);
    await pick(w, []);

    const last = hMocks.replace.mock.calls.at(-1)[0];
    expect(last.query.pods).toBeUndefined();
  });
});

describe("超阈值：不落 URL，选择态进 sessionStorage", () => {
  it("21 台 → query 无 pods，sessionStorage 存全量", async () => {
    const w = mount(ShellIndex, mountOpts);
    await nextTick();
    const pods = podsOf(21);

    await pick(w, pods);

    const last = hMocks.replace.mock.calls.at(-1)[0];
    expect(last.query.pods).toBeUndefined();
    expect(JSON.parse(sessionStorage.getItem(STORAGE_KEY)!)).toEqual(pods);
  });

  it("台数不超但序列化超 1500 字符 → 同样不落 URL", async () => {
    const w = mount(ShellIndex, mountOpts);
    await nextTick();
    const pods = podsOf(3, "a-very-long-hostname-that-eats-quota-".repeat(20) + "-");
    expect(pods.join(",").length).toBeGreaterThan(1500);

    await pick(w, pods);

    const last = hMocks.replace.mock.calls.at(-1)[0];
    expect(last.query.pods).toBeUndefined();
    expect(JSON.parse(sessionStorage.getItem(STORAGE_KEY)!)).toEqual(pods);
  });

  it("超阈值档内继续勾选：query 不变就不再 router.replace（核心收益）", async () => {
    const w = mount(ShellIndex, mountOpts);
    await nextTick();
    await pick(w, podsOf(21));
    hMocks.replace.mockClear();

    await pick(w, podsOf(25));
    await pick(w, podsOf(30));

    expect(hMocks.replace).not.toHaveBeenCalled(); // 只改内存与 sessionStorage
    expect(JSON.parse(sessionStorage.getItem(STORAGE_KEY)!)).toHaveLength(30);
  });

  it("从超阈值缩回少量：pods 重新落 URL，sessionStorage 清掉", async () => {
    const w = mount(ShellIndex, mountOpts);
    await nextTick();
    await pick(w, podsOf(21));
    expect(sessionStorage.getItem(STORAGE_KEY)).not.toBeNull();

    await pick(w, ["host-a", "host-b"]);

    const last = hMocks.replace.mock.calls.at(-1)[0];
    expect(last.query.pods).toBe("host-a,host-b");
    expect(sessionStorage.getItem(STORAGE_KEY)).toBeNull();
  });

  it("从小勾选涨过阈值：URL 里的旧 pods 必须移除（不许留陈旧小选择）", async () => {
    hMocks.route.query = { pods: "host-0,host-1" };
    const w = mount(ShellIndex, mountOpts);
    await nextTick();

    await pick(w, podsOf(21));

    const last = hMocks.replace.mock.calls.at(-1)[0];
    expect(last.query.pods).toBeUndefined();
  });
});

describe("?cmdline= 一次性入口参数不再残留", () => {
  it("带 cmdline 跳入后首次同步即从 query 抹掉（命令体不留地址栏）", async () => {
    hMocks.route.query = { cmdline: "df -h", pods: "host-a" };
    const w = mount(ShellIndex, mountOpts);
    await nextTick();

    await pick(w, ["host-a", "host-b"]);

    const last = hMocks.replace.mock.calls.at(-1)[0];
    expect(last.query.cmdline).toBeUndefined();
    expect(last.query.pods).toBe("host-a,host-b");
  });
});

describe("刷新恢复：URL 优先，sessionStorage 兜底", () => {
  it("URL 无 pods 时从 sessionStorage 恢复超阈值选择", async () => {
    const pods = podsOf(25);
    sessionStorage.setItem(STORAGE_KEY, JSON.stringify(pods));

    const w = mount(ShellIndex, mountOpts);
    await nextTick();

    expect(
      w.findComponent({ name: "HostPicker" }).props("pods")
    ).toEqual(pods);
  });

  it("URL 带 pods 时以 URL 为准（分享/跳转入口优先于本地残留）", async () => {
    sessionStorage.setItem(STORAGE_KEY, JSON.stringify(podsOf(25)));
    hMocks.route.query = { pods: "host-x,host-y" };

    const w = mount(ShellIndex, mountOpts);
    await nextTick();

    expect(
      w.findComponent({ name: "HostPicker" }).props("pods")
    ).toEqual(["host-x", "host-y"]);
  });

  it("sessionStorage 内容损坏按无预选处理，不挡页面", async () => {
    sessionStorage.setItem(STORAGE_KEY, "{坏 JSON");

    const w = mount(ShellIndex, mountOpts);
    await nextTick();

    expect(
      w.findComponent({ name: "HostPicker" }).props("pods")
    ).toEqual([]);
  });

  it("存储里混入非字符串元素时过滤掉（脏数据不进勾选）", async () => {
    sessionStorage.setItem(
      STORAGE_KEY,
      JSON.stringify(["host-a", 42, null, "", "host-b"])
    );

    const w = mount(ShellIndex, mountOpts);
    await nextTick();

    expect(
      w.findComponent({ name: "HostPicker" }).props("pods")
    ).toEqual(["host-a", "host-b"]);
  });
});