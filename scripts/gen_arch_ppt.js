/* KontainKeeper 项目架构设计方案 PPT 生成脚本
 * 数据口径：docs/design.md / proto/messages.md（协议 v3）
 * 运行：NODE_PATH="$(npm root -g)" node scripts/gen_arch_ppt.js
 */
const pptxgen = require("pptxgenjs");

const p = new pptxgen();
p.layout = "LAYOUT_WIDE"; // 13.33 x 7.5
p.author = "KontainKeeper";
p.title = "KontainKeeper 项目架构设计方案";

// ---------- 调色板（背景 → 主色 → 强调色） ----------
const BG = "0B1121";    // 深空藏青，主导背景
const BG2 = "121D36";   // 面板
const BG3 = "1A2A4A";   // 亮一级面板 / 芯片
const LINE = "263A5E";  // 发丝线
const CYAN = "22D3EE";  // PRIMARY 科技青
const CYAND = "0E7490"; // 深青（已完成节点/次级）
const AMBER = "F5A623"; // ACCENT 琥珀，克制使用
const TEXT = "EAF1FB";
const BODY = "C9D6EA";
const MUTED = "94A9C9";
const FAINT = "5A6E92";

const CJK = "Microsoft YaHei";
const MONO = "Consolas";
const STAGES = ["业界对比", "方案优势", "技术架构", "功能扩展", "未来展望"];
const TOTAL = 13;

// ---------- 公共组件 ----------
// 贯穿时间线：active=当前阶段(0-4)，-1=全灰(封面预告)，99=全亮(结尾回收)
function timeline(s, active, y, labelFs) {
  const x0 = 0.73, dx = 2.95;
  s.addShape(p.shapes.LINE, { x: x0 - 0.25, y, w: dx * 4 + 0.5, h: 0, line: { color: LINE, width: 1 } });
  for (let i = 0; i < 5; i++) {
    const cx = x0 + i * dx;
    const cur = i === active, all = active === 99, off = active === -1;
    const nodeFill = all || cur ? CYAN : (!off && i < active ? CYAND : BG);
    const nodeLine = all || cur ? CYAN : (!off && i < active ? CYAND : FAINT);
    s.addShape(p.shapes.OVAL, { x: cx - 0.06, y: y - 0.06, w: 0.12, h: 0.12, fill: { color: nodeFill }, line: { color: nodeLine, width: 1.2 } });
    s.addText(STAGES[i], {
      x: cx - 0.7, y: y + 0.09, w: 1.4, h: 0.3, align: "center", margin: 0,
      fontSize: labelFs || 12, fontFace: CJK, bold: cur || all,
      color: all ? CYAN : cur ? CYAN : (!off && i < active ? MUTED : FAINT),
    });
  }
}

function base(stage, pageNo) {
  const s = p.addSlide();
  s.background = { color: BG };
  timeline(s, stage, 7.02, 12);
  s.addText(`${String(pageNo).padStart(2, "0")} / ${TOTAL}`, { x: 11.7, y: 0.34, w: 1.15, h: 0.3, align: "right", fontSize: 12, fontFace: MONO, color: FAINT, margin: 0 });
  return s;
}

function header(s, kicker, title) {
  s.addText(kicker, { x: 0.5, y: 0.36, w: 10, h: 0.32, fontSize: 13, fontFace: MONO, color: CYAN, charSpacing: 2, margin: 0 });
  s.addText(title, { x: 0.5, y: 0.7, w: 12.3, h: 0.64, fontSize: 29, bold: true, fontFace: CJK, color: TEXT, margin: 0 });
}

function chip(s, txt, x, y, w, o = {}) {
  s.addText(txt, {
    shape: p.shapes.ROUNDED_RECTANGLE, rectRadius: 0.05, x, y, w, h: o.h || 0.42,
    fill: { color: o.fill || BG3 }, line: { color: o.line || LINE, width: 0.75 },
    fontSize: o.fs || 12.5, fontFace: o.mono ? MONO : CJK, color: o.color || TEXT,
    align: "center", valign: "middle", margin: 0, bold: !!o.bold,
  });
}

function hairline(s, x, y, w) {
  s.addShape(p.shapes.LINE, { x, y, w, h: 0, line: { color: LINE, width: 0.75 } });
}

// 芯片宽度：CJK/全角 0.175in、ASCII 0.095in + 内边距
const ord = (ch) => ch.codePointAt(0);
function chipW(t) {
  let w = 0.36;
  for (const ch of t) w += ord(ch) > 0x2e7f ? 0.175 : 0.095;
  return w;
}

