<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { useRouter } from "vue-router";
import { message } from "@/utils/message";

import { listHosts, type HostSummary } from "@/api/containers";
import {
  getHealth,
  getStats,
  type HealthResult,
  type StatsResult
} from "@/api/system";
import { listCommands, type CommandRow } from "@/api/commands";
import {
  argvText,
  cmdStateClass,
  durText,
  elapsedText,
  errText,
  kindText,
  nOr,
  statusLabel,
  tsText
} from "@/utils/kk";
import { usePolls, useSeq } from "@/utils/kkPoll";
import { useRenderIcon } from "@/components/ReIcon/src/hooks";
import {
  IconAlert,
  IconAudit,
  IconCommand,
  IconGrid,
  IconHost,
  IconMetal,
  IconStats,
  IconVersion
} from "@/utils/kkIcons";

defineOptions({ name: "Welcome" });

const router = useRouter();

const loading = ref(false);
const hosts = ref<HostSummary[]>([]);
const online = ref(0);
const alerts = ref(0);
const stats = ref<StatsResult | null>(null);
const health = ref<HealthResult | null>(null);
const recentCmds = ref<CommandRow[]>([]);
/** 静默轮询失败：状态进页头一条读数，不再每 10s 弹 toast（W5） */
const pollFailed = ref(false);
// 卸载时统一清轮询（漏一处就是「切页后仍在刷接口」）；作用域版只清本页注册的 key
const { setPoll } = usePolls();
/** 轮询与手动刷新两个入口，迟到的回包不能盖掉更新的一轮数据（QR-W4）。 */
const beginLoad = useSeq();

// 夹住下界（QR1120-W16）：两路读数来自同一响应但口径独立，坏数据不渲染负数台
const offline = computed(() => Math.max(0, hosts.value.length - online.value));

/**
 * 「读到过」与「读到 0」必须是两件事（QR-W16，同 /system 那条红线）：
 * 首页的 0 和「无告警主机」在请求失败时是**假的安全声明**，比不显示更危险。
 * hosts/命令两个来源空数组是合法值，所以只能各记一笔；stats 用 null 自身判定。
 */
const hostsRead = ref(false);
const cmdsRead = ref(false);
const statsRead = computed(() => stats.value !== null);

/** 命令状态分布里值得一眼关注的项（其余归入「其他」） */
const cmdStats = computed(() => {
  const c = stats.value?.commands ?? {};
  const entries: Array<{ label: string; key: string; value: number }> = [
    { label: "已完成", key: "done", value: c.done ?? 0 },
    {
      label: "失败",
      key: "failed",
      value: (c.failed ?? 0) + (c.timeout ?? 0) + (c.lost ?? 0)
    },
    {
      label: "执行中",
      key: "running",
      value: (c.running ?? 0) + (c.sent ?? 0)
    },
    { label: "待下发", key: "pending", value: c.pending ?? 0 },
    // queued 必须计入（QR1120-W8）：离线主机的命令排在 Broker 等补投，
    // 漏了它「命令总数」就与统计页的在途口径（含 queued）打架
    { label: "已排队", key: "queued", value: c.queued ?? 0 }
  ];
  return entries;
});

const brokerOk = computed(() => stats.value?.broker?.connected ?? false);

/** 三态分开：没问到 ≠ 断开 ≠ 正常。把 null 说成「断开」会把人支去查 Broker，而真停的是服务端 */
const brokerLabel = computed(() =>
  statsRead.value ? (brokerOk.value ? "正常" : "断开") : "未知（没读到）"
);

/** 落后台数 + 当前版本：欢迎页头部一句话「Agent 版本 vX.Y.Z · 落后 N 台」
 *  —— 没上传过版本时显示「尚无版本」（不是「落后 0 台」）。 */
const agentVerText = computed(() => {
  if (!statsRead.value) return "没读到（服务端不可达）";
  const v = stats.value?.agent_latest_ver ?? "";
  const n = stats.value?.agents_outdated ?? 0;
  if (!v) return "尚无版本（去「版本与更新」上传二进制）";
  return `Agent 版本 ${v} · 落后 ${n} 台`;
});

/** 跳到「版本与更新」页 */
function gotoUpdate() {
  router.push("/hosts/update");
}

/** 离线主机名单（最多 5 个）：告警卡片下钻用 */
const offlineHosts = computed(() =>
  hosts.value
    .filter(h => !h.online)
    .slice(0, 5)
    .map(h => h.pod)
);

const alertHosts = computed(() =>
  hosts.value
    .filter(h => h.disk_alert)
    .slice(0, 5)
    .map(h => h.pod)
);

