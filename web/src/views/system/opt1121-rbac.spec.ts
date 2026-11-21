/** QR1121-W17 / W5 的回归锁：RBAC 三页失败重试 + 菜单图标离线预览。
 *
 *  - W17：加载失败时空态给「重试」按钮（collect 页同范式），点击再发请求、
 *    成功后按钮退场；W28 的三态文案一字不动（只补行动入口）。
 *  - W5（组件侧）：KkMenuIcon 已知图标渲染 svg、未知/含 : 的字符串显原串
 *    不递给渲染链（离线红线；纯函数侧锁在 utils/kkIcons.spec.ts）。
 *
 *  EP 替身沿用 opt1120-rbac 的口径，但 ElEmpty 渲染 default 插槽——
 *  重试按钮落在插槽里，原替身只渲染 description 会把它吞掉。
 */
// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from "vitest";
import { defineComponent, h, nextTick } from "vue";
import { mount } from "@vue/test-utils";

const hMocks = vi.hoisted(() => ({
  msgError: vi.fn(),
  msgSuccess: vi.fn(),
  msgWarning: vi.fn(),
  confirm: vi.fn(),
  listUsers: vi.fn(),
  listRoles: vi.fn(),
  listMenus: vi.fn(),
  createUser: vi.fn(),
  updateUser: vi.fn(),
  deleteUser: vi.fn(),
  createRole: vi.fn(),
  updateRole: vi.fn(),
  deleteRole: vi.fn(),
  updateMenu: vi.fn(),
  username: "admin"
}));