// 封面/结尾的点阵星座母题（Broker 枢纽 = 琥珀点）
function constellation(s, dx) {
  const pts = [
    [9.0, 1.4], [10.3, 2.1], [11.6, 1.5], [12.4, 2.6], [10.9, 3.2], [9.6, 2.9],
    [11.9, 4.2], [10.4, 4.6], [9.2, 4.0], [12.6, 3.6], [11.2, 5.4], [9.8, 5.2],
  ].map(([x, y]) => [x + dx, y]);
  const edges = [[0, 1], [1, 2], [1, 4], [2, 3], [3, 9], [4, 5], [4, 6], [4, 8], [5, 8], [6, 7], [6, 9], [7, 10], [7, 11], [8, 11], [10, 11]];
  edges.forEach(([a, b]) => {
    const [x1, y1] = pts[a], [x2, y2] = pts[b];
    s.addShape(p.shapes.LINE, {
      x: Math.min(x1, x2), y: Math.min(y1, y2), w: Math.abs(x2 - x1), h: Math.abs(y2 - y1),
      line: { color: "24406B", width: 0.75 },
    });
  });
  const sizes = [0.09, 0.12, 0.08, 0.1, 0.18, 0.07, 0.13, 0.09, 0.11, 0.08, 0.1, 0.07];
  pts.forEach(([x, y], i) => {
    s.addShape(p.shapes.OVAL, {
      x: x - sizes[i] / 2, y: y - sizes[i] / 2, w: sizes[i], h: sizes[i],
      fill: { color: i === 4 ? AMBER : CYAN, transparency: i === 4 ? 0 : 55 },
    });
  });
}

// ============ S1 封面 ============
(() => {
  const s = p.addSlide();
  s.background = { color: BG };
  constellation(s, 0);
  s.addText("KONTAINKEEPER · TECH REVIEW", { x: 0.55, y: 1.5, w: 8, h: 0.35, fontSize: 14, fontFace: MONO, color: CYAN, charSpacing: 4, margin: 0 });
  s.addText("项目架构设计方案", { x: 0.5, y: 1.95, w: 9.5, h: 1.1, fontSize: 52, bold: true, fontFace: CJK, color: TEXT, margin: 0 });
  s.addText([
    { text: "Linux 主机直连管理与指标提取平台", options: { breakLine: true } },
    { text: "Agent 出站 MQTT · Broker 承载可靠性 · 服务端无状态桥接", options: {} },
  ], { x: 0.55, y: 3.25, w: 7.6, h: 0.95, fontSize: 16, fontFace: CJK, color: MUTED, paraSpaceAfter: 6, margin: 0 });
  s.addText("2026-09 · 协议 v3 · 目标规模 500 台", { x: 0.55, y: 4.35, w: 7, h: 0.32, fontSize: 13, fontFace: MONO, color: FAINT, margin: 0 });
  timeline(s, -1, 6.35, 12.5);
})();

// ============ S2 目录 ============
(() => {
  const s = base(0, 2);
  header(s, "AGENDA · 演进脉络", "一条时间线，讲完五件事");
  const y = 3.97, xs = [1.55, 4.15, 6.75, 9.35, 11.95];
  s.addShape(p.shapes.LINE, { x: 0.8, y, w: 11.73, h: 0, line: { color: CYAND, width: 1.5, endArrowType: "triangle" } });
  const descs = [
    "入站拉取走不通时，出站推送是唯一解",
    "可靠性整体下沉 Broker，服务端无状态",
    "三层四主题，QoS 与 retain 是硬约束",
    "插件 / 命令 / 自更新 / 鉴权四个扩展面",
    "近中远三期候选路线，从工具到平台",
  ];
  xs.forEach((cx, i) => {
    s.addText(`0${i + 1}`, { x: cx - 0.8, y: 2.85, w: 1.6, h: 0.7, align: "center", fontSize: 36, bold: true, fontFace: MONO, color: i === 4 ? AMBER : CYAN, margin: 0 });
    s.addShape(p.shapes.OVAL, { x: cx - 0.08, y: y - 0.08, w: 0.16, h: 0.16, fill: { color: CYAN }, line: { color: CYAN, width: 1 } });
    s.addText(STAGES[i], { x: cx - 1.15, y: 4.27, w: 2.3, h: 0.4, align: "center", fontSize: 18, bold: true, fontFace: CJK, color: TEXT, margin: 0 });
    s.addText(descs[i], { x: cx - 1.15, y: 4.73, w: 2.3, h: 0.95, align: "center", fontSize: 12.5, fontFace: CJK, color: MUTED, margin: 0 });
  });
})();

