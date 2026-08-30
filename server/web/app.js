/* KontainKeeper 管理界面：无框架单页应用，hash 路由 + 轮询 */
"use strict";

const $ = (sel, el) => (el || document).querySelector(sel);
const app = $("#app");
let TOKEN = localStorage.getItem("kk_token") || "";
let USER = "";
let pollTimers = [];

function setPoll(fn, ms) { pollTimers.push(setInterval(fn, ms)); }
function clearPolls() { pollTimers.forEach(clearInterval); pollTimers = []; }

/* ---------- 基础工具 ---------- */
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function toast(msg, isErr) {
  const el = document.createElement("div");
  el.className = "toast" + (isErr ? " err" : "");
  el.textContent = msg;
  $("#toast").appendChild(el);
  setTimeout(() => el.remove(), 3500);
}
function fmtTs(ts) {
  if (!ts) return "-";
  return new Date(ts * 1000).toLocaleString("zh-CN", { hour12: false });
}
function fmtAgo(sec) {
  if (sec == null) return "-";
  if (sec < 60) return sec + " 秒前";
  if (sec < 3600) return Math.floor(sec / 60) + " 分钟前";
  return Math.floor(sec / 3600) + " 小时前";
}
async function api(path, opts = {}) {
  const headers = { "Content-Type": "application/json", ...(opts.headers || {}) };
  if (TOKEN) headers["Authorization"] = "Bearer " + TOKEN;
  const res = await fetch(path, { ...opts, headers });
  if (res.status === 401) { showLogin(); throw new Error("未登录或会话过期"); }
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.detail || res.status);
  return body;
}
function sparkline(canvas, values, color) {
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth || 300, h = 64;
  canvas.width = w * dpr; canvas.height = h * dpr;
  const ctx = canvas.getContext("2d");
  ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, w, h);
  if (!values || values.length < 2) {
    ctx.fillStyle = "#8b949e"; ctx.font = "12px sans-serif";
    ctx.fillText("暂无数据", 8, h / 2);
    return;
  }
  const max = Math.max(...values, 0.001);
  const pt = i => [i / (values.length - 1) * (w - 8) + 4, h - 6 - (values[i] / max) * (h - 14)];
  ctx.beginPath();
  values.forEach((v, i) => { const [x, y] = pt(i); i ? ctx.lineTo(x, y) : ctx.moveTo(x, y); });
  ctx.strokeStyle = color; ctx.lineWidth = 1.6; ctx.stroke();
  ctx.lineTo(w - 4, h - 2); ctx.lineTo(4, h - 2); ctx.closePath();
  ctx.fillStyle = color + "22"; ctx.fill();
}
function barHtml(pct) {
  pct = Math.min(100, pct || 0);
  const cls = pct >= 90 ? "crit" : pct >= 75 ? "warn" : "";
  return `<div class="bar"><i class="${cls}" style="width:${pct}%"></i></div>`;
}
function statusChip(s) { return `<span class="chip ${esc(s)}">${esc(s)}</span>`; }

/* ---------- 路由 ---------- */
function nav() {
  clearPolls();
  const h = location.hash || "#/containers";
  if (!TOKEN) return showLogin();
  if (h.startsWith("#/container/")) return viewContainer(decodeURIComponent(h.slice(12)));
  if (h === "#/console") return viewConsole();
  if (h === "#/audit") return viewAudit();
  return viewContainers();
}
window.addEventListener("hashchange", nav);

function layout(active, bodyHtml) {
  const link = (id, label, href) =>
    `<a href="${href}" class="${active === id ? "active" : ""}">${label}</a>`;
  app.innerHTML = `
    <div class="topbar">
      <div class="brand">Konta<span>in</span>Keeper</div>
      <div class="nav">${link("c", "容器", "#/containers")}${link("x", "命令控制台", "#/console")}${link("a", "审计日志", "#/audit")}</div>
      <div class="userbox">${esc(USER)} <a id="logout">退出</a></div>
    </div>
    <div class="content">${bodyHtml}</div>`;
  $("#logout").onclick = async () => {
    try { await api("/api/logout", { method: "POST" }); } catch (e) {}
    TOKEN = ""; USER = ""; localStorage.removeItem("kk_token");
    showLogin();
  };
}

/* ---------- 登录 ---------- */
function showLogin() {
  clearPolls();
  app.innerHTML = `
    <div class="login-wrap"><div class="login">
      <h1>KontainKeeper 控制台</h1>
      <p>容器直连管理平台 · 管理员登录</p>
      <input id="u" placeholder="用户名" autocomplete="username">
      <input id="p" type="password" placeholder="密码" autocomplete="current-password">
      <button id="go">登 录</button>
    </div></div>`;
  const go = async () => {
    try {
      const r = await api("/api/login", {
        method: "POST",
        body: JSON.stringify({ username: $("#u").value.trim(), password: $("#p").value }),
      });
      TOKEN = r.token; USER = r.username;
      localStorage.setItem("kk_token", TOKEN);
      location.hash = "#/containers"; nav();
    } catch (e) { toast("登录失败：" + e.message, true); }
  };
  $("#go").onclick = go;
  $("#p").addEventListener("keydown", e => e.key === "Enter" && go());
  $("#u").focus();
}

