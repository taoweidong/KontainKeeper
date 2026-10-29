/** monitor/utils/hook 的纯函数回归锁（L1-8 迁移时从 index.vue 抽出，语义一行没改）。
 *
 *  这批函数在 500 台 × 每 10s 轮询的热路径上被逐行调用，边界值全部钉死：
 *  - fillPct 的 NaN/Infinity 钳制（QR29-W5：插件写坏指标不许渲染出字面 NaN）
 *  - tickState 的四档分级边界（30/120/600 秒，写进列头 tooltip 的口径）
 *  - 状态判级的 updating 方点变体（B6：自更新窗口不是故障）
 */
import { describe, expect, it, vi } from "vitest";
import type { HostSummary } from "@/api/containers";

// 本文件只测纯函数，不实例化 hook；mock 掉 import 链上的真实模块——
// api/* 引真实 http 会连带拉起 @/router 的模块级初始化（getHistoryMode 读 env），
// node 环境下没有 VITE_ 变量、直接 crash（update 页 spec 同款隔离，成例照抄）。
vi.mock("vue-router", () => ({ useRouter: () => ({ push: vi.fn() }) }));
vi.mock("@/utils/http", () => ({ http: { request: vi.fn() } }));
vi.mock("@/utils/message", () => ({ message: vi.fn() }));

import {
  HOST_TYPE_LABEL,
  OFFLINE_REASON_LABEL,
  fillPct,
  stateClass,
  stateText,
  tickState
} from "./hook";

const host = (over: Partial<HostSummary> = {}): HostSummary =>
  ({
    pod: "h1",
    image: "",
    agent_ver: "0.1.0",
    latest_agent_ver: "",
    agent_outdated: false,
    online: true,
    age_sec: 0,
    cpu: 0,
    mem_mb: 0,
    disk_pct: 0,
    disk_alert: false,
    status_reason: "",
    host_type: "container",
    os_name: "",
    ip: "",
    group_name: "",
    proto_ver: 4,
    docker_total: 0,
    docker_running: 0,
    docker_unhealthy: 0,
    caps: {},
    ...over
  }) as HostSummary;

describe("fillPct：计量条 0–100 钳制（QR29-W5）", () => {
  it("坏值一律回 0：NaN / Infinity / null / undefined 都不许画出越界的条", () => {
    expect(fillPct(Number.NaN)).toBe(0);
    expect(fillPct(Number.POSITIVE_INFINITY)).toBe(0);
    expect(fillPct(null)).toBe(0);
    expect(fillPct(undefined)).toBe(0);
  });

  it("正常值四舍五入并钳到 [0,100]", () => {
    expect(fillPct(33.4)).toBe(33);
    expect(fillPct(-5)).toBe(0);
    expect(fillPct(120)).toBe(100);
    expect(fillPct(0)).toBe(0);
  });
});

describe("tickState：心跳三格判级（阈值与列头 tooltip 同一口径）", () => {
  it("离线恒空格，age 读不到按 0 算（在线 + 无 age = 满格）", () => {
    expect(tickState(host({ online: false, age_sec: 1 }))).toEqual({
      n: 0,
      cls: ""
    });
    expect(tickState(host({ age_sec: undefined }))).toEqual({
      n: 3,
      cls: ""
    });
  });

  it("三档边界：≤30 满格、≤120 两格、≤600 一格、更久空格", () => {
    expect(tickState(host({ age_sec: 30 })).n).toBe(3);
    expect(tickState(host({ age_sec: 31 })).n).toBe(2);
    expect(tickState(host({ age_sec: 120 })).cls).toBe("kk-ticks--mid");
    expect(tickState(host({ age_sec: 121 }))).toEqual({
      n: 1,
      cls: "kk-ticks--low"
    });
    expect(tickState(host({ age_sec: 600 })).cls).toBe("kk-ticks--low");
    expect(tickState(host({ age_sec: 601 }))).toEqual({ n: 0, cls: "" });
  });
});

describe("状态判级：B6 的 updating 方点不能丢", () => {
  it("在线 = 默认实心绿点；自更新窗口 = 方点变体 + 「更新中」；其余离线 = 空心「离线」", () => {
    expect(stateClass(host())).toBe("kk-state");
    expect(stateText(host())).toBe("在线");

    expect(stateClass(host({ online: false, status_reason: "updating" }))).toBe(
      "kk-state kk-state--updating"
    );
    expect(stateText(host({ online: false, status_reason: "updating" }))).toBe(
      "更新中"
    );

    expect(stateClass(host({ online: false }))).toBe("kk-state kk-state--idle");
    expect(stateText(host({ online: false }))).toBe("离线");
    expect(stateText(host({ online: false, status_reason: "weird" }))).toBe(
      "离线"
    );
  });

  it("离线原因词表只认 updating：未知原因一律呈现为「离线」", () => {
    expect(OFFLINE_REASON_LABEL).toEqual({ updating: "更新中" });
  });
});

describe("HOST_TYPE_LABEL：v4 元信息三态，空值由调用方回落 '-'", () => {
  it("metal / vm / container 三键齐全", () => {
    expect(HOST_TYPE_LABEL).toEqual({
      metal: "物理机",
      vm: "虚拟机",
      container: "容器"
    });
  });
});