// ============ S3 业界对比 · 方案矩阵 ============
(() => {
  const s = base(0, 3);
  header(s, "01 · 业界对比", "业界方案对比：入站拉取 vs 出站推送");
  const hd = (t) => ({ text: t, options: { fill: { color: BG3 }, color: CYAN, bold: true, fontSize: 12.5, fontFace: CJK, valign: "middle" } });
  const c = (t, kk) => ({ text: t, options: { fill: { color: kk ? "14324F" : BG2 }, color: kk ? TEXT : BODY, bold: !!kk, fontSize: 12, fontFace: CJK, valign: "middle" } });
  const name = (t, kk) => ({ text: t, options: { fill: { color: kk ? "14324F" : BG2 }, color: kk ? CYAN : TEXT, bold: true, fontSize: 12.5, fontFace: CJK, valign: "middle" } });
  const rows = [
    [hd("方案"), hd("通道模型"), hd("主机侧改造"), hd("服务端形态"), hd("离线能力"), hd("场景适配")],
    [name("Zabbix Agent"), c("被动检查为主 · 需入站"), c("安装系统服务"), c("Server + DB 有状态"), c("内置队列"), c("传统机房通用")],
    [name("Prometheus + Exporter"), c("HTTP 拉取 /metrics"), c("暴露抓取端口"), c("TSDB 常驻"), c("依赖抓取周期"), c("K8S 生态标配")],
    [name("Telegraf + InfluxDB"), c("插件推送 HTTP"), c("装 Agent 配输出"), c("TSDB 常驻"), c("有限缓冲"), c("指标管道自由组合")],
    [name("商业 SaaS（Datadog 类）"), c("Agent 出站上云"), c("装 Agent 联外网"), c("全托管"), c("成熟"), c("预算充足 · 快速上线")],
    [name("KontainKeeper", 1), c("出站 MQTT 单通道", 1), c("镜像内置单二进制", 1), c("无状态 · 水平扩容", 1), c("Broker 排队 + LWT", 1), c("受限内网 · 容器 IDE", 1)],
  ];
  s.addTable(rows, {
    x: 0.5, y: 1.72, w: 12.33, colW: [1.95, 2.1, 2.0, 2.05, 2.15, 2.08],
    rowH: [0.42, 0.6, 0.6, 0.6, 0.6, 0.64],
    border: { pt: 0.5, color: LINE }, margin: 0.08, align: "left",
  });
  s.addShape(p.shapes.RECTANGLE, { x: 0, y: 5.9, w: 13.34, h: 0.72, fill: { color: BG2 }, line: { color: LINE, width: 0.5 } });
  s.addText([
    { text: "硬约束：不动宿主机 · 不用 K8S 入站 · 用户无感知  ——  ", options: { color: TEXT } },
    { text: "唯一可行路径：主机主动连出去", options: { color: AMBER, bold: true } },
  ], { x: 0.9, y: 5.9, w: 11.6, h: 0.72, fontSize: 15, fontFace: CJK, valign: "middle", margin: 0 });
})();

// ============ S4 业界对比 · 协议演进时间线 ============
(() => {
  const s = base(0, 4);
  header(s, "01 · 业界对比 · 演进", "从自研到开源：协议三次演进");
  const y = 3.05, xs = [2.3, 6.65, 11.0];
  s.addShape(p.shapes.LINE, { x: 0.9, y, w: 11.5, h: 0, line: { color: CYAND, width: 1.5, endArrowType: "triangle" } });
  const cards = [
    {
      v: "v1 · 自研 WebSocket", cur: false, head: "自研全栈",
      lines: ["内存连接表 + 手写补发 + 超时判定 ≈ 200 行，多处缺陷", "手工解析 /proc 233 行，仅覆盖 Linux", "自研 RFC6455 WebSocket 261 行"],
    },
    {
      v: "v2 · MQTT + token", cur: false, head: "可靠性移交 Broker",
      lines: ["引入主题布局 + QoS 分级", "重连 / 离线排队 / LWT 全部下沉 Mosquitto", "保留 token 接入认证"],
    },
    {
      v: "v3 · 匿名 + 白名单（现行）", cur: true, head: "零凭据 + 白名单",
      lines: ["Broker 匿名开放，镜像只烧 KK_SERVER 等非敏感配置", "KK_AGENT_IPS 白名单管控接入，帧自报 ip", "可选加固：password_file + ACL 鉴权"],
    },
  ];
  xs.forEach((cx, i) => {
    const cd = cards[i];
    chip(s, cd.v, cx - 1.35, 2.32, 2.7, { line: cd.cur ? CYAN : LINE, color: cd.cur ? CYAN : MUTED, bold: cd.cur, h: 0.44 });
    s.addShape(p.shapes.OVAL, { x: cx - 0.08, y: y - 0.08, w: 0.16, h: 0.16, fill: { color: cd.cur ? CYAN : BG }, line: { color: cd.cur ? CYAN : FAINT, width: 1.2 } });
    s.addShape(p.shapes.RECTANGLE, { x: cx - 1.9, y: 3.5, w: 3.8, h: 2.4, fill: { color: cd.cur ? BG3 : BG2 }, line: { color: cd.cur ? CYAND : LINE, width: 0.75 } });
    s.addText(cd.head, { x: cx - 1.65, y: 3.66, w: 3.3, h: 0.36, fontSize: 15, bold: true, fontFace: CJK, color: cd.cur ? CYAN : TEXT, margin: 0 });
    const bu = () => ({ code: "2013", indent: 8 });
    s.addText(cd.lines.map((t, j) => ({ text: t, options: { bullet: bu(), breakLine: j < cd.lines.length - 1 } })), {
      x: cx - 1.65, y: 4.1, w: 3.4, h: 1.7, fontSize: 12, fontFace: CJK, color: MUTED, paraSpaceAfter: 6, margin: 0, valign: "top",
    });
  });
  s.addText([
    { text: "迁移账本：", options: { color: TEXT, bold: true } },
    { text: "494 行", options: { color: AMBER, bold: true } },
    { text: " 自研传输/采集代码退场（233 行 /proc 解析 + 261 行自研 WS），新增部署单元仅 1 个 Mosquitto（EPL/EDL）", options: { color: MUTED } },
  ], { x: 0.9, y: 6.25, w: 11.6, h: 0.4, fontSize: 13, fontFace: CJK, margin: 0 });
})();

