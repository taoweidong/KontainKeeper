/** QR1120-W23 / W28 / W29 / W32 的回归锁：RBAC 三页（用户/角色/菜单）。
 *
 *  锁住的语义：
 *  - W23：删除用户/角色必须先过 ElMessageBox.confirm（文案带名字与影响），
 *    取消不发 DELETE；确认后才真删。
 *  - W28：三态红线（QR-W16）——首次加载失败显「没读到（接口不可达）」，
 *    读到空表才许说「还没有用户/角色」「菜单表为空」。
 *  - W29：弹窗宽度 min(92vw, Npx)，窄屏不溢出。
 *  - W32：角色创建两段式——createRole 成功而 updateRole(menus) 失败时，
 *    提示必须指向「编辑该角色补授权」（重试新增会撞 409），并关框刷新让
 *    无授权角色可见可编辑。
 *
 *  element-plus 用轻量替身（el-table 只渲染 #empty、el-dialog 暴露 width、
 *  el-form.validate 恒真、el-tree 暴露勾选句柄）：本锁测页面逻辑，不测 EP 渲染。
 */
// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { defineComponent, h, nextTick } from "vue";
import { mount } from "@vue/test-utils";

const hMocks = vi.hoisted(() => ({
  confirm: vi.fn(),
  msgError: vi.fn(),
  msgSuccess: vi.fn(),
  msgWarning: vi.fn(),
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
  checkedKeys: [] as string[],
  username: "admin"
}));

vi.mock("element-plus", () => ({
  ElMessageBox: { confirm: hMocks.confirm }
}));
// QR1123-W5：三页迁 message() 单一入口；mock 按 type 分发到既有 per-type spy，
// 下方 msgError/msgSuccess 断言零改写
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

/** el-table 替身：data 为空渲染 #empty（三态文案落点）；非空逐行调各列的
 *  #default 作用域插槽（操作列的编辑/删除按钮因此可点）。 */
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
const ElEmpty = defineComponent({
  name: "ElEmpty",
  props: { description: { type: String, default: "" } },
  setup(props) {
    return () => h("div", { class: "empty-stub" }, props.description);
  }
});
const ElDialog = defineComponent({
  name: "ElDialog",
  props: {
    modelValue: Boolean,
    width: { type: String, default: "" },
    title: { type: String, default: "" }
  },
  setup(_, { slots }) {
    return () =>
      h("div", { class: "dialog-stub" }, [slots.default?.(), slots.footer?.()]);
  }
});
const ElForm = defineComponent({
  name: "ElForm",
  setup(_, { slots, expose }) {
    expose({ validate: (cb: (valid: boolean) => void) => cb(true) });
    return () => h("div", slots.default?.());
  }
});
const ElTree = defineComponent({
  name: "ElTree",
  setup(_, { expose }) {
    expose({
      setCheckedKeys: vi.fn(),
      getCheckedKeys: () => hMocks.checkedKeys,
      getHalfCheckedKeys: () => []
    });
    return () => h("div");
  }
});
/** 其余 EP 组件：透传插槽的壳，按钮保留 click 可触发 */
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
    ElDialog,
    ElForm,
    ElTree,
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
    ElFormItem: passthrough("ElFormItem"),
    ElInput: passthrough("ElInput"),
    ElSelect: passthrough("ElSelect"),
    ElOption: passthrough("ElOption"),
    ElSwitch: passthrough("ElSwitch"),
    ElTag: passthrough("ElTag"),
    ElInputNumber: passthrough("ElInputNumber"),
    ElTableColumn: passthrough("ElTableColumn")
  },
  config: { warnHandler: () => {} }
};

import UserPage from "./user/index.vue";
import RolePage from "./role/index.vue";
import MenuPage from "./menu/index.vue";

const settle = async () => {
  await nextTick();
  await nextTick();
  await nextTick();
};

const userRow = (over: Record<string, any> = {}) => ({
  username: "bob",
  role: "operator",
  role_label: "操作员",
  enabled: true,
  created: 1690000000,
  ...over
});
const roleRow = (over: Record<string, any> = {}) => ({
  name: "ops",
  label: "运维",
  remark: "",
  menus: ["host"],
  users: 0,
  built_in: false,
  created: 1690000000,
  ...over
});
const menuRow = (over: Record<string, any> = {}) => ({
  name: "host",
  title: "主机",
  parent: "",
  path: "/host",
  component: "host/index",
  icon: "",
  rank: 1,
  show_link: true,
  enabled: true,
  built_in: true,
  ...over
});

