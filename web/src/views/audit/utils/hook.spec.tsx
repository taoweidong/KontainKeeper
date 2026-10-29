/** views/audit/utils/hook.tsx 的单元测试：审计页数据流（不挂载业务页，直接驱动 hook）。
 *
 *  锁住的语义（本页是「追溯凭证」，谎报比报错更贵）：
 *  - `read` 三态不许塌成两态：读到过（含读到空表）/ 没读到（接口失败）是两回事，
 *    「暂无审计记录」在接口挂掉时是一句假话（QR-W16 红线）。
 *  - 分页只有一个状态源：`offset` 由 `(currentPage-1)*pageSize` 现场推导，
 *    条数变化必须归位第 1 页且**只打一发**后端（FE-8 同族：既归位又靠 emit 会各打一发）。
 *  - 关键字 300ms 防抖 + trim 后为空要下推 `undefined`（空串会被后端当有效筛选）。
 *  - 迟到包被 `useSeq` 丢弃：慢响应不许写回数据、也不许替最新一路关掉 loading（QR-W4）。
 *  - `loading`/`exporting` 在失败路径上必须复位（否则按钮永久置灰）。
 *  - 动作列的严重度四档（L1-5 评审揪出的真实回归：`login_locked`/`ip_rejected` 这类
 *    事件若只按「含不含 fail」上色，会顶着绿色健康点说「一切正常」）。
 */
// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { defineComponent, nextTick } from "vue";
import { mount } from "@vue/test-utils";

const h = vi.hoisted(() => ({
  listAudit: vi.fn(),
  exportAudit: vi.fn(),
  message: vi.fn(),
  downloadBlob: vi.fn()
}));

vi.mock("@/utils/http", () => ({ http: { request: vi.fn() } }));
vi.mock("@/utils/message", () => ({ message: h.message }));
vi.mock("@/api/exporting", () => ({ exportAudit: h.exportAudit }));
// 只桩 listAudit（网络出口），parseDetail 保持真实实现：明细列的渲染器要用它
vi.mock("@/api/audit", async () => {
  const actual = await vi.importActual<any>("@/api/audit");
  return { ...actual, listAudit: h.listAudit };
});
vi.mock("@/utils/kk", async () => {
  const actual = await vi.importActual<any>("@/utils/kk");
  return { ...actual, downloadBlob: h.downloadBlob };
});

import { useAuditLog } from "./hook";

type AuditApi = ReturnType<typeof useAuditLog>;
let api: AuditApi;

const settle = async () => {
  await nextTick();
  await vi.advanceTimersByTimeAsync(0);
  await nextTick();
};

/** 在组件上下文中驱动 hook（onMounted/onBeforeUnmount/watch 都需要 setup 作用域） */
function withHook() {
  const Comp = defineComponent({
    setup() {
      api = useAuditLog();
      return () => null;
    }
  });
  return mount(Comp);
}

const pageOf = (items: any[] = [], total = items.length) => ({
  items,
  total,
  offset: 0,
  limit: 200
});
const rowOf = (id: number, action = "login.ok") => ({
  id,
  actor: "admin",
  action,
  ts: id * 10
});

beforeEach(() => {
  vi.useFakeTimers();
  h.listAudit.mockReset();
  h.exportAudit.mockReset();
  h.message.mockReset();
  h.downloadBlob.mockReset();
  h.listAudit.mockResolvedValue(pageOf([rowOf(1)]));
});

afterEach(() => {
  vi.useRealTimers();
});

describe("初始加载与唯一状态源", () => {
  it("挂载即拉一次：limit 取 pageSize、offset 由页码推导、无关键字时下推 undefined", async () => {
    withHook();
    await settle();

    expect(h.listAudit).toHaveBeenCalledTimes(1);
    expect(h.listAudit.mock.calls[0][0]).toEqual({
      limit: 200,
      offset: 0,
      keyword: undefined
    });
  });

  it("offset 由 (currentPage-1)*pageSize 现场推导；返回值原样落进 dataList/total", async () => {
    withHook();
    await settle();

    api.pagination.currentPage = 3;
    await api.onSearch();

    expect(h.listAudit.mock.calls[1][0]).toEqual({
      limit: 200,
      offset: 400,
      keyword: undefined
    });
    expect(api.pagination.total).toBe(1);
  });

  it("pageSize 变化 → 归位第 1 页并只打一发（条数是 pure-table 直接改的 reactive）", async () => {
    withHook();
    await settle();
    h.listAudit.mockClear();

    api.pagination.pageSize = 100;
    api.pagination.currentPage = 4; // 用户改条数前可能已在第 4 页
    await settle();
    await settle();

    expect(api.pagination.currentPage).toBe(1);
    expect(h.listAudit).toHaveBeenCalledTimes(1);
    expect(h.listAudit.mock.calls[0][0]).toEqual({
      limit: 100,
      offset: 0,
      keyword: undefined
    });
  });
});