// ============ S5 方案优势 ============
(() => {
  const s = base(1, 5);
  header(s, "02 · 方案优势", "方案优势：把可靠性难题整体外包");
  const stats = [
    { n: "0", f: 40, l: "入站端口暴露", d: "唯一通道是出站 MQTT，主机零端口暴露" },
    { n: "25–35MB", f: 40, l: "Agent 常驻 RSS", d: "空闲 CPU < 0.1%，对业务进程无感" },
    { n: "≈200 行 → 0", f: 30, l: "自研连接管理代码", d: "随 MQTT 迁移整体清零" },
  ];
  stats.forEach((st, i) => {
    const yy = 1.85 + i * 1.62;
    s.addText(st.n, { x: 0.5, y: yy, w: 4.0, h: 0.72, fontSize: st.f, bold: true, fontFace: MONO, color: CYAN, margin: 0 });
    s.addText(st.l, { x: 0.5, y: yy + 0.74, w: 4.0, h: 0.32, fontSize: 13.5, bold: true, fontFace: CJK, color: TEXT, margin: 0 });
    s.addText(st.d, { x: 0.5, y: yy + 1.06, w: 4.0, h: 0.32, fontSize: 12, fontFace: CJK, color: MUTED, margin: 0 });
  });
  const rows = [
    ["服务端无状态，水平扩容", "连接 / 重连 / 离线排队 / 在线判定整体由 Broker 承担；多实例仅需 client_id 唯一"],
    ["用户零感知接入", "镜像制作期 entrypoint 透传原启动命令，崩溃 5s 自动拉起；容器用户完全不改变习惯"],
    ["三库通用零迁移", "SQLite / PostgreSQL / MySQL 一套代码；方言差异收敛在 _upsert / _ensure_schema 两处"],
    ["安全面收敛可审计", "命令黑名单源头拦截 + 全量审计；IP 白名单接入管控；自更新 sha256/HMAC 校验"],
  ];
  rows.forEach((r, i) => {
    const yy = 1.85 + i * 1.2;
    s.addText(`${i + 1}`, {
      shape: p.shapes.ROUNDED_RECTANGLE, rectRadius: 0.04, x: 4.95, y: yy, w: 0.44, h: 0.44,
      fill: { color: BG3 }, line: { color: CYAND, width: 0.75 }, fontSize: 15, bold: true, fontFace: MONO, color: CYAN, align: "center", valign: "middle", margin: 0,
    });
    s.addText(r[0], { x: 5.6, y: yy - 0.02, w: 7.2, h: 0.4, fontSize: 16.5, bold: true, fontFace: CJK, color: TEXT, margin: 0 });
    s.addText(r[1], { x: 5.6, y: yy + 0.4, w: 7.2, h: 0.6, fontSize: 12.5, fontFace: CJK, color: MUTED, margin: 0 });
    if (i < 3) hairline(s, 4.95, yy + 1.06, 7.85);
  });
})();

// ============ S6 数字看方案 ============
(() => {
  const s = base(1, 6);
  header(s, "02 · 方案优势 · 容量", "数字看方案：500 台规模下的余量");
  s.addText("8.3", { x: 0.5, y: 2.0, w: 4.6, h: 1.5, fontSize: 88, bold: true, fontFace: MONO, color: CYAN, margin: 0 });
  s.addText("msg/s · 心跳流量", { x: 0.55, y: 3.55, w: 4.4, h: 0.4, fontSize: 18, bold: true, fontFace: CJK, color: TEXT, margin: 0 });
  s.addText("500 台 × 60s 心跳间隔（±10% 抖动打散峰值）", { x: 0.55, y: 4.02, w: 4.5, h: 0.35, fontSize: 12.5, fontFace: CJK, color: MUTED, margin: 0 });
  s.addText("每帧 2–4KB · Mosquitto 万级 msg/s 吞吐，余量 > 100×", { x: 0.55, y: 4.4, w: 4.5, h: 0.6, fontSize: 12.5, fontFace: CJK, color: MUTED, margin: 0 });
  const cells = [
    ["8–12MB", "单文件二进制", "PyInstaller 打包，含 psutil + paho 增量 ≈ 3MB"],
    ["< 0.1%", "空闲 CPU", "60s 一帧默认节奏，目标主机无感"],
    ["几百 MB/年", "存储成本", "心跳 2 天聚合 + hourly 90 天 + 命令 30 天"],
    ["4MB / 48KB", "输出上限 / 分块", "超限截断置 truncated，rc=-3 终态不静默丢"],
  ];
  cells.forEach((cd, i) => {
    const x = 5.6 + (i % 2) * 3.7, y = 1.95 + Math.floor(i / 2) * 1.95;
    s.addShape(p.shapes.RECTANGLE, { x, y, w: 3.45, h: 1.7, fill: { color: BG2 }, line: { color: LINE, width: 0.75 } });
    s.addText(cd[0], { x: x + 0.22, y: y + 0.18, w: 3.0, h: 0.55, fontSize: 28, bold: true, fontFace: i === 2 ? CJK : MONO, color: TEXT, margin: 0 });
    s.addText(cd[1], { x: x + 0.22, y: y + 0.78, w: 3.0, h: 0.3, fontSize: 13, bold: true, fontFace: CJK, color: CYAN, margin: 0 });
    s.addText(cd[2], { x: x + 0.22, y: y + 1.1, w: 3.05, h: 0.52, fontSize: 11.5, fontFace: CJK, color: MUTED, margin: 0 });
  });
  s.addText("口径：docs/design.md §6 容量估算，规模目标 500 台。", { x: 0.55, y: 6.15, w: 8, h: 0.3, fontSize: 12, fontFace: CJK, color: FAINT, margin: 0 });
})();