/* ---------- 容器总览 ---------- */
async function viewContainers() {
  layout("c", `<div class="stats" id="stats"></div>
    <div class="card">
      <div class="row"><input type="text" id="q" placeholder="搜索 Pod / 镜像…">
        <span class="muted" id="hint"></span></div>
      <div style="overflow-x:auto"><table>
        <thead><tr><th>状态</th><th>Pod</th><th>镜像 / Agent</th><th class="num">CPU</th>
          <th class="num">内存</th><th>磁盘（最大使用率）</th><th>自定义采集</th><th>最后心跳</th></tr></thead>
        <tbody id="rows"></tbody>
      </table></div>
    </div>`);
  $("#q").addEventListener("input", () => renderRows(viewContainers._data));

  async function tick() {
    try {
      const r = await api("/api/containers");
      viewContainers._data = r;
      renderRows(r);
    } catch (e) { toast(e.message, true); clearPolls(); }
  }
  function renderRows(r) {
    if (!r) return;
    $("#stats").innerHTML = `
      <div class="stat"><div class="lbl">容器总数</div><div class="num">${r.total}</div></div>
      <div class="stat green"><div class="lbl">在线</div><div class="num">${r.online}</div></div>
      <div class="stat ${r.alerts ? "red" : ""}"><div class="lbl">磁盘告警</div><div class="num">${r.alerts}</div></div>`;
    const q = ($("#q").value || "").toLowerCase();
    const items = r.items.filter(i => !q || i.pod.toLowerCase().includes(q) || (i.image || "").toLowerCase().includes(q));
    $("#hint").textContent = `${items.length} / ${r.total}`;
    $("#rows").innerHTML = items.map(i => {
      const m = i.metrics || {};
      const disks = Object.entries(m.disks || {});
      const worst = Math.max(0, ...disks.map(([p, d]) => d.pct || 0));
      const worstPath = (disks.find(([p, d]) => (d.pct || 0) === worst) || ["-"])[0];
      const custom = Object.keys(i.custom || {});
      return `<tr class="clickable" data-pod="${esc(i.pod)}">
        <td><span class="dot ${i.online ? "on" : "off"}"></span>${i.online ? "在线" : "离线"}</td>
        <td class="mono">${esc(i.pod)}</td>
        <td class="muted nowrap">${esc(i.image || "-")}<br><span class="mono" style="font-size:11px">agent ${esc(i.agent_ver || "?")}</span></td>
        <td class="num">${m.cpu != null ? m.cpu + "%" : "-"}</td>
        <td class="num">${m.mem_mb != null ? m.mem_mb + " / " + (m.mem_total_mb || "?") + " MB" : "-"}</td>
        <td>${i.disk_alert ? '<span class="badge-red">' + worst + "%</span> " : ""}${barHtml(worst)}<div class="muted" style="font-size:11px">${esc(worstPath)}</div></td>
        <td>${custom.length ? esc(custom.join(", ")) : '<span class="muted">-</span>'}</td>
        <td class="muted nowrap">${fmtTs(i.last_seen)}<br>${fmtAgo(i.age_sec)}</td>
      </tr>`;
    }).join("") || `<tr><td colspan="8" class="empty">暂无容器上报，请确认 Agent 已随镜像部署</td></tr>`;
    $("#rows").querySelectorAll("tr[data-pod]").forEach(tr =>
      tr.onclick = () => { location.hash = "#/container/" + encodeURIComponent(tr.dataset.pod); });
  }
  await tick();
  setPoll(tick, 5000);
}