describe("read 三态与失败路径（QR-W16）", () => {
  it("读到空表 read=true：「确实为空」是强断言，与「没读到」分开", async () => {
    h.listAudit.mockResolvedValue(pageOf([], 0));
    withHook();
    await settle();

    expect(api.dataList.value).toEqual([]);
    expect(api.pagination.total).toBe(0);
    expect(api.read.value).toBe(true);
  });

  it("接口失败：read 不回退、loading 复位、旧数据保留、错误文案可读（含无 detail 的回落）", async () => {
    withHook();
    await settle();
    expect(api.read.value).toBe(true);

    h.listAudit.mockRejectedValue({
      response: { data: { detail: "数据库连接失败" } }
    });
    await api.onSearch();

    expect(api.read.value).toBe(true); // 读过就是读过
    expect(api.dataList.value).toHaveLength(1); // 旧数据不被谎称「暂无」
    expect(api.loading.value).toBe(false);
    expect(h.message).toHaveBeenCalledWith("加载审计日志失败：数据库连接失败", {
      type: "error"
    });

    h.listAudit.mockRejectedValue(new Error("Network Error"));
    await api.onSearch();
    expect(h.message.mock.calls.at(-1)[0]).toBe(
      "加载审计日志失败：Network Error"
    );
  });

  it("首次加载就失败：read 保持 false（页面必须显示「接口不可达」而不是「暂无审计记录」）", async () => {
    h.listAudit.mockRejectedValue({ response: { data: { detail: "500" } } });
    withHook();
    await settle();

    expect(api.read.value).toBe(false);
    expect(api.loading.value).toBe(false);
    expect(h.message).toHaveBeenCalledTimes(1);
    expect(h.message.mock.calls[0][1]).toEqual({ type: "error" });
  });
});

describe("迟到包丢弃（useSeq，QR-W4）", () => {
  it("慢响应后到时不写回数据、不替最新一路关 loading", async () => {
    let resolveSlow!: (v: any) => void;
    h.listAudit
      .mockImplementationOnce(
        () =>
          new Promise(r => {
            resolveSlow = r;
          })
      )
      .mockResolvedValue(pageOf([rowOf(99)]));

    withHook(); // onMounted 那一路挂在 slow 上
    await settle();
    await api.onSearch(); // 第二路立即返回 → 拿到最新票据

    expect(api.dataList.value[0].id).toBe(99);
    expect(api.loading.value).toBe(false);

    resolveSlow(pageOf([rowOf(1)]));
    await settle();

    expect(api.dataList.value[0].id).toBe(99); // 迟到包被丢弃
    expect(api.pagination.total).toBe(1);
    expect(api.loading.value).toBe(false);
  });

  it("两路并发：最新一路先回、旧一路后到，也不许把界面回卷成旧数据", async () => {
    withHook();
    await settle();

    const gates: Array<(v: any) => void> = [];
    h.listAudit.mockImplementation(
      () =>
        new Promise(r => {
          gates.push(r);
        })
    );
    void api.onSearch();
    void api.onSearch();
    await settle();
    expect(gates).toHaveLength(2);

    gates[1](pageOf([rowOf(2)]));
    await settle();
    expect(api.dataList.value[0].id).toBe(2);

    gates[0](pageOf([rowOf(1)]));
    await settle();
    expect(api.dataList.value[0].id).toBe(2);
  });
});

describe("关键字防抖与归零", () => {
  it("输入未满 300ms 不打后端；到期归位第 1 页、只打一发并带 trim 值", async () => {
    withHook();
    await settle();
    h.listAudit.mockClear();

    api.form.keyword = "logout";
    await nextTick();
    vi.advanceTimersByTime(299);
    await settle();
    expect(h.listAudit).not.toHaveBeenCalled();

    await vi.advanceTimersByTimeAsync(300);
    await settle();

    expect(h.listAudit).toHaveBeenCalledTimes(1);
    expect(h.listAudit.mock.calls[0][0]).toEqual({
      limit: 200,
      offset: 0,
      keyword: "logout"
    });
    expect(api.pagination.currentPage).toBe(1);
  });

  it("连续输入只保留最后一发；纯空格 trim 后按 undefined 下推（空串会被后端当有效筛选）", async () => {
    withHook();
    await settle();
    h.listAudit.mockClear();

    api.form.keyword = "a";
    await nextTick();
    await vi.advanceTimersByTimeAsync(200);
    api.form.keyword = "  df -h ";
    await nextTick();
    await vi.advanceTimersByTimeAsync(400);
    await settle();

    expect(h.listAudit).toHaveBeenCalledTimes(1);
    expect(h.listAudit.mock.calls[0][0].keyword).toBe("df -h");

    h.listAudit.mockClear();
    api.form.keyword = "   ";
    await nextTick();
    await vi.advanceTimersByTimeAsync(400);
    await settle();
    expect(h.listAudit.mock.calls.at(-1)[0].keyword).toBeUndefined();
  });

  it("卸载清掉待触发的防抖：切页后不再有迟到的一发打后端", async () => {
    const wrapper = withHook();
    await settle();
    h.listAudit.mockClear();

    api.form.keyword = "x";
    await nextTick();
    wrapper.unmount();
    await vi.advanceTimersByTimeAsync(1000);
    await settle();

    expect(h.listAudit).not.toHaveBeenCalled();
  });
});