// ============ S7 技术架构总览 ============
(() => {
  const s = base(2, 7);
  header(s, "03 · 技术架构", "技术架构总览：三层四主题一条通道");
  const band = (y, h, title) => {
    s.addShape(p.shapes.ROUNDED_RECTANGLE, { x: 0.6, y, w: 12.13, h, rectRadius: 0.06, fill: { color: BG2 }, line: { color: LINE, width: 1 } });
    s.addText(title, { x: 1.0, y: y + 0.1, w: 11.2, h: 0.34, fontSize: 15, bold: true, fontFace: CJK, color: CYAN, margin: 0 });
  };
  // 主机层
  band(1.7, 1.22, "主机内 Agent — kk-agent 单二进制（8–12MB）");
  ["psutil 采集 × 8 项", "命令执行器 · argv 直传", "插件热加载 · mtime", "自更新 · sha256 + execv"].reduce((x, t) => {
    const w = chipW(t);
    chip(s, t, x, 2.3, w, { h: 0.44 }); return x + w + 0.24;
  }, 1.0);
  // 通道一
  s.addShape(p.shapes.LINE, { x: 1.35, y: 2.96, w: 0, h: 0.46, line: { color: AMBER, width: 2.5, endArrowType: "triangle" } });
  s.addText("出站 MQTT : 1883 — 主机零端口暴露 · hb QoS0 / result·cmd QoS1（唯一通道）", { x: 1.85, y: 2.96, w: 10.5, h: 0.46, fontSize: 13, fontFace: CJK, color: AMBER, valign: "middle", margin: 0 });
  // Broker 层
  band(3.46, 1.1, "Mosquitto 2.x — MQTT Broker（可靠性中枢）");
  ["LWT 遗嘱", "retained status", "持久会话 + QoS1 离线排队", "匿名开放 · 可选 password_file + ACL"].reduce((x, t) => {
    const w = chipW(t);
    chip(s, t, x, 3.98, w, { h: 0.44 }); return x + w + 0.24;
  }, 1.0);
  // 通道二
  s.addShape(p.shapes.LINE, { x: 1.35, y: 4.6, w: 0, h: 0.46, line: { color: CYAN, width: 2.5, endArrowType: "triangle" } });
  s.addText("订阅 hb / status / result · 发布 cmd — 无长连接状态，服务端重启即恢复", { x: 1.85, y: 4.6, w: 10.5, h: 0.46, fontSize: 13, fontFace: CJK, color: MUTED, valign: "middle", margin: 0 });
  // 服务端层
  band(5.1, 1.48, "服务端 kk-server — FastAPI · 无状态桥接");
  ["MqttBridge 无状态桥接", "REST /api/* 会话鉴权", "Store · SQLAlchemy 2 async", "Vue3 构建产物单端口托管"].reduce((x, t) => {
    const w = chipW(t);
    chip(s, t, x, 5.56, w, { h: 0.44 }); return x + w + 0.24;
  }, 1.0);
  s.addText("三库 KK_DB_URL 三选一：SQLite / PostgreSQL / MySQL，驱动按需安装", { x: 1.0, y: 6.12, w: 11, h: 0.32, fontSize: 12, fontFace: CJK, color: MUTED, margin: 0 });
})();