vi.mock("element-plus", () => ({
  ElMessageBox: { confirm: hMocks.confirm }
}));
// QR1123-W5：三页迁 message() 单一入口；mock 按 type 分发到既有 per-type spy
vi.mock("@/utils/message", () => ({
  message: (msg: any, params?: any) => {
    const t = params?.type ?? "info";
    if (t === "success") return hMocks.msgSuccess(msg);
    if (t === "warning") return hMocks.msgWarning(msg);
    return hMocks.msgError(msg);
  }
}));
vi.mock("@/api/rbac", () => ({
  listUsers: (...a: any[]) => hMocks.listUsers(...a),
  listRoles: (...a: any[]) => hMocks.listRoles(...a),
  listMenus: (...a: any[]) => hMocks.listMenus(...a),
  createUser: (...a: any[]) => hMocks.createUser(...a),
  updateUser: (...a: any[]) => hMocks.updateUser(...a),
  deleteUser: (...a: any[]) => hMocks.deleteUser(...a),
  createRole: (...a: any[]) => hMocks.createRole(...a),
  updateRole: (...a: any[]) => hMocks.updateRole(...a),
  deleteRole: (...a: any[]) => hMocks.deleteRole(...a),
  updateMenu: (...a: any[]) => hMocks.updateMenu(...a)
}));
vi.mock("@/store/modules/user", () => ({
  useUserStoreHook: () => ({ username: hMocks.username })
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
/** 与 opt1120 的差异：渲染 default 插槽（重试按钮的落点） */
const ElEmpty = defineComponent({
  name: "ElEmpty",
  props: { description: { type: String, default: "" } },
  setup(props, { slots }) {
    return () =>
      h("div", { class: "empty-stub" }, [props.description, slots.default?.()]);
  }
});
const passthrough = (name: string) =>
  defineComponent({
    name,
    setup(_, { slots }) {
      return () => h("div", { class: `${name}-stub` }, slots.default?.());
    }
  });

const globalStubs = {
  components: {
    ElTable,
    ElEmpty,
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
    ElButton: defineComponent({
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
    }),
    ElDialog: passthrough("ElDialog"),
    ElForm: passthrough("ElForm"),
    ElFormItem: passthrough("ElFormItem"),
    ElInput: passthrough("ElInput"),
    ElInputNumber: passthrough("ElInputNumber"),
    ElSelect: passthrough("ElSelect"),
    ElOption: passthrough("ElOption"),
    ElSwitch: passthrough("ElSwitch"),
    ElTag: passthrough("ElTag"),
    ElTree: passthrough("ElTree"),
    ElTooltip: passthrough("ElTooltip"),
    ElIcon: passthrough("ElIcon"),
    ElTableColumn: passthrough("ElTableColumn")
  },
  config: { warnHandler: () => {} }
};

import UserPage from "./user/index.vue";
import RolePage from "./role/index.vue";
import MenuPage from "./menu/index.vue";
import KkMenuIcon from "@/components/KkMenuIcon.vue";

const settle = async () => {
  await nextTick();
  await nextTick();
  await nextTick();
};

const retryButton = (w: ReturnType<typeof mount>) =>
  w.findAll("button").find(b => b.text() === "重试");

beforeEach(() => {
  vi.clearAllMocks();
  hMocks.listUsers.mockResolvedValue({ items: [] });
  hMocks.listRoles.mockResolvedValue({ items: [] });
  hMocks.listMenus.mockResolvedValue({ items: [] });
});

describe("W17：加载失败 → 空态重试按钮", () => {
  it("用户页：失败显重试（W28 文案不动），点击再发请求、成功后退场", async () => {
    hMocks.listUsers.mockRejectedValue(new Error("down"));
    const w = mount(UserPage, { global: globalStubs });
    await settle();

    expect(w.find(".empty-stub").text()).toContain("没读到（用户接口不可达）");
    const retry = retryButton(w);
    expect(retry).toBeTruthy();

    hMocks.listUsers.mockResolvedValue({
      items: [
        {
          username: "bob",
          role: "operator",
          role_label: "操作员",
          enabled: true,
          created: 1690000000
        }
      ]
    });
    await retry!.trigger("click");
    await settle();
    expect(hMocks.listUsers).toHaveBeenCalledTimes(2);
    expect(retryButton(w)).toBeUndefined(); // 成功后按钮退场
  });

  it("角色页：失败显重试，成功后退场", async () => {
    hMocks.listRoles.mockRejectedValue(new Error("down"));
    const w = mount(RolePage, { global: globalStubs });
    await settle();

    expect(w.find(".empty-stub").text()).toContain("没读到（角色接口不可达）");
    const retry = retryButton(w);
    expect(retry).toBeTruthy();

    hMocks.listRoles.mockResolvedValue({ items: [] });
    await retry!.trigger("click");
    await settle();
    expect(retryButton(w)).toBeUndefined();
    // 读到空表 → W28 的真空文案
    expect(w.find(".empty-stub").text()).toContain("还没有角色");
  });

  it("菜单页：失败显重试，成功后退场", async () => {
    hMocks.listMenus.mockRejectedValue(new Error("down"));
    const w = mount(MenuPage, { global: globalStubs });
    await settle();

    expect(w.find(".empty-stub").text()).toContain("没读到（菜单接口不可达）");
    const retry = retryButton(w);
    expect(retry).toBeTruthy();

    hMocks.listMenus.mockResolvedValue({ items: [] });
    await retry!.trigger("click");
    await settle();
    expect(retryButton(w)).toBeUndefined();
    expect(w.find(".empty-stub").text()).toContain("菜单表为空");
  });

  it("读到真空时没有重试按钮（重试只给失败态，不给空表）", async () => {
    const w = mount(UserPage, { global: globalStubs });
    await settle();
    expect(w.find(".empty-stub").text()).toContain("还没有用户");
    expect(retryButton(w)).toBeUndefined();
  });
});

describe("W5：KkMenuIcon 离线预览（组件侧）", () => {
  it("已注册图标渲染 svg（ep/monitor 在 offlineIcon 名单内）", () => {
    const w = mount(KkMenuIcon, {
      props: { icon: "ep/monitor" },
      global: globalStubs
    });
    expect(w.html()).toContain("svg");
  });

  it("含 : 的在线图标字符串显原串、不渲染 svg（离线红线）", () => {
    const w = mount(KkMenuIcon, {
      props: { icon: "ep:monitor" },
      global: globalStubs
    });
    expect(w.text()).toContain("ep:monitor");
    expect(w.html()).not.toContain("svg");
  });

  it("未注册字符串显原串 + 提示；空串显 -", () => {
    const w = mount(KkMenuIcon, {
      props: { icon: "ep/not-registered" },
      global: globalStubs
    });
    expect(w.text()).toContain("ep/not-registered");
    expect(w.find("span").attributes("title")).toContain("未注册图标");

    const empty = mount(KkMenuIcon, {
      props: { icon: "" },
      global: globalStubs
    });
    expect(empty.text()).toBe("-");
  });
});
