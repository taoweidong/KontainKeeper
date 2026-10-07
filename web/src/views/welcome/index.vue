<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { useRouter } from "vue-router";
import { ElMessage } from "element-plus";

import { listHosts, type HostSummary } from "@/api/containers";
import {
  getHealth,
  getStats,
  type HealthResult,
  type StatsResult
} from "@/api/system";
import { listCommands, type CommandRow } from "@/api/commands";
import {
  durText,
  elapsedText,
  statusLabel,
  statusType,
  tsText
} from "@/utils/kk";
import { usePolls, useSeq } from "@/utils/kkPoll";
import { useRenderIcon } from "@/components/ReIcon/src/hooks";
import MonitorIcon from "~icons/ri/dashboard-2-line";
import CommandIcon from "~icons/ri/terminal-box-line";
import AuditIcon from "~icons/ri/file-list-3-line";

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

const offline = computed(() => hosts.value.length - online.value);

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
    { label: "待下发", key: "pending", value: c.pending ?? 0 }
  ];
  return entries;
});

const brokerOk = computed(() => stats.value?.broker?.connected ?? false);

/** 落后台数 + 当前版本：欢迎页头部一句话「Agent 版本 vX.Y.Z · 落后 N 台」
 *  —— 没上传过版本时显示「尚无版本」（不是「落后 0 台」）。 */
const agentVerText = computed(() => {
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
    const [hostData, statsData, cmdData, healthData] = await Promise.all([
      listHosts("summary"),
      getStats().catch(() => null),
      listCommands({ limit: 12 }).catch(() => ({ items: [] as CommandRow[] })),
      getHealth().catch(() => null)
    ]);
    if (!isLatest()) return;
    pollFailed.value = false;
    hosts.value = hostData.items;
    online.value = hostData.online;
    alerts.value = hostData.alerts;
    stats.value = statsData;
    recentCmds.value = cmdData.items;
    health.value = healthData;
  } catch (e: any) {
    if (!isLatest()) return;
    pollFailed.value = silent;
    if (!silent) ElMessage.error("加载汇总数据失败：" + (e?.message ?? e));
  } finally {
    if (isLatest()) loading.value = false;
  }
}

function kindLabel(k: string): string {
  return { shell: "命令", collect: "采集", plugin_reload: "插件重载" }[k] || k;
}