// ============ S8 主题与 QoS 语义 ============
(() => {
  const s = base(2, 8);
  header(s, "03 · 技术架构 · 协议", "MQTT 主题布局：QoS 与 retain 是硬约束");
  const lanes = [
    { t: "kk/v1/{host}/status", qos: "QoS 1", ret: "retain 是", hot: true, d: "LWT 遗嘱兼用；服务端重启靠 retained 立刻恢复全量在线视图" },
    { t: "kk/v1/{host}/hb", qos: "QoS 0", ret: "retain 否", hot: false, d: "指标真相在数据库；retain 回放会让重启灌入幽灵心跳" },
    { t: "kk/v1/{host}/result", qos: "QoS 1", ret: "retain 否", hot: false, d: "结果必达；48KB 分块，任一分块失败补发 rc=-3 终态" },
    { t: "kk/v1/{host}/cmd", qos: "QoS 1", ret: "retain 否", hot: true, d: "离线由持久会话排队，重连自动补投；服务端不做补发", rev: true },
  ];
  const boxY = 1.74, boxH = 4.0;
  lanes.forEach((ln, i) => {
    const lt = 1.78 + i * 1.0, ly = lt + 0.86;
    const seg = (x, w, rev) => s.addShape(p.shapes.LINE, {
      x, y: ly, w, h: 0, flipH: !!rev,
      line: { color: "31486E", width: 1.5, endArrowType: "triangle" },
    });
    seg(2.28, 3.54, ln.rev);
    seg(7.48, 3.54, ln.rev);
    s.addText(ln.t, { x: 2.42, y: lt + 0.04, w: 2.7, h: 0.32, fontSize: 14, bold: true, fontFace: MONO, color: ln.hot ? CYAN : TEXT, margin: 0 });
    chip(s, ln.qos, 2.42, lt + 0.42, 0.72, { h: 0.3, fs: 11.5, mono: true, color: CYAN, line: CYAND });
    chip(s, ln.ret, 3.24, lt + 0.42, 1.02, { h: 0.3, fs: 11.5, color: ln.ret.endsWith("是") ? AMBER : MUTED, line: ln.ret.endsWith("是") ? AMBER : LINE });
    s.addText(ln.d, { x: 7.62, y: lt + 0.08, w: 3.32, h: 0.72, fontSize: 12, fontFace: CJK, color: MUTED, margin: 0 });
  });
  s.addShape(p.shapes.RECTANGLE, { x: 0.5, y: boxY, w: 1.7, h: boxH, fill: { color: BG2 }, line: { color: LINE, width: 1 } });
  s.addText([{ text: "Agent", options: { breakLine: true, fontSize: 17, bold: true, color: TEXT } }, { text: "kk-agent", options: { fontSize: 11.5, color: MUTED } }], { x: 0.5, y: boxY, w: 1.7, h: boxH, align: "center", valign: "middle", fontFace: CJK, margin: 0 });
  s.addShape(p.shapes.RECTANGLE, { x: 5.9, y: boxY, w: 1.5, h: boxH, fill: { color: BG3 }, line: { color: CYAN, width: 1.2 } });
  s.addText([{ text: "Mosquitto", options: { breakLine: true, fontSize: 15, bold: true, color: CYAN } }, { text: "2.x Broker", options: { fontSize: 11.5, color: MUTED } }], { x: 5.9, y: boxY, w: 1.5, h: boxH, align: "center", valign: "middle", fontFace: CJK, margin: 0 });
  s.addShape(p.shapes.RECTANGLE, { x: 11.1, y: boxY, w: 1.7, h: boxH, fill: { color: BG2 }, line: { color: LINE, width: 1 } });
  s.addText([{ text: "kk-server", options: { breakLine: true, fontSize: 16, bold: true, color: TEXT } }, { text: "MqttBridge", options: { fontSize: 11.5, color: MUTED } }], { x: 11.1, y: boxY, w: 1.7, h: boxH, align: "center", valign: "middle", fontFace: CJK, margin: 0 });
  // 两条红线语义
  const notes = [
    "hb 绝不 retain —— retained 心跳随服务端建订阅整批回放，按帧内 ts 落库即成「刚刚上报」的幽灵点",
    "QoS1 结果不做在线预检 —— publish() 本身入 out-queue 重连重发，预检等于自己短路离线排队",
  ];
  notes.forEach((t, i) => {
    const x = 0.5 + i * 6.42;
    s.addShape(p.shapes.RECTANGLE, { x, y: 5.92, w: 5.9, h: 0.82, fill: { color: BG2 }, line: { color: LINE, width: 0.5 } });
    s.addText([{ text: `0${i + 1}  `, options: { color: AMBER, bold: true, fontFace: MONO } }, { text: t, options: { color: BODY } }], { x: x + 0.18, y: 5.92, w: 5.6, h: 0.82, fontSize: 12, fontFace: CJK, valign: "middle", margin: 0 });
  });
})();