describe("resetFields 的双路径", () => {
  it("关键字非空时只清空并交给防抖那一路重载（不叠加成两发）", async () => {
    withHook();
    await settle();
    api.form.keyword = "cmd";
    await nextTick();
    await vi.advanceTimersByTimeAsync(300);
    await settle();
    h.listAudit.mockClear();

    api.resetFields();
    await nextTick();

    expect(api.form.keyword).toBe("");
    expect(h.listAudit).not.toHaveBeenCalled(); // 此刻还在防抖窗内

    await vi.advanceTimersByTimeAsync(300);
    await settle();
    expect(h.listAudit).toHaveBeenCalledTimes(1);
    expect(h.listAudit.mock.calls[0][0].keyword).toBeUndefined();
  });

  it("关键字本就为空时立即归位重载一次（防抖不会触发，得自己打）", async () => {
    withHook();
    await settle();
    api.pagination.currentPage = 5;
    h.listAudit.mockClear();

    api.resetFields();
    await settle();

    expect(api.pagination.currentPage).toBe(1);
    expect(h.listAudit).toHaveBeenCalledTimes(1);
    expect(h.listAudit.mock.calls[0][0]).toEqual({
      limit: 200,
      offset: 0,
      keyword: undefined
    });
  });
});

describe("导出", () => {
  const blob = { kind: "blob" } as unknown as Blob;

  it("成功：带上当前关键字与条数、文件名由前端拼、exporting 复位", async () => {
    h.exportAudit.mockResolvedValue(blob);
    withHook();
    await settle();

    api.form.keyword = "login";
    await nextTick();
    await vi.advanceTimersByTimeAsync(300);
    await settle();
    await api.onExport();

    expect(h.exportAudit).toHaveBeenCalledWith({
      keyword: "login",
      limit: 200
    });
    expect(h.downloadBlob.mock.calls[0][0]).toBe(blob);
    expect(h.downloadBlob.mock.calls[0][1]).toMatch(
      /^审计日志_\d{8}-\d{6}\.csv$/
    );
    expect(api.exporting.value).toBe(false);
  });

  it("失败：exporting 必复位、不落盘、给可读错误（按钮不许永久置灰）", async () => {
    h.exportAudit.mockRejectedValue({
      response: { data: { detail: "导出行数超限" } }
    });
    withHook();
    await settle();

    await api.onExport();

    expect(api.exporting.value).toBe(false);
    expect(h.downloadBlob).not.toHaveBeenCalled();
    expect(h.message.mock.calls.at(-1)[0]).toBe("导出失败：导出行数超限");
  });

  it("导出范围与页面所见一致：无关键字下推 undefined，条数取当前 pageSize", async () => {
    h.exportAudit.mockResolvedValue(blob);
    withHook();
    await settle();

    api.pagination.pageSize = 500;
    await settle();
    await api.onExport();

    expect(h.exportAudit).toHaveBeenCalledWith({
      keyword: undefined,
      limit: 500
    });
  });
});

describe("列定义：严重度四档与 label 唯一性（L1-5 回归锁）", () => {
  const actionClass = (action: string): string =>
    (api.columns[3] as any).cellRenderer({ row: { action } }).props.class;

  it("bad 先判：fail/blocked/rejected/locked/violation → 红点红字；mismatch/timeout → 琥珀", async () => {
    withHook();
    await settle();

    for (const a of [
      "login_failed",
      "cmd_blocked",
      "ip_rejected",
      "login_locked",
      "blacklist_violation"
    ])
      expect(actionClass(a), a).toBe("kk-state kk-state--bad kk-bad");
    expect(actionClass("proto_mismatch")).toBe("kk-state kk-state--warn");
    expect(actionClass("cmd timeout")).toBe("kk-state kk-state--warn");
  });

  it("ok/create/restore → 默认实心绿点；其余中性事件 → 空心点（不是错误也不是健康）", async () => {
    withHook();
    await settle();

    expect(actionClass("login.ok")).toBe("kk-state");
    expect(actionClass("agent_upload.create")).toBe("kk-state");
    expect(actionClass("config_restore")).toBe("kk-state");
    expect(actionClass("logout")).toBe("kk-state kk-state--idle");
    expect(actionClass("export_audit")).toBe("kk-state kk-state--idle");
  });

  it("每列都有唯一且非空的 label（缺了会被 PureTableBar 静默丢列）", async () => {
    withHook();
    await settle();

    const labels = api.columns.map((c: any) => c.label);
    expect(labels.every(Boolean)).toBe(true);
    expect(new Set(labels).size).toBe(labels.length);
  });
});
