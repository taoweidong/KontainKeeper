// @vitest-environment jsdom
/** QR1121-W9 / W10 / W11 的回归锁：主机详情 hook 沉底后的新语义。
 *
 *  - W9（语义修复，旧行为本身是缺陷）：getHost 网络失败原渲染「主机不存在或
 *    尚未上报」，把接口不可达谎报成不存在，违反 QR-W16 红线。现按 HTTP 状态
 *    分流：只有 404 才许说不存在，其余说「没读到（主机接口不可达）」并可重试。
 *  - W10：指标类子表空态文案（该帧没带这项采集 ≠ 接口失败）。
 *  - W11：buildDetailOption 纯函数——双轴单位（CPU % / 内存 MB）、null 显 -、
 *    跨天刻度带月-日、colors 注入（暗色适配）。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { defineComponent, nextTick } from "vue";
import { mount } from "@vue/test-utils";

import { clearPolls } from "@/utils/kkPoll";

const h = vi.hoisted(() => ({
  getHost: vi.fn(),
  getHostMetrics: vi.fn(),
  upgradeHosts: vi.fn(),
  exportMetrics: vi.fn(),
  push: vi.fn(),
  // QR1123-W5：hook 迁 message() 单一入口，mock 随之换（原 ElMessage 三 spy）
  message: vi.fn(),
  winWidth: { value: 1400 }
}));

vi.mock("vue-router", () => ({
  useRoute: () => ({ params: { pod: "h1" } }),
  useRouter: () => ({ push: h.push })
}));
vi.mock("@vueuse/core", async () => {
  const { ref } = await import("vue");
  return { useWindowSize: () => ({ width: ref(h.winWidth.value) }) };
});
vi.mock("@/utils/message", () => ({ message: h.message }));
vi.mock("@/utils/http", () => ({ http: { request: vi.fn() } }));
vi.mock("@/api/containers", () => ({
  getHost: (...a: any[]) => h.getHost(...a),
  getHostMetrics: (...a: any[]) => h.getHostMetrics(...a)
}));
vi.mock("@/api/agent", () => ({
  upgradeHosts: (...a: any[]) => h.upgradeHosts(...a)
}));
vi.mock("@/api/exporting", () => ({
  exportMetrics: (...a: any[]) => h.exportMetrics(...a)
}));

import {
  buildDetailOption,
  DETAIL_TABLE_MAX_H,
  detailEmptyText,
  useHostDetail
} from "./hook";
import { DEFAULT_CHART_COLORS } from "@/utils/kkChart";

type DetailApi = ReturnType<typeof useHostDetail>;
let api: DetailApi;

const settle = async () => {
  await nextTick();
  await vi.advanceTimersByTimeAsync(0);
  await nextTick();
};

function withHook() {
  const Comp = defineComponent({
    setup() {
      api = useHostDetail();
      return () => null;
    }
  });
  return mount(Comp);
}

const detailOf = (over: Record<string, any> = {}) => ({
  pod: "h1",
  image: "img",
  agent_ver: "1.0.0",
  latest_agent_ver: "1.1.0",
  agent_outdated: false,
  hb_interval: 10,
  first_seen: 1690000000,
  last_seen: 1690000100,
  online: true,
  age_sec: 5,
  status_reason: "",
  metrics: {},
  custom: {},
  labels: {},
  commands: [],
  disk_alert: false,
  mem_limit: null,
  disk_limit: null,
  ...over
});

beforeEach(() => {
  vi.useFakeTimers();
  clearPolls();
  h.getHost.mockReset();
  h.getHostMetrics.mockReset();
  h.upgradeHosts.mockReset();
  h.exportMetrics.mockReset();
  h.push.mockReset();
  h.message.mockReset();
  h.getHost.mockResolvedValue(detailOf());
  h.getHostMetrics.mockResolvedValue({
    pod: "h1",
    hours: 24,
    source: "raw",
    series: [],
    mem_limit: null,
    disk_limit: 85,
    disk_limit_raw: null
  });
});

afterEach(() => {
  clearPolls();
  vi.useRealTimers();
});

describe("W9：全失败态 404 与不可达分流（修 QR-W16 红线违反）", () => {
  it("网络错误（无 response）→「没读到（主机接口不可达）」，不许谎报不存在", async () => {
    h.getHost.mockRejectedValue(new Error("Network Error"));
    withHook();
    await settle();
    expect(api.detail.value).toBeNull();
    expect(api.hostError.value).toBe("unreachable");
    expect(api.hostEmptyText.value).toBe("没读到（主机接口不可达）");
    expect(api.hostRead.value).toBe(false);
    // 整页新鲜度必须变冷（详情缺失也是过期）
    expect(api.pollFailed.value).toBe(true);
  });

  it("404 →「主机不存在或尚未上报」（唯一许说不存在的分支）", async () => {
    h.getHost.mockRejectedValue({ response: { status: 404 } });
    withHook();
    await settle();
    expect(api.hostError.value).toBe("notfound");
    expect(api.hostEmptyText.value).toBe("主机不存在或尚未上报");
  });

  it("500 等服务端错误也按不可达处理（不是 404 就不许说不存在）", async () => {
    h.getHost.mockRejectedValue({ response: { status: 500 } });
    withHook();
    await settle();
    expect(api.hostError.value).toBe("unreachable");
  });

  it("重试成功 → 错误态清空、hostRead 置真、lastLoadedAt 有值", async () => {
    h.getHost.mockRejectedValueOnce(new Error("Network Error"));
    withHook();
    await settle();
    expect(api.hostError.value).toBe("unreachable");

    h.getHost.mockResolvedValue(detailOf());
    await api.load();
    expect(api.hostError.value).toBe("");
    expect(api.hostRead.value).toBe(true);
    expect(api.detail.value?.pod).toBe("h1");
    expect(api.lastLoadedAt.value).toBeGreaterThan(0);
    expect(api.pollFailed.value).toBe(false);
  });

  it("getHost 失败而曲线成功 → pollFailed 仍置真（QR1120-W11 口径保持）", async () => {
    h.getHost.mockRejectedValue(new Error("boom"));
    h.getHostMetrics.mockResolvedValue({
      pod: "h1",
      hours: 24,
      source: "raw",
      series: [{ ts: 1, cpu: 1, mem_mb: 2, mem_pct: 1, disk_pct: 1 }],
      mem_limit: null,
      disk_limit: 85,
      disk_limit_raw: null
    });
    withHook();
    await settle();
    expect(api.pollFailed.value).toBe(true);
  });
});

describe("W10：子表空态文案与高度收口", () => {
  it("detailEmptyText：该帧没带这项采集，等一帧心跳", () => {
    expect(detailEmptyText("磁盘")).toBe(
      "该帧没有磁盘数据，等一帧心跳再回来看"
    );
    expect(detailEmptyText("网卡")).toContain("网卡");
  });

  it("子表最大高度统一常量（原 220/260/240 三值收口）", () => {
    expect(DETAIL_TABLE_MAX_H).toBe(260);
  });
});

describe("W11：buildDetailOption 纯函数", () => {
  const pts = [
    { ts: 1700000000, cpu: 33.45, mem_mb: 512 },
    { ts: 1700000060, cpu: null, mem_mb: null }
  ];

  it("双轴：CPU % 轴 0–100，内存 MB 走第二轴；null 原样透传画断线", () => {
    const opt = buildDetailOption(pts, 24) as any;
    expect(opt.series[0].name).toBe("CPU %");
    expect(opt.series[0].data).toEqual([33.45, null]);
    expect(opt.series[1].name).toBe("内存 MB");
    expect(opt.series[1].yAxisIndex).toBe(1);
    expect(opt.series[1].data).toEqual([512, null]);
    expect(opt.yAxis[0].max).toBe(100);
  });

  it("tooltip 按 seriesName 配单位：CPU 带 %、内存带 MB、null 显 -（QR1120-W16）", () => {
    const opt = buildDetailOption(pts, 24) as any;
    const html = opt.tooltip.formatter([
      { seriesName: "CPU %", value: 33.45, marker: "m", axisValueLabel: "t" },
      { seriesName: "内存 MB", value: null, marker: "m", name: "t" }
    ]);
    expect(html).toContain("33.5%");
    expect(html).toContain("内存 MB: -");
  });

  it("跨天窗口（>24h）刻度带月-日，24h 内只有时分秒", () => {
    const day = buildDetailOption(pts, 168) as any;
    const hour = buildDetailOption(pts, 24) as any;
    expect(day.xAxis.data[0]).toMatch(/\d{2}\/\d{2}/);
    expect(hour.xAxis.data[0]).not.toMatch(/\d{2}\/\d{2}/);
  });

  it("colors 注入：轴文字/网格/tooltip 走注入值；不传回落亮色默认", () => {
    const dark = {
      ...DEFAULT_CHART_COLORS,
      text: "#cfd3dc",
      splitLine: "#414243",
      tooltipBg: "#1d1e1f"
    };
    const opt = buildDetailOption(pts, 24, dark) as any;
    expect(opt.xAxis.axisLabel.color).toBe("#cfd3dc");
    expect(opt.yAxis[0].splitLine.lineStyle.color).toBe("#414243");
    expect(opt.tooltip.backgroundColor).toBe("#1d1e1f");
    expect(opt.legend.textStyle.color).toBe("#cfd3dc");

    const def = buildDetailOption(pts, 24) as any;
    expect(def.xAxis.axisLabel.color).toBe(DEFAULT_CHART_COLORS.text);
  });

  it("双轴线色（QR1123-W1）：CPU=主色、内存=成功色，注入与回落", () => {
    const themed = {
      ...DEFAULT_CHART_COLORS,
      line: "#8b5cf6",
      lineAlt: "#22c55e"
    };
    const opt = buildDetailOption(pts, 24, themed) as any;
    expect(opt.series[0].color).toBe("#8b5cf6");
    expect(opt.series[1].color).toBe("#22c55e");

    const def = buildDetailOption(pts, 24) as any;
    expect(def.series[0].color).toBe(DEFAULT_CHART_COLORS.line);
    expect(def.series[1].color).toBe(DEFAULT_CHART_COLORS.lineAlt);
  });
});

describe("descCols 三档断点（QR1121 增 1 列档）", () => {
  it("≥1200 四列 / ≥768 两列 / 更窄一列", async () => {
    h.winWidth.value = 1400;
    withHook();
    await settle();
    expect(api.descCols.value).toBe(4);

    h.winWidth.value = 1024;
    withHook();
    await settle();
    expect(api.descCols.value).toBe(2);

    h.winWidth.value = 500;
    withHook();
    await settle();
    expect(api.descCols.value).toBe(1);
    h.winWidth.value = 1400;
  });
});

describe("v4 标签/自定义采集展示记账", () => {
  it("labels 滤掉 _truncated 标记键，截断标志单独露出", async () => {
    h.getHost.mockResolvedValue(
      detailOf({
        labels: { env: "prod", _truncated: true },
        custom: { gpu_temp: 42 }
      })
    );
    withHook();
    await settle();
    expect(api.labelsList.value).toEqual([["env", "prod"]]);
    expect(api.labelsTruncated.value).toBe(true);
    expect(api.customEntries.value).toEqual([["gpu_temp", 42]]);
  });

  it("无标签无自定义 → 空数组（模板据此不渲染小节）", async () => {
    withHook();
    await settle();
    expect(api.labelsList.value).toEqual([]);
    expect(api.labelsTruncated.value).toBe(false);
    expect(api.customEntries.value).toEqual([]);
  });
});
