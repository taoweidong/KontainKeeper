/** QR1124 白名单管理页回归锁：
 *  - 三态空态（QR-W16）：加载失败「没读到（白名单接口不可达）」+ 重试；读到空表「还没有白名单规则」。
 *  - 删除二次确认：文案含 CIDR + 「立即被拒」，取消不发 DELETE。
 *  - 停用/删除最后一条启用规则前警告「放行所有上报」，取消不发请求。
 *  - 弹窗 min(92vw, 460px) 窄屏不溢出。
 *  EP 轻量替身测页面逻辑不测渲染（照 opt1120-rbac.spec.ts）。 */
// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from "vitest";
import { defineComponent, h, nextTick } from "vue";
import { mount } from "@vue/test-utils";

const hMocks = vi.hoisted(() => ({
  confirm: vi.fn(),
  msgError: vi.fn(),
  msgSuccess: vi.fn(),
  listIpRules: vi.fn(),
  createIpRule: vi.fn(),
  updateIpRule: vi.fn(),
  deleteIpRule: vi.fn()
}));

vi.mock("element-plus", () => ({ ElMessageBox: { confirm: hMocks.confirm } }));
vi.mock("@/utils/message", () => ({
  message: (msg: any, params?: any) => {
    const t = params?.type ?? "info";
    if (t === "success") return hMocks.msgSuccess(msg);
    return hMocks.msgError(msg);
  }
}));
vi.mock("@/api/ipwhitelist", () => ({
  listIpRules: (...a: any[]) => hMocks.listIpRules(...a),
  createIpRule: (...a: any[]) => hMocks.createIpRule(...a),
  updateIpRule: (...a: any[]) => hMocks.updateIpRule(...a),
  deleteIpRule: (...a: any[]) => hMocks.deleteIpRule(...a)
}));

const ElTable = defineComponent({
  name: "ElTable",
  props: { data: { type: Array, default: () => [] } },
  setup(props, { slots }) {
    return () => {
      const cols = (slots.default?.() ?? [])
        .flat(Infinity)
        .filter((v: any) => v && typeof v === "object" && v.children?.default);
      return h("div", { class: "table-stub" }, [
        props.data.length
          ? props.data.map((row: any) =>
              h(
                "div",
                { class: "row-stub" },
                cols.map((col: any) => col.children.default({ row }))
              )
            )
          : slots.empty?.()
      ]);
    };
  }
});
// 渲染 description + default 插槽（重试按钮落点）
const ElEmpty = defineComponent({
  name: "ElEmpty",
  props: { description: { type: String, default: "" } },
  setup(props, { slots }) {
    return () =>
      h("div", { class: "empty-stub" }, [props.description, slots.default?.()]);
  }
});
const ElDialog = defineComponent({
  name: "ElDialog",
  props: { modelValue: Boolean, width: { type: String, default: "" } },
  setup(_, { slots }) {
    return () =>
      h("div", { class: "dialog-stub" }, [slots.default?.(), slots.footer?.()]);
  }
});
const ElForm = defineComponent({
  name: "ElForm",
  setup(_, { slots, expose }) {
    expose({ validate: (cb: (v: boolean) => void) => cb(true) });
    return () => h("div", slots.default?.());
  }
});
const passthrough = (name: string) =>
  defineComponent({
    name,
    setup(_, { slots }) {
      return () => h("div", { class: `${name}-stub` }, slots.default?.());
    }
  });
const ElButton = defineComponent({
  name: "ElButton",
  emits: ["click"],
  setup(_, { slots, emit }) {
    return () =>
      h(
        "button",
        { class: "button-stub", onClick: () => emit("click") },
        slots.default?.()
      );
  }
});

const globalStubs = {
  components: {
    ElTable,
    ElEmpty,
    ElDialog,
    ElForm,
    ElButton,
    ElCard: defineComponent({
      name: "ElCard",
      setup(_, { slots }) {
        return () =>
          h("div", { class: "card-stub" }, [
            slots.header?.(),
            slots.default?.()
          ]);
      }
    }),
    ElFormItem: passthrough("ElFormItem"),
    ElInput: passthrough("ElInput"),
    ElSwitch: passthrough("ElSwitch"),
    ElTag: passthrough("ElTag"),
    ElTableColumn: passthrough("ElTableColumn")
  },
  config: { warnHandler: () => {} }
};

import Page from "./index.vue";

const rule = (over: Record<string, any> = {}) => ({
  id: 1,
  cidr: "10.0.0.0/24",
  remark: "dev",
  enabled: true,
  created: 1690000000,
  ...over
});

const settle = async () => {
  await nextTick();
  await new Promise(r => setTimeout(r, 0));
  await nextTick();
};

function mountPage() {
  return mount(Page, { global: globalStubs });
}