/** 找操作列里的「删除」按钮（文本匹配，避开「编辑」） */
const deleteButtons = (w: ReturnType<typeof mount>) =>
  w.findAll("button").filter(b => b.text() === "删除");

beforeEach(() => {
  vi.clearAllMocks();
  hMocks.checkedKeys = [];
  hMocks.confirm.mockResolvedValue(undefined); // 默认「确认」
  hMocks.listUsers.mockResolvedValue({ items: [userRow()] });
  hMocks.listRoles.mockResolvedValue({ items: [roleRow()] });
  hMocks.listMenus.mockResolvedValue({ items: [menuRow()] });
  hMocks.deleteUser.mockResolvedValue({ ok: true });
  hMocks.deleteRole.mockResolvedValue({ ok: true });
  hMocks.createRole.mockResolvedValue({ ok: true });
  hMocks.updateRole.mockResolvedValue({ ok: true });
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("W23：删除二次确认", () => {
  it("用户：先 confirm（文案带用户名与影响），确认后才 DELETE", async () => {
    const w = mount(UserPage, { global: globalStubs });
    await settle();

    await deleteButtons(w)[0].trigger("click");
    expect(hMocks.confirm).toHaveBeenCalledTimes(1);
    const [msg, title, opts] = hMocks.confirm.mock.calls[0];
    expect(msg).toContain("bob");
    expect(msg).toContain("不可恢复");
    expect(title).toBe("删除用户");
    expect(opts.type).toBe("warning");
    expect(hMocks.deleteUser).toHaveBeenCalledWith("bob");
  });

  it("用户：取消（confirm reject）不发 DELETE", async () => {
    hMocks.confirm.mockRejectedValue("cancel");
    const w = mount(UserPage, { global: globalStubs });
    await settle();

    await deleteButtons(w)[0].trigger("click");
    await settle();
    expect(hMocks.deleteUser).not.toHaveBeenCalled();
  });

  it("角色：先 confirm（文案带角色名与标识），确认后才 DELETE；取消不发", async () => {
    const w = mount(RolePage, { global: globalStubs });
    await settle();

    await deleteButtons(w)[0].trigger("click");
    const [msg, title] = hMocks.confirm.mock.calls[0];
    expect(msg).toContain("运维");
    expect(msg).toContain("ops");
    expect(title).toBe("删除角色");
    expect(hMocks.deleteRole).toHaveBeenCalledWith("ops");

    hMocks.deleteRole.mockClear();
    hMocks.confirm.mockRejectedValue("cancel");
    await deleteButtons(w)[0].trigger("click");
    await settle();
    expect(hMocks.deleteRole).not.toHaveBeenCalled();
  });
});

describe("W28：空态三态（QR-W16）", () => {
  it("用户页首次加载失败 → 「没读到（用户接口不可达）」，不是「还没有用户」", async () => {
    hMocks.listUsers.mockRejectedValue(new Error("500"));
    const w = mount(UserPage, { global: globalStubs });
    await settle();

    expect(w.find(".empty-stub").text()).toBe("没读到（用户接口不可达）");
  });

  it("用户页读到空表 → 「还没有用户」（真空才是强断言）", async () => {
    hMocks.listUsers.mockResolvedValue({ items: [] });
    const w = mount(UserPage, { global: globalStubs });
    await settle();

    expect(w.find(".empty-stub").text()).toBe("还没有用户");
  });

  it("角色页首次加载失败 → 「没读到（角色接口不可达）」；读到空表才说「还没有角色」", async () => {
    hMocks.listRoles.mockRejectedValue(new Error("500"));
    const w = mount(RolePage, { global: globalStubs });
    await settle();
    expect(w.find(".empty-stub").text()).toBe("没读到（角色接口不可达）");

    hMocks.listRoles.mockResolvedValue({ items: [] });
    const w2 = mount(RolePage, { global: globalStubs });
    await settle();
    expect(w2.find(".empty-stub").text()).toBe("还没有角色");
  });

  it("菜单页首次加载失败 → 「没读到（菜单接口不可达）」；读到空表才说「菜单表为空」", async () => {
    hMocks.listMenus.mockRejectedValue(new Error("500"));
    const w = mount(MenuPage, { global: globalStubs });
    await settle();
    expect(w.find(".empty-stub").text()).toBe("没读到（菜单接口不可达）");

    hMocks.listMenus.mockResolvedValue({ items: [] });
    const w2 = mount(MenuPage, { global: globalStubs });
    await settle();
    expect(w2.find(".empty-stub").text()).toBe("菜单表为空");
  });
});

describe("W29：弹窗宽度窄屏自适应", () => {
  it("三页弹窗宽度都是 min(92vw, Npx)，不再是固定 px", async () => {
    const cases: Array<[any, string]> = [
      [UserPage, "min(92vw, 460px)"],
      [RolePage, "min(92vw, 520px)"],
      [MenuPage, "min(92vw, 480px)"]
    ];
    for (const [Page, width] of cases) {
      const w = mount(Page, { global: globalStubs });
      await settle();
      expect(
        w.findComponent({ name: "ElDialog" }).props("width"),
        (Page as any).name
      ).toBe(width);
    }
  });
});

describe("W32：角色创建两段式的失败指引", () => {
  /** 打开新增角色弹窗并填表保存（el-form.validate 替身恒真） */
  async function createRoleWithMenus(w: ReturnType<typeof mount>) {
    // 「新增角色」按钮
    const addBtn = w.findAll("button").find(b => b.text() === "新增角色");
    await addBtn!.trigger("click");
    await settle();

    // input 替身不做双向绑，直接写 setup 的 form reactive（dev 构建下实例代理
    // 暴露 setupState）；菜单勾选走 ElTree 替身暴露的句柄
    const vm = w.vm as any;
    vm.form.name = "ops2";
    vm.form.label = "运维二组";
    hMocks.checkedKeys = ["host"];
    await settle();

    const saveBtn = w.findAll("button").find(b => b.text().trim() === "保存");
    await saveBtn!.trigger("click");
    await settle();
  }

  it("createRole 成功 + updateRole(menus) 失败：提示指向「编辑补授权」，关框刷新", async () => {
    hMocks.updateRole.mockRejectedValue({
      response: { data: { detail: "菜单服务不可用" } }
    });
    const w = mount(RolePage, { global: globalStubs });
    await settle();
    hMocks.listRoles.mockClear();

    await createRoleWithMenus(w);

    expect(hMocks.createRole).toHaveBeenCalledWith({
      name: "ops2",
      label: "运维二组",
      remark: ""
    });
    expect(hMocks.updateRole).toHaveBeenCalledWith("ops2", {
      menus: ["host"]
    });
    const errMsg = hMocks.msgError.mock.calls.at(-1)[0] as string;
    expect(errMsg).toContain("已创建");
    expect(errMsg).toContain("编辑");
    expect(errMsg).toContain("补授权");
    expect(errMsg).toContain("409");
    expect(errMsg).toContain("菜单服务不可用");
    // 关框 + 刷新：无授权角色立刻可见可编辑
    expect((w.vm as any).dialog.visible).toBe(false);
    expect(hMocks.listRoles).toHaveBeenCalled();
    // 不许再报「保存失败」那种让用户以为整体没成功的文案
    expect(errMsg).not.toContain("保存失败");
  });

  it("两段都成功：仍是原来的成功文案，不走失败指引", async () => {
    const w = mount(RolePage, { global: globalStubs });
    await settle();

    await createRoleWithMenus(w);

    expect(hMocks.msgSuccess.mock.calls.at(-1)[0]).toContain("已创建角色");
    expect(hMocks.msgError).not.toHaveBeenCalled();
  });

  it("createRole 本身失败：仍是「保存失败」原语义（角色没建成，不存在补授权）", async () => {
    hMocks.createRole.mockRejectedValue({
      response: { data: { detail: "角色已存在" } }
    });
    const w = mount(RolePage, { global: globalStubs });
    await settle();

    await createRoleWithMenus(w);

    expect(hMocks.msgError.mock.calls.at(-1)[0]).toContain("保存失败");
    expect(hMocks.updateRole).not.toHaveBeenCalled();
  });
});