// ============ S9 服务端工程化 ============
(() => {
  const s = base(2, 9);
  header(s, "03 · 技术架构 · 工程化", "服务端工程：无状态 MVC 与方言收敛");
  const layers = [
    ["controllers — REST /api/*", "主机 / 命令 / 审计 / 自更新 / stats · 会话鉴权 12h"],
    ["services — MqttBridge · security", "无状态桥接四主题 · 命令黑名单源头拦截"],
    ["models — SQLAlchemy 2 Core async", "Store 心跳/命令/审计/聚合回收 · 三库方言收敛"],
    ["web — Vue3 构建产物", "六个业务页 · REST 轮询 10s / 30s / 5s · 单端口托管"],
  ];
  layers.forEach((ly, i) => {
    const y = 1.78 + i * 1.08;
    s.addShape(p.shapes.RECTANGLE, { x: 0.5, y, w: 6.3, h: 0.94, fill: { color: BG2 }, line: { color: i === 1 ? CYAND : LINE, width: i === 1 ? 1.2 : 0.75 } });
    s.addText(ly[0], { x: 0.78, y: y + 0.12, w: 5.8, h: 0.34, fontSize: 14, bold: true, fontFace: MONO, color: i === 1 ? CYAN : TEXT, margin: 0 });
    s.addText(ly[1], { x: 0.78, y: y + 0.5, w: 5.8, h: 0.32, fontSize: 12, fontFace: CJK, color: MUTED, margin: 0 });
  });
  s.addText("工程纪律", { x: 7.3, y: 1.78, w: 5.4, h: 0.36, fontSize: 15, bold: true, fontFace: CJK, color: TEXT, margin: 0 });
  const rules = [
    ["方言差异只收两处", "LONGTEXT 防静默截断 · 定长主键 · INSERT IGNORE upsert"],
    ["自动补列零迁移", "create_all 只建表；新增列登记 _ADD_COLUMNS 自动 ALTER"],
    ["分批删按主键 IN", "PG 不支持 DELETE…LIMIT，跨方言统一主键批删防锁表"],
    ["日志单一后端 loguru", "新代码禁 import logging（静态用例锁住）· diagnose=False"],
  ];
  rules.forEach((r, i) => {
    const y = 2.26 + i * 0.98;
    s.addShape(p.shapes.RECTANGLE, { x: 7.3, y: y + 0.06, w: 0.09, h: 0.09, fill: { color: CYAN } });
    s.addText(r[0], { x: 7.55, y: y - 0.05, w: 5.2, h: 0.32, fontSize: 14, bold: true, fontFace: CJK, color: TEXT, margin: 0 });
    s.addText(r[1], { x: 7.55, y: y + 0.29, w: 5.25, h: 0.55, fontSize: 12, fontFace: CJK, color: MUTED, margin: 0 });
  });
  s.addShape(p.shapes.RECTANGLE, { x: 0, y: 6.28, w: 13.34, h: 0.56, fill: { color: BG2 }, line: { color: LINE, width: 0.5 } });
  s.addText([
    { text: "CI/CD 流水线（Jenkinsfile）：", options: { color: TEXT, bold: true } },
    { text: "290+ 测试用例", options: { color: AMBER, bold: true } },
    { text: " 把关 — 测试 → Agent 二进制 → 服务端镜像 → 冒烟 → 推送 → 部署 → 部署验证", options: { color: MUTED } },
  ], { x: 0.9, y: 6.28, w: 11.8, h: 0.56, fontSize: 13, fontFace: CJK, valign: "middle", margin: 0 });
})();

// ============ S10 功能扩展 ============
(() => {
  const s = base(3, 10);
  header(s, "04 · 功能扩展", "功能扩展：四个已打通的扩展面");
  const tx = 6.62;
  s.addShape(p.shapes.LINE, { x: tx, y: 1.95, w: 0, h: 4.5, line: { color: LINE, width: 1.5 } });
  const items = [
    {
      n: "01", side: "L", head: "自定义采集插件",
      d: "plugins/ 任意 *.py 按 mtime 热加载；输出并入心跳 custom 字段；单插件故障 5s 超时隔离，只跳过自身",
    },
    {
      n: "02", side: "R", head: "命令协议四类 kind",
      d: "shell / collect / plugin_reload / update；use_shell 支持管道重定向，黑名单 + 全量审计兜底",
    },
    {
      n: "03", side: "L", head: "Agent 自更新闭环",
      d: "上传 → 清单 → 下载 → sha256/HMAC 校验 → execv 换进程；reason=updating 等 PUBACK，120s 宽限防抹白",
    },
    {
      n: "04", side: "R", head: "精简与按需装配",
      d: "KK_HB_ITEMS 裁剪采集项（agent_ver 不受影响）；--extra 按需装库驱动；Broker 鉴权可选加固",
    },
  ];
  items.forEach((it, i) => {
    const ny = 2.3 + i * 1.3;
    s.addShape(p.shapes.OVAL, { x: tx - 0.07, y: ny - 0.07, w: 0.14, h: 0.14, fill: { color: CYAN }, line: { color: CYAN, width: 1 } });
    const left = it.side === "L";
    const cx = left ? 0.5 : 7.35;
    s.addShape(p.shapes.LINE, { x: left ? 6.0 : tx + 0.07, y: ny, w: left ? 0.55 : 0.73, h: 0, line: { color: LINE, width: 1 } });
    s.addShape(p.shapes.RECTANGLE, { x: cx, y: ny - 0.52, w: 5.5, h: 1.08, fill: { color: BG2 }, line: { color: LINE, width: 0.75 } });
    s.addText([
      { text: `${it.n} · `, options: { color: CYAN, bold: true, fontFace: MONO, fontSize: 14 } },
      { text: it.head, options: { color: TEXT, bold: true, fontSize: 14.5 } },
    ], { x: cx + 0.24, y: ny - 0.4, w: 5.0, h: 0.34, fontFace: CJK, margin: 0 });
    s.addText(it.d, { x: cx + 0.24, y: ny - 0.03, w: 5.05, h: 0.52, fontSize: 12, fontFace: CJK, color: MUTED, margin: 0 });
  });
})();