/** silent=true 供轮询复用：数据原位更新，不闪整页 loading（交互流畅度，评审 P3） */
async function load(silent = false) {
  const isLatest = beginLoad();
  if (!silent) loading.value = true;
  try {
    // 每一路单独记账：Promise.all 整体成功不等于每路都成功。原先三个 .catch 把
    // 失败吞成 null/[]，页面照样把「没读到」渲染成 0（QR-W16）。
    const miss = { stats: false, cmds: false, health: false };
    const [hostData, statsData, cmdData, healthData] = await Promise.all([
      listHosts("summary"),
      getStats().catch(() => {
        miss.stats = true;
        return null;
      }),
      listCommands({ limit: 12 }).catch(() => {
        miss.cmds = true;
        return null;
      }),
      getHealth().catch(() => {
        miss.health = true;
        return null;
      })
    ]);
    if (!isLatest()) return;
    hosts.value = hostData.items;
    online.value = hostData.online;
    alerts.value = hostData.alerts;
    hostsRead.value = true;
    // 某一路失败就保留上一轮的真值，只让页头读数变冷——用空值盖掉真读数是第二次的谎
    if (!miss.stats) stats.value = statsData;
    if (!miss.cmds && cmdData) {
      recentCmds.value = cmdData.items;
      cmdsRead.value = true;
    }
    if (!miss.health) health.value = healthData;
    pollFailed.value = miss.stats || miss.cmds || miss.health;
  } catch (e: any) {
    if (!isLatest()) return;
    // 手动失败同样要让读数变冷：数据确实可能已过期，区别只是多一条 toast
    pollFailed.value = true;
    if (!silent) message("加载汇总数据失败：" + errText(e), { type: "error" });
  } finally {
    if (isLatest()) loading.value = false;
  }
}

// 命令类型中文收口到 @/utils/kk 的 kindText（QR1121-W19），本页不再内联一份

function go(path: string) {
  router.push(path);
}

/** 主机行跳详情；空行点击跳总览 */
function goHost(pod: string) {
  router.push(`/hosts/detail/${encodeURIComponent(pod)}`);
}

/** 「整块可点」的卡片与条目必须同时能用键盘走到：Tab 聚焦、Enter/Space 触发同一个动作。
 *  这些原来是纯 div，键盘用户在首页一步都走不动（FE-17）。 */
const press = (run: () => void) => ({
  role: "button",
  tabindex: "0",
  onClick: run,
  onKeyup: (e: KeyboardEvent) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      run();
    }
  }
});

/** 按 key 取命令读数：写死下标等于把「数组一改顺序界面就错」埋进模板（FE-29）。 */
const cmdStat = (key: string): number =>
  cmdStats.value.find(i => i.key === key)?.value ?? 0;

onMounted(() => {
  load();
  // 汇总页 10s 轮询：给个「页面活着」的信号即可，不必更密；静默刷新不闪 loading
  setPoll("welcome", () => load(true), 10 * 1000);
});
</script>