/* ---------- 容器详情 ---------- */
async function viewContainer(pod) {
  layout("c", `<div class="row"><a href="#/containers">← 返回</a><b class="mono" style="font-size:16px">${esc(pod)}</b></div>
    <div id="detail"><div class="empty">加载中…</div></div>`);
  async function tick() {
    let d;
    try { d = await api("/api/containers/" + encodeURIComponent(pod)); }
    catch (e) { $("#detail").innerHTML = `<div class="card">${esc(e.message)}</div>`; clearPolls(); return; }
    let series = viewContainer._series;
    try {
      const s = await api(`/api/containers/${encodeURIComponent(pod)}/metrics?hours=24`);
      series = s.series || [];
    } catch (e) {}
    const m = d.metrics || {};
    const disks = Object.entries(m.disks || {});
    const users = m.users || [];
    const procs = m.procs_top || [];
    const cmds = d.commands || [];
    $("#detail").innerHTML = `
      <div class="card">
        <div class="row">
          <span class="dot ${d.online ? "on" : "off"}"></span>${d.online ? "在线" : "离线"}
          <span class="muted">${esc(d.image || "-")}</span>
          <span class="chip">agent ${esc(d.agent_ver)}</span>
          <span class="chip">间隔 ${d.hb_interval}s</span>
          <span class="muted">首见 ${fmtTs(d.first_seen)} · 最后心跳 ${fmtTs(d.last_seen)}</span>
          <span style="flex:1"></span>
          <button id="reload-plugins">重载采集插件</button>
        </div>
      </div>
      <div class="grid2">
        <div class="card"><h3>CPU%（近 24 小时，${series.length} 点）</h3><canvas id="cv-cpu"></canvas>
          <div class="muted">当前 ${m.cpu != null ? m.cpu + "%" : "-"} · load ${esc(m.load || "-")}</div></div>
        <div class="card"><h3>内存 MB（近 24 小时）</h3><canvas id="cv-mem"></canvas>
          <div class="muted">当前 ${m.mem_mb != null ? m.mem_mb + " / " + m.mem_total_mb + " MB (" + m.mem_pct + "%)" : "-"}</div></div>
      </div>
      <div class="grid3">
        <div class="card"><h3>磁盘</h3>${disks.map(([p, dd]) => `
          <div class="row" style="margin-bottom:8px"><span class="mono" style="min-width:110px">${esc(p)}</span>
            ${barHtml(dd.pct)}<span class="muted">${dd.used_mb}/${dd.total_mb} MB · ${dd.pct}%</span></div>`).join("") || '<div class="empty">无数据</div>'}</div>
        <div class="card"><h3>用户（vscode-server 标记）</h3><table>
          <thead><tr><th>用户</th><th class="num">UID</th><th class="num">进程</th><th>IDE</th></tr></thead>
          <tbody>${users.map(u => `<tr><td>${esc(u.name)}</td><td class="num">${u.uid}</td>
            <td class="num">${u.procs}</td><td>${u.vscode ? "✅" : "-"}</td></tr>`).join("") || '<tr><td colspan="4" class="empty">无数据</td></tr>'}</tbody>
        </table></div>
        <div class="card"><h3>Top 进程</h3><table>
          <thead><tr><th>进程</th><th class="num">CPU%</th><th class="num">内存 MB</th></tr></thead>
          <tbody>${procs.map(p2 => `<tr><td class="mono">${esc(p2.name)} <span class="muted">(${p2.pid})</span></td>
            <td class="num">${p2.cpu}</td><td class="num">${p2.mem_mb}</td></tr>`).join("") || '<tr><td colspan="3" class="empty">无数据</td></tr>'}</tbody>
        </table></div>
      </div>
      <div class="grid2">
        <div class="card"><h3>自定义采集（插件）</h3>
          <div class="kv">${esc(JSON.stringify(d.custom || {}, null, 2))}</div></div>
        <div class="card"><h3>快速命令</h3>
          <div class="row"><input type="text" id="quick" placeholder="例如：du -sh /workspace">
            <input type="number" id="qt" value="30" min="1" max="600" style="width:80px" title="超时秒">
            <button id="qrun">执行</button></div>
          <div class="muted" style="font-size:12px">命令经管理通道下发到容器内执行，全程审计</div>
          <h3 style="margin-top:16px">最近命令</h3>${cmds.map(renderCmd).join("") || '<div class="empty">暂无</div>'}
        </div>
      </div>`;
    sparkline($("#cv-cpu"), series.map(s => s.cpu).filter(v => v != null), "#3b82f6");
    sparkline($("#cv-mem"), series.map(s => s.mem_mb).filter(v => v != null), "#a855f7");
    $("#reload-plugins").onclick = async () => {
      try {
        const r = await api("/api/commands", { method: "POST", body: JSON.stringify({ pods: [pod], kind: "plugin_reload" }) });
        toast("已下发插件重载：" + r.items[0].status);
      } catch (e) { toast(e.message, true); }
    };
    const runQuick = async () => {
      const cmdline = $("#quick").value.trim();
      if (!cmdline) return;
      try {
        await api("/api/commands", { method: "POST", body: JSON.stringify({ pods: [pod], cmdline, timeout: +$("#qt").value || 30 }) });
        toast("命令已下发"); $("#quick").value = "";
      } catch (e) { toast(e.message, true); }
    };
    $("#qrun").onclick = runQuick;
    $("#quick").addEventListener("keydown", e => e.key === "Enter" && runQuick());
  }
  await tick();
  setPoll(tick, 5000);
}