// ============ S11 未来展望 ============
(() => {
  const s = base(4, 11);
  header(s, "05 · 未来展望", "未来展望：三期演进路线（候选）");
  const y = 2.75, xs = [2.9, 6.55, 10.2];
  s.addShape(p.shapes.LINE, { x: 0.8, y, w: 11.7, h: 0, line: { color: CYAND, width: 1.5, endArrowType: "triangle" } });
  s.addText("演进方向", { x: 11.55, y: y - 0.42, w: 1.3, h: 0.3, fontSize: 12, fontFace: CJK, color: FAINT, margin: 0 });
  const phases = [
    {
      c: "近期 · 夯实底座", hot: true,
      items: ["PG / MySQL 真库验证与上线", "告警规则引擎：阈值 → 通知", "Broker 持久化 + 监控与高可用"],
    },
    {
      c: "中期 · 体验升级", hot: false,
      items: ["服务端 → 前端实时推送（SSE / WS）替代轮询", "多 Broker 分组联邦，启用共享订阅扩容", "OpenTelemetry 指标导出对接"],
    },
    {
      c: "远期 · 平台化", hot: false,
      items: ["插件分发与配置中心", "多租户与 RBAC 权限模型", "边缘自治：策略下发 + 本地决策"],
    },
  ];
  xs.forEach((cx, i) => {
    const ph = phases[i];
    chip(s, ph.c, cx - 1.2, 1.95, 2.4, { h: 0.46, line: ph.hot ? AMBER : CYAN, color: ph.hot ? AMBER : CYAN, bold: true, fs: 13.5 });
    s.addShape(p.shapes.OVAL, { x: cx - 0.08, y: y - 0.08, w: 0.16, h: 0.16, fill: { color: ph.hot ? AMBER : CYAN }, line: { color: ph.hot ? AMBER : CYAN, width: 1 } });
    ph.items.forEach((t, j) => {
      const iy = 3.25 + j * 0.82;
      s.addShape(p.shapes.RECTANGLE, { x: cx - 1.8, y: iy + 0.07, w: 0.08, h: 0.08, fill: { color: ph.hot ? AMBER : CYAND } });
      s.addText(t, { x: cx - 1.58, y: iy - 0.04, w: 3.4, h: 0.72, fontSize: 12.5, fontFace: CJK, color: BODY, margin: 0 });
    });
  });
  s.addText("推演口径：基于 design.md §8 风险清单与 proto §6 已登记未实现项外推，为候选方向而非版本承诺。", { x: 0.55, y: 6.0, w: 11, h: 0.3, fontSize: 12, fontFace: CJK, color: FAINT, margin: 0 });
})();

// ============ S12 总结 ============
(() => {
  const s = base(5, 12);
  header(s, "SUMMARY", "总结：三个设计决策撑起整个方案");
  const rows = [
    ["01", "出站推送是唯一解", "不能动宿主机、不能用 K8S 入站、用户无感知；主机主动连出去，零端口暴露换最大兼容"],
    ["02", "可靠性整体外包", "重连 / 排队 / LWT 交给 Mosquitto，≈200 行自研缺陷代码清零，服务端无状态可水平扩容"],
    ["03", "复杂度收敛到少数函数", "单二进制 · 三库方言收敛两处 · 单一日志后端；改协议四处同步（双端 PROTO_VER + 文档 + 测试）"],
  ];
  rows.forEach((r, i) => {
    const y = 1.9 + i * 1.32;
    s.addText(r[0], { x: 0.6, y: y + 0.05, w: 1.0, h: 0.6, fontSize: 26, bold: true, fontFace: MONO, color: CYAN, margin: 0 });
    s.addText(r[1], { x: 1.8, y, w: 10.9, h: 0.42, fontSize: 19, bold: true, fontFace: CJK, color: TEXT, margin: 0 });
    s.addText(r[2], { x: 1.8, y: y + 0.46, w: 10.9, h: 0.36, fontSize: 13, fontFace: CJK, color: MUTED, margin: 0 });
    if (i < 2) hairline(s, 0.6, y + 1.12, 12.1);
  });
  s.addText("Agent 只管采集与执行 · Broker 只管可靠传输 · Server 只管路由与落库", { x: 0.5, y: 6.0, w: 12.33, h: 0.5, align: "center", fontSize: 16.5, bold: true, fontFace: CJK, color: AMBER, margin: 0 });
})();

// ============ S13 结束页 ============
(() => {
  const s = p.addSlide();
  s.background = { color: BG };
  constellation(s, -8.2);
  s.addText("谢谢观看", { x: 3.67, y: 2.15, w: 6, h: 1.0, align: "center", fontSize: 46, bold: true, fontFace: CJK, color: TEXT, margin: 0 });
  s.addText("KontainKeeper · 项目架构设计方案", { x: 3.67, y: 3.3, w: 6, h: 0.4, align: "center", fontSize: 15, fontFace: CJK, color: MUTED, margin: 0 });
  s.addText("2026-09 · 协议 v3 · 目标规模 500 台", { x: 3.67, y: 3.75, w: 6, h: 0.32, align: "center", fontSize: 12.5, fontFace: MONO, color: FAINT, margin: 0 });
  timeline(s, 99, 5.5, 13);
})();

p.writeFile({ fileName: "KontainKeeper-项目架构设计方案.pptx" }).then(() => console.log("OK"));