<template>
  <div v-loading="loading" class="welcome kk-page--grow">
    <!-- 轮询失败只在页头说一次：后端宕机时 10s 一次的 toast 会把真正的告警淹成噪音（W5） -->
    <div v-if="pollFailed" class="kk-sync kk-sync--stale kk-mb">
      刷新失败，下方读数可能已过期
    </div>
    <!-- 统计卡片行：核心数字一眼可见，点击进入对应页面 -->
    <el-row :gutter="16" class="stat-row">
      <el-col :xs="12" :sm="6">
        <el-card
          shadow="never"
          class="stat-card clickable"
          v-bind="press(() => go('/hosts/monitor'))"
        >
          <div class="stat-value">{{ nOr(hostsRead, hosts.length) }}</div>
          <div class="stat-label">
            <el-icon class="kk-type-icon"
              ><component :is="useRenderIcon(IconHost)" /></el-icon
            >主机总数
          </div>
          <div class="stat-sub">
            在线 {{ nOr(hostsRead, online) }} / 离线
            {{ nOr(hostsRead, offline) }}
          </div>
        </el-card>
      </el-col>
      <el-col :xs="12" :sm="6">
        <el-card
          shadow="never"
          class="stat-card clickable"
          :class="{ 'stat-warn': alerts > 0 }"
          v-bind="press(() => go('/hosts/monitor'))"
        >
          <div class="stat-value" :class="{ 'text-danger': alerts > 0 }">
            {{ nOr(hostsRead, alerts) }}
          </div>
          <div class="stat-label">
            <el-icon class="kk-type-icon"
              ><component :is="useRenderIcon(IconAlert)" /></el-icon
            >磁盘告警
          </div>
          <div
            class="stat-sub text-overflow"
            :title="alertHosts.length ? alertHosts.join('、') : ''"
          >
            {{
              hostsRead
                ? alertHosts.length
                  ? alertHosts.join("、")
                  : "无告警主机"
                : "没读到（主机接口不可达）"
            }}
          </div>
        </el-card>
      </el-col>
      <el-col :xs="12" :sm="6">
        <el-card
          shadow="never"
          class="stat-card clickable"
          v-bind="press(() => go('/command/shell'))"
        >
          <div class="stat-value">
            {{
              nOr(
                statsRead,
                cmdStats.reduce((s, i) => s + i.value, 0)
              )
            }}
          </div>
          <div class="stat-label">
            <el-icon class="kk-type-icon"
              ><component :is="useRenderIcon(IconCommand)" /></el-icon
            >命令总数
          </div>
          <div class="stat-sub">
            失败 {{ nOr(statsRead, cmdStat("failed")) }} / 执行中
            {{ nOr(statsRead, cmdStat("running")) }}
          </div>
        </el-card>
      </el-col>
      <el-col :xs="12" :sm="6">
        <el-card
          shadow="never"
          class="stat-card clickable"
          v-bind="press(gotoUpdate)"
        >
          <!-- 版本字符串不走大数字档（QR1121）：长版本号（v1.2.3-beta.10）
               会撑破卡片，16px + ellipsis + title 兜底 -->
          <div
            class="stat-value stat-value--ver"
            :class="{ 'text-warning': (stats?.agents_outdated ?? 0) > 0 }"
            :title="stats?.agent_latest_ver ? `v${stats.agent_latest_ver}` : ''"
          >
            {{ stats?.agent_latest_ver ? `v${stats.agent_latest_ver}` : "—" }}
          </div>
          <div class="stat-label">
            <el-icon class="kk-type-icon"
              ><component :is="useRenderIcon(IconVersion)" /></el-icon
            >Agent 当前版本
          </div>
          <div class="stat-sub text-overflow" :title="agentVerText">
            {{ agentVerText }}
          </div>
        </el-card>
      </el-col>
    </el-row>

    <el-row :gutter="16" class="content-row">
      <!-- 左：最近命令（跳命令中心）。卡片拉满列高，与右栏底边对齐 -->
      <el-col :xs="24" :lg="16" class="left-col">
        <el-card shadow="never" class="panel fill">
          <template #header>
            <div class="panel-header">
              <span class="panel-title"
                ><el-icon class="kk-type-icon"
                  ><component :is="useRenderIcon(IconCommand)" /></el-icon
                >最近命令</span
              >
              <el-button
                link
                type="primary"
                :icon="useRenderIcon(IconCommand)"
                @click="go('/command/shell')"
              >
                进命令中心
              </el-button>
            </div>
          </template>
          <el-table
            :data="recentCmds"
            size="small"
            height="100%"
            class="cmd-table"
            @row-click="(r: any) => go('/command/shell')"
          >
            <el-table-column prop="created_at" label="时间" width="170">
              <template #default="{ row }">
                <span class="kk-num">{{ tsText(row.created_at) }}</span>
              </template>
            </el-table-column>
            <el-table-column
              prop="pod"
              label="主机"
              min-width="120"
              show-overflow-tooltip
            />
            <el-table-column prop="kind" label="类型" width="90">
              <template #default="{ row }">{{ kindText(row.kind) }}</template>
            </el-table-column>
            <el-table-column label="内容" min-width="180" show-overflow-tooltip>
              <template #default="{ row }">{{
                argvText((row as CommandRow).argv)
              }}</template>
            </el-table-column>
            <el-table-column prop="status" label="状态" width="90">
              <template #default="{ row }">
                <!-- 圆点四档与命令历史/统计页同语言（QR1121-W2） -->
                <span :class="cmdStateClass(row.status)">{{
                  statusLabel(row.status)
                }}</span>
              </template>
            </el-table-column>
            <el-table-column label="耗时" width="90">
              <template #default="{ row }">
                <span class="kk-num">{{ elapsedText(row.elapsed_ms) }}</span>
              </template>
            </el-table-column>
            <template #empty>
              {{
                cmdsRead
                  ? "暂无命令记录，去命令中心下发第一条"
                  : "没读到（命令接口不可达）"
              }}
            </template>
          </el-table>
        </el-card>
      </el-col>

      <!-- 右：快速入口 + 离线主机 + 系统状态。flex 纵排，末卡弹性拉伸保证与左栏底边对齐 -->
      <el-col :xs="24" :lg="8" class="right-col">
        <el-card shadow="never" class="panel">
          <template #header
            ><span class="panel-title"
              ><el-icon class="kk-type-icon"
                ><component :is="useRenderIcon(IconGrid)" /></el-icon
              >快速入口</span
            ></template
          >
          <div class="quick-links">
            <div class="quick-link" v-bind="press(() => go('/hosts/monitor'))">
              <el-icon><component :is="useRenderIcon(IconHost)" /></el-icon>
              <span>主机总览</span>
            </div>
            <div class="quick-link" v-bind="press(() => go('/command/shell'))">
              <el-icon><component :is="useRenderIcon(IconCommand)" /></el-icon>
              <span>命令中心</span>
            </div>
            <div class="quick-link" v-bind="press(() => go('/audit/index'))">
              <el-icon><component :is="useRenderIcon(IconAudit)" /></el-icon>
              <span>审计日志</span>
            </div>
          </div>
        </el-card>

        <el-card shadow="never" class="panel">
          <template #header
            ><span class="panel-title"
              ><el-icon class="kk-type-icon"
                ><component :is="useRenderIcon(IconMetal)" /></el-icon
              >离线主机</span
            ></template
          >
          <template v-if="offlineHosts.length">
            <div
              v-for="pod in offlineHosts"
              :key="pod"
              class="offline-host clickable"
              v-bind="press(() => goHost(pod))"
            >
              <!-- 离线不是错误（QR1121）：空心冷灰圆点与 monitor/detail 同语言，
                   不再用 danger 红 tag 把运维场景渲染成告警 -->
              <span class="kk-state kk-state--idle">离线</span>
              <span class="pod-name">{{ pod }}</span>
            </div>
            <div class="stat-sub kk-mt">
              共 {{ offline }} 台离线，
              <el-link
                type="primary"
                underline="never"
                @click="go('/hosts/monitor')"
              >
                查看全部
              </el-link>
            </div>
          </template>
          <div v-else :class="hostsRead ? 'all-online' : 'stat-sub'">
            {{ hostsRead ? "全部主机在线" : "没读到（主机接口不可达）" }}
          </div>
        </el-card>

        <el-card shadow="never" class="panel grow">
          <template #header
            ><span class="panel-title"
              ><el-icon class="kk-type-icon"
                ><component :is="useRenderIcon(IconStats)" /></el-icon
              >系统状态</span
            ></template
          >
          <div class="sys-row">
            <span>Broker 链路</span>
            <span
              :class="
                statsRead ? (brokerOk ? 'text-success' : 'text-danger') : ''
              "
            >
              {{ brokerLabel }}
            </span>
          </div>
          <div class="sys-row">
            <span>服务版本</span><span>{{ health?.version ?? "-" }}</span>
          </div>
          <div class="sys-row">
            <span>协议版本</span
            ><span>{{ health ? `v${health.proto_ver}` : "-" }}</span>
          </div>
          <div class="sys-row">
            <span>心跳样本</span
            ><span>{{ stats?.storage?.heartbeats ?? "-" }}</span>
          </div>
          <div class="sys-row">
            <span>运行时长</span
            ><span>{{ stats ? durText(stats.uptime_sec) : "-" }}</span>
          </div>
          <!-- 升级汇总（QR1121）：StatsResult.updates 此前未展示；
               statsRead 守卫——没读到显 —，不补 0（QR-W16） -->
          <div class="sys-row">
            <span>升级汇总</span>
            <span v-if="statsRead" class="kk-num">
              在途 {{ stats?.updates?.in_flight ?? 0 }} · 完成
              {{ stats?.updates?.done ?? 0 }} · 失败
              {{ stats?.updates?.failed ?? 0 }} · 超时
              {{ stats?.updates?.timeout ?? 0 }}
            </span>
            <span v-else>—</span>
          </div>
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>