/** 命令内容预览：列表接口的 argv 是 JSON 字符串（单条接口才是对象），两种都兼容 */
function argvText(row: CommandRow): string {
  let argv: any = row.argv;
  if (typeof argv === "string") {
    try {
      argv = JSON.parse(argv);
    } catch {
      return argv || "-";
    }
  }
  if (Array.isArray(argv)) return argv.join(" ");
  if (argv && typeof argv === "object") {
    if (Array.isArray(argv.items)) return "采集项: " + argv.items.join(", ");
    return JSON.stringify(argv);
  }
  return "-";
}

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
  <div v-loading="loading" class="welcome">
    <!-- 轮询失败只在页头说一次：后端宕机时 10s 一次的 toast 会把真正的告警淹成噪音（W5） -->
    <div v-if="pollFailed" class="kk-sync kk-sync--stale kk-mb">
      自动刷新失败，下方读数可能已过期
    </div>
    <!-- 统计卡片行：核心数字一眼可见，点击进入对应页面 -->
    <el-row :gutter="16" class="stat-row">
      <el-col :xs="12" :sm="6">
        <el-card
          shadow="hover"
          class="stat-card clickable"
          v-bind="press(() => go('/hosts/monitor'))"
        >
          <div class="stat-value">{{ hosts.length }}</div>
          <div class="stat-label">主机总数</div>
          <div class="stat-sub">在线 {{ online }} / 离线 {{ offline }}</div>
        </el-card>
      </el-col>
      <el-col :xs="12" :sm="6">
        <el-card
          shadow="hover"
          class="stat-card clickable"
          :class="{ 'stat-warn': alerts > 0 }"
          v-bind="press(() => go('/hosts/monitor'))"
        >
          <div class="stat-value" :class="{ 'text-danger': alerts > 0 }">
            {{ alerts }}
          </div>
          <div class="stat-label">磁盘告警</div>
          <div class="stat-sub text-overflow" :title="alertHosts.join('、')">
            {{ alertHosts.length ? alertHosts.join("、") : "无告警主机" }}
          </div>
        </el-card>
      </el-col>
      <el-col :xs="12" :sm="6">
        <el-card
          shadow="hover"
          class="stat-card clickable"
          v-bind="press(() => go('/command/shell'))"
        >
          <div class="stat-value">
            {{ cmdStats.reduce((s, i) => s + i.value, 0) }}
          </div>
          <div class="stat-label">命令总数</div>
          <div class="stat-sub">
            失败 {{ cmdStat("failed") }} / 执行中 {{ cmdStat("running") }}
          </div>
        </el-card>
      </el-col>
      <el-col :xs="12" :sm="6">
        <el-card
          shadow="hover"
          class="stat-card clickable"
          v-bind="press(gotoUpdate)"
        >
          <div
            class="stat-value"
            :class="{ 'text-warning': (stats?.agents_outdated ?? 0) > 0 }"
          >
            {{ stats?.agent_latest_ver ? `v${stats.agent_latest_ver}` : "—" }}
          </div>
          <div class="stat-label">Agent 当前版本</div>
          <div class="stat-sub text-overflow">
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
              <span>最近命令</span>
              <el-button link type="primary" @click="go('/command/shell')">
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
              <template #default="{ row }">{{
                tsText(row.created_at)
              }}</template>
            </el-table-column>
            <el-table-column
              prop="pod"
              label="主机"
              min-width="120"
              show-overflow-tooltip
            />
            <el-table-column prop="kind" label="类型" width="90">
              <template #default="{ row }">{{ kindLabel(row.kind) }}</template>
            </el-table-column>
            <el-table-column label="内容" min-width="180" show-overflow-tooltip>
              <template #default="{ row }">{{ argvText(row) }}</template>
            </el-table-column>
            <el-table-column prop="status" label="状态" width="90">
              <template #default="{ row }">
                <el-tag :type="statusType(row.status)" size="small">
                  {{ statusLabel(row.status) }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="耗时" width="90">
              <template #default="{ row }">{{
                elapsedText(row.elapsed_ms)
              }}</template>
            </el-table-column>
            <template #empty>暂无命令记录，去命令中心下发第一条</template>
          </el-table>
        </el-card>
      </el-col>

      <!-- 右：快速入口 + 离线主机 + 系统状态。flex 纵排，末卡弹性拉伸保证与左栏底边对齐 -->
      <el-col :xs="24" :lg="8" class="right-col">
        <el-card shadow="never" class="panel">
          <template #header><span>快速入口</span></template>
          <div class="quick-links">
            <div class="quick-link" v-bind="press(() => go('/hosts/monitor'))">
              <el-icon size="22"
                ><component :is="useRenderIcon(MonitorIcon)"
              /></el-icon>
              <span>主机总览</span>
            </div>
            <div class="quick-link" v-bind="press(() => go('/command/shell'))">
              <el-icon size="22"
                ><component :is="useRenderIcon(CommandIcon)"
              /></el-icon>
              <span>命令中心</span>
            </div>
            <div class="quick-link" v-bind="press(() => go('/audit/index'))">
              <el-icon size="22"
                ><component :is="useRenderIcon(AuditIcon)"
              /></el-icon>
              <span>审计日志</span>
            </div>
          </div>
        </el-card>

        <el-card shadow="never" class="panel">
          <template #header><span>离线主机</span></template>
          <template v-if="offlineHosts.length">
            <div
              v-for="pod in offlineHosts"
              :key="pod"
              class="offline-host clickable"
              v-bind="press(() => goHost(pod))"
            >
              <el-tag type="danger" size="small" effect="plain">离线</el-tag>
              <span class="pod-name">{{ pod }}</span>
            </div>
            <div class="stat-sub" style="margin-top: 8px">
              共 {{ offline }} 台离线，
              <el-link
                type="primary"
                :underline="false"
                @click="go('/hosts/monitor')"
              >
                查看全部
              </el-link>
            </div>
          </template>
          <div v-else class="all-online">全部主机在线</div>
        </el-card>

        <el-card shadow="never" class="panel grow">
          <template #header><span>系统状态</span></template>
          <div class="sys-row">
            <span>Broker 链路</span>
            <span :class="brokerOk ? 'text-success' : 'text-danger'">
              {{ brokerOk ? "正常" : "断开" }}
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
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>

<style scoped lang="scss">
.welcome {
  padding: 0;
}

.stat-row {
  margin-bottom: 16px;
}

.stat-card {
  text-align: center;
  cursor: default;

  :deep(.el-card__body) {
    padding: 18px 12px;
  }

  &.clickable {
    cursor: pointer;
    transition: transform 0.15s;

    &:hover {
      transform: translateY(-2px);
    }
  }

  .stat-value {
    font-size: 30px;
    font-weight: 600;
    line-height: 1.2;
  }

  .stat-label {
    margin-top: 4px;
    color: var(--el-text-color-secondary);
    font-size: 14px;
  }

  .stat-sub {
    margin-top: 6px;
    color: var(--el-text-color-placeholder);
    font-size: 12px;
  }
}

.stat-warn {
  border-color: var(--el-color-danger-light-7);
}

// 第二行左右两栏底边对齐：el-row 是 flex，col 拉伸同高（取更高一侧），
// 左栏卡片撑满列高，右栏纵排卡片用 gap 控距、末卡弹性补齐
.content-row {
  align-items: stretch;
}

.left-col {
  display: flex;

  .panel {
    width: 100%;
    margin-bottom: 0;
    display: flex;
    flex-direction: column;

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
  gap: 16px;

  .panel {
    margin-bottom: 0;
  }

  // 末卡（系统状态）拉伸吸收左右栏高度差，保证底边对齐
  .panel.grow {
    flex: 1;
  }
}

.all-online {
  padding: 14px 0;
  color: var(--el-text-color-placeholder);
  font-size: 13px;
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
  gap: 12px;
}

.quick-link {
  display: flex;
  flex: 1;
  flex-direction: column;
  align-items: center;
  gap: 6px;
  padding: 16px 8px;
  min-width: 80px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
  cursor: pointer;
  transition: all 0.15s;

  &:hover {
    border-color: var(--el-color-primary);
    color: var(--el-color-primary);
    background-color: var(--el-color-primary-light-9);
  }

  span {
    font-size: 13px;
  }
}

.offline-host {
  display: flex;
  gap: 8px;
  align-items: center;
  padding: 4px 0;
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
  padding: 5px 0;
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