beforeEach(() => {
  vi.clearAllMocks();
  hMocks.confirm.mockResolvedValue(undefined);
  hMocks.listIpRules.mockResolvedValue({ items: [] });
  hMocks.createIpRule.mockResolvedValue({ ok: true, cidr: "10.0.0.0/24" });
  hMocks.updateIpRule.mockResolvedValue({ ok: true });
  hMocks.deleteIpRule.mockResolvedValue({ ok: true });
});

describe("三态空态 + 重试（QR-W16 / QR1121-W17）", () => {
  it("加载失败 → 没读到 + 重试按钮；重试再发请求", async () => {
    hMocks.listIpRules.mockRejectedValueOnce(new Error("down"));
    const w = mountPage();
    await settle();
    expect(w.find(".empty-stub").text()).toContain(
      "没读到（白名单接口不可达）"
    );
    const retry = w.findAll("button").find(b => b.text() === "重试");
    expect(retry).toBeTruthy();
    await retry!.trigger("click");
    await settle();
    expect(hMocks.listIpRules).toHaveBeenCalledTimes(2);
    expect(w.find(".empty-stub").text()).toContain("还没有白名单规则");
  });

  it("读到空表 → 还没有白名单规则（真空才强断言）", async () => {
    const w = mountPage();
    await settle();
    expect(w.find(".empty-stub").text()).toContain("还没有白名单规则");
    expect(w.findAll("button").find(b => b.text() === "重试")).toBeUndefined();
  });
});

describe("enabled 布尔归一（QR1124）", () => {
  it("后端整型 1/0 → 布尔：防 el-switch 非布尔值误触发 change", async () => {
    hMocks.listIpRules.mockResolvedValue({
      items: [rule({ enabled: 1 }), rule({ id: 2, cidr: "192.168.0.0/16", enabled: 0 })]
    });
    const w = mountPage();
    await settle();
    const rules = (w.vm as any).rules;
    expect(rules[0].enabled).toBe(true);
    expect(rules[1].enabled).toBe(false);
  });
});

describe("删除二次确认", () => {
  it("文案含 CIDR + 立即被拒；取消不发 DELETE，确认才删", async () => {
    hMocks.listIpRules.mockResolvedValue({
      items: [rule(), rule({ id: 2, cidr: "192.168.0.0/16" })]
    });
    const w = mountPage();
    await settle();
    hMocks.confirm.mockRejectedValueOnce(new Error("cancel"));
    await (w.vm as any).onRemove(rule());
    expect(hMocks.confirm).toHaveBeenCalledTimes(1);
    expect(hMocks.confirm.mock.calls[0][0]).toContain("10.0.0.0/24");
    expect(hMocks.confirm.mock.calls[0][0]).toContain("立即被拒");
    expect(hMocks.deleteIpRule).not.toHaveBeenCalled();

    await (w.vm as any).onRemove(rule());
    expect(hMocks.deleteIpRule).toHaveBeenCalledWith(1);
  });

  it("删除最后一条启用规则 → 文案追加放行警告", async () => {
    hMocks.listIpRules.mockResolvedValue({ items: [rule()] });
    const w = mountPage();
    await settle();
    await (w.vm as any).onRemove(rule());
    expect(hMocks.confirm.mock.calls[0][0]).toContain("放行所有上报");
  });
});

describe("停用最后一条启用规则警告", () => {
  it("单条启用时停用 → 先警告，取消不发请求", async () => {
    hMocks.listIpRules.mockResolvedValue({ items: [rule()] });
    const w = mountPage();
    await settle();
    hMocks.confirm.mockRejectedValueOnce(new Error("cancel"));
    await (w.vm as any).onToggle(rule(), false);
    expect(hMocks.confirm.mock.calls[0][0]).toContain("放行所有上报");
    expect(hMocks.updateIpRule).not.toHaveBeenCalled();

    await (w.vm as any).onToggle(rule(), false);
    expect(hMocks.updateIpRule).toHaveBeenCalledWith(1, { enabled: false });
  });

  it("多条启用时停用不警告", async () => {
    hMocks.listIpRules.mockResolvedValue({
      items: [rule(), rule({ id: 2, cidr: "192.168.0.0/16" })]
    });
    const w = mountPage();
    await settle();
    await (w.vm as any).onToggle(rule(), false);
    expect(hMocks.confirm).not.toHaveBeenCalled();
    expect(hMocks.updateIpRule).toHaveBeenCalledWith(1, { enabled: false });
  });
});

describe("弹窗与新增", () => {
  it("弹窗宽度 min(92vw, 460px)", async () => {
    const w = mountPage();
    await settle();
    expect(w.findComponent(ElDialog).props("width")).toBe("min(92vw, 460px)");
  });

  it("新增提交 → createIpRule 带 cidr/remark", async () => {
    const w = mountPage();
    await settle();
    (w.vm as any).openCreate();
    (w.vm as any).form.cidr = "172.16.0.0/12";
    (w.vm as any).form.remark = "office";
    await (w.vm as any).submit();
    await settle();
    expect(hMocks.createIpRule).toHaveBeenCalledWith({
      cidr: "172.16.0.0/12",
      remark: "office"
    });
  });
});