<style scoped lang="scss">
/* 页面留白由全局 .kk-page--grow 统一供给（横向 32px + 顶部 12px，与统计页/
   详情页同源）；块间距由 .kk-page--grow > * + * 统一给 16px——本页不再自带
   padding/margin（QR1121 清理了原先两条只有注释的空规则）。 */

/* xs=12 窄屏两行堆叠时的纵向缝（QR1122-W5）：gutter 只管横向，
   el-row 是 flex，row-gap 生效 */
.stat-row {
  row-gap: var(--kk-space-lg);
}

.stat-card {
  text-align: center;
  cursor: default;

  :deep(.el-card__body) {
    padding: var(--kk-space-lg) var(--kk-space);
  }

  /* hover 收口为扁平（QR1122-W19）：原 shadow=hover + 上浮 2px 与全站
     「不靠阴影」语言相悖，改边框主色（与 quick-link 入口同写法） */
  &.clickable {
    cursor: pointer;
    transition: border-color 0.15s;

    &:hover {
      border-color: var(--el-color-primary);
    }
  }

  /* 大数字尺度收口（QR1121）：原硬编码 30px 与仪表带 16px 两套无承载，
     统一走 --kk-font-stat（24px）+ 全站等宽栈，数字逐位对齐 */
  .stat-value {
    font-family: var(--kk-font-num);
    font-size: var(--kk-font-stat);
    font-weight: 600;
    font-variant-numeric: tabular-nums;
    line-height: 1.2;
  }

  /* 版本字符串不是数字：小一档 + 溢出保护，长版本号不撑破卡片。
     line-height 1.8 使 16×1.8=28.8 与 24px 档（24×1.2）等高，
     四卡横排 label/sub 基线对齐（QR1122-W2） */
  .stat-value--ver {
    overflow: hidden;
    font-size: 16px;
    line-height: 1.8;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .stat-label {
    margin-top: var(--kk-space-sm);
    font-size: 14px;
    color: var(--el-text-color-secondary);
  }

  .stat-sub {
    margin-top: var(--kk-space-sm);
    font-size: 12px;
    color: var(--el-text-color-placeholder);
  }
}

.stat-warn {
  border-color: var(--el-color-danger-light-7);
}

// 第二行左右两栏底边对齐：el-row 是 flex，col 拉伸同高（取更高一侧），
// 左栏卡片撑满列高，右栏纵排卡片用 gap 控距、末卡弹性补齐。
// flex:1 让这一行吃掉 grow 根（min-height）撑出来的剩余高度：视口富余时
// 卡片下缘贴到页脚，不再在页脚上方留一条灰白空带（2026-10-31 收口）。
.content-row {
  display: flex;
  flex: 1;
  align-items: stretch;
}

.left-col {
  display: flex;

  .panel {
    display: flex;
    flex-direction: column;
    width: 100%;
    margin-bottom: 0;

    // 表格填满卡片剩余高度：行不足时空白收进表格区域，不出现卡片大片留白
    :deep(.el-card__body) {
      display: flex;
      flex: 1;
      flex-direction: column;
      min-height: 0;
    }

    .cmd-table {
      flex: 1;
      min-height: 0;
    }
  }
}

.right-col {
  display: flex;
  flex-direction: column;
  gap: var(--kk-space-lg);

  .panel {
    margin-bottom: 0;
  }

  // 末卡（系统状态）拉伸吸收左右栏高度差，保证底边对齐
  .panel.grow {
    flex: 1;
  }
}

.all-online {
  padding: var(--kk-space-md) 0;
  font-size: 13px;
  color: var(--el-text-color-placeholder);
  text-align: center;
}

.panel-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.quick-links {
  display: flex;
  flex-wrap: wrap;
  gap: var(--kk-space-md);
}

.quick-link {
  display: flex;
  flex: 1;
  flex-direction: column;
  gap: var(--kk-space-sm);
  align-items: center;
  padding: var(--kk-space-lg) var(--kk-space);
  cursor: pointer;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 2px;
  transition: all 0.15s;

  &:hover {
    color: var(--el-color-primary);
    background-color: var(--el-color-primary-light-9);
    border-color: var(--el-color-primary);
  }

  /* 图标尺寸走类不写内联 size（QR1121）：flex:1 已分配宽度，min-width 退役 */
  .el-icon {
    font-size: 16px;
  }

  span {
    font-size: 13px;
  }
}

.offline-host {
  display: flex;
  gap: var(--kk-space);
  align-items: center;
  padding: var(--kk-space-sm) 0;
  cursor: pointer;

  .pod-name {
    font-size: 13px;

    &:hover {
      color: var(--el-color-primary);
    }
  }
}

.sys-row {
  display: flex;
  justify-content: space-between;
  padding: var(--kk-space-sm) 0;
  font-size: 13px;

  span:first-child {
    color: var(--el-text-color-secondary);
  }
}

.text-success {
  color: var(--el-color-success);
}

.text-danger {
  color: var(--el-color-danger);
}

.text-warning {
  color: var(--el-color-warning);
}

.text-overflow {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

// 表格行可点
:deep(.el-table__row) {
  cursor: pointer;
}
</style>