function renderCmd(c) {
  const argv = Array.isArray(c.argv) ? c.argv.join(" ") : c.argv;
  const isReload = c.kind === "plugin_reload";
  return `<details style="margin-bottom:8px">
    <summary>${statusChip(c.status)} <span class="mono">${isReload ? "［插件重载］" : esc(argv)}</span>
      <span class="muted" style="font-size:12px"> ${fmtTs(c.created_at)} · ${c.elapsed_ms != null ? c.elapsed_ms + "ms" : ""}${c.rc != null ? " · rc=" + c.rc : ""}${c.timed_out ? " · 超时" : ""}</span></summary>
    ${c.out ? `<pre class="out">${esc(c.out)}</pre>` : ""}
  </details>`;
}

/* ---------- 命令控制台 ---------- */
async function viewConsole() {
  layout("x", `
    <div class="grid2">
      <div class="card">
        <h3>目标容器（勾选可批量）</h3>
        <div id="pods" style="max-height:420px;overflow:auto"></div>
      </div>
      <div class="card">
        <h3>命令</h3>
        <div class="row"><input type="text" id="cmd" placeholder="argv 命令行，例如：df -h /workspace"></div>
        <div class="row">
          <input type="number" id="tmo" value="30" min="1" max="600" style="width:100px" title="超时秒">
          <button id="run">批量下发</button>
          <button id="reload-sel" class="ghost">对勾选容器重载插件</button>
        </div>
        <div class="muted" style="font-size:12px">argv 直传 exec（不经容器内 shell），命中黑名单会被拒绝并记入审计。</div>
        <h3 style="margin-top:18px">执行结果</h3>
        <div id="results"><div class="empty">暂无命令</div></div>
      </div>
    </div>`);
  async function tick() {
    let r;
    try { r = await api("/api/containers"); }
    catch (e) { toast(e.message, true); clearPolls(); return; }
    const checked = new Set([...$("#pods").querySelectorAll("input:checked")].map(i => i.value));
    $("#pods").innerHTML = r.items.map(i => `
      <label class="ck"><input type="checkbox" value="${esc(i.pod)}" ${checked.has(i.pod) ? "checked" : ""}>
        <span class="dot ${i.online ? "on" : "off"}"></span>
        <span class="mono">${esc(i.pod)}</span>
        <span class="muted" style="font-size:12px">${i.online ? "" : "离线"}</span></label>`).join("");
  }
  setPoll(tick, 5000);
  $("#run").onclick = async () => {
    const pods = [...$("#pods").querySelectorAll("input:checked")].map(i => i.value);
    const cmdline = $("#cmd").value.trim();
    if (!pods.length) return toast("请先勾选目标容器", true);
    if (!cmdline) return toast("请输入命令", true);
    try {
      const r = await api("/api/commands", { method: "POST", body: JSON.stringify({ pods, cmdline, timeout: +$("#tmo").value || 30 }) });
      toast(`已下发 ${r.items.length} 条（${r.items.filter(i => i.status === "sent").length} 条直达，${r.items.filter(i => i.status === "pending").length} 条离线排队）`);
      pollResults();
    } catch (e) { toast(e.message, true); }
  };
  $("#reload-sel").onclick = async () => {
    const pods = [...$("#pods").querySelectorAll("input:checked")].map(i => i.value);
    if (!pods.length) return toast("请先勾选目标容器", true);
    try {
      await api("/api/commands", { method: "POST", body: JSON.stringify({ pods, kind: "plugin_reload" }) });
      toast("插件重载已下发"); pollResults();
    } catch (e) { toast(e.message, true); }
  };
  async function pollResults() {
    try {
      const r = await api("/api/commands?limit=30");
      $("#results").innerHTML = r.items.map(renderCmd).join("") || '<div class="empty">暂无命令</div>';
    } catch (e) {}
  }
  pollResults();
  setPoll(pollResults, 2000);
}

/* ---------- 审计 ---------- */
async function viewAudit() {
  layout("a", `<div class="card"><table>
    <thead><tr><th>时间</th><th>操作者</th><th>动作</th><th>详情</th></tr></thead>
    <tbody id="rows"><tr><td colspan="4" class="empty">加载中…</td></tr></tbody>
  </table></div>`);
  async function tick() {
    try {
      const r = await api("/api/audit?limit=200");
      $("#rows").innerHTML = r.items.map(a => `<tr>
        <td class="muted nowrap">${fmtTs(a.ts)}</td><td>${esc(a.actor)}</td><td>${esc(a.action)}</td>
        <td class="kv">${esc(a.detail || "")}</td></tr>`).join("") || '<tr><td colspan="4" class="empty">暂无记录</td></tr>';
    } catch (e) { toast(e.message, true); clearPolls(); }
  }
  await tick();
  setPoll(tick, 10000);
}

/* ---------- 启动 ---------- */
(async () => {
  if (TOKEN) {
    try { const me = await api("/api/me"); USER = me.username; } catch (e) {}
  }
  nav();
})();
