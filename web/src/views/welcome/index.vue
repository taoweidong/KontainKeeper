<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import { useRouter } from "vue-router";
import { ElMessage } from "element-plus";

import { listHosts, type HostSummary } from "@/api/containers";
import { getHealth, getStats, type HealthResult, type StatsResult } from "@/api/system";
import { listCommands, type CommandRow } from "@/api/commands";
import { ageText, durText, statusLabel, statusType, tsText } from "@/utils/kk";
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
/** 汇总页 10s 轮询：给个「页面活着」的信号即可，不必更密 */
let timer: ReturnType<typeof setInterval> | null = null;

const offline = computed(() => hosts.value.length - online.value);

/** 命令状态分布里值得一眼关注的项（其余归入「其他」） */
const cmdStats = computed(() => {
  const c = stats.value?.commands ?? {};
  const entries: Array<{ label: string; key: string; value: number }> = [
    { label: "已完成", key: "done", value: c.done ?? 0 },
    { label: "失败", key: "failed", value: (c.failed ?? 0) + (c.timeout ?? 0) + (c.lost ?? 0) },
    { label: "执行中", key: "running", value: (c.running ?? 0) + (c.sent ?? 0) },
    { label: "待下发", key: "pending", value: c.pending ?? 0 }
  ];
  return entries;
});

const brokerOk = computed(() => stats.value?.broker?.connected ?? false);

/** 离线主机名单（最多 5 个）：告警卡片下钻用 */
const offlineHosts = computed(() =>
  hosts.value.filter(h => !h.online).slice(0, 5).map(h => h.pod)
);

const alertHosts = computed(() =>
  hosts.value.filter(h => h.disk_alert).slice(0, 5).map(h => h.pod)
);

async function load() {
  loading.value = true;
  try {
    const [hostData, statsData, cmdData, healthData] = await Promise.all([
      listHosts("summary"),
      getStats().catch(() => null),
      listCommands({ limit: 12 }).catch(() => ({ items: [] as CommandRow[] })),
      getHealth().catch(() => null)
    ]);
    hosts.value = hostData.items;
    online.value = hostData.online;
    alerts.value = hostData.alerts;
    stats.value = statsData;
    recentCmds.value = cmdData.items;
    health.value = healthData;
  } catch (e: any) {
    ElMessage.error("加载汇总数据失败：" + (e?.message ?? e));
  } finally {
    loading.value = false;
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

onMounted(() => {
  load();
  timer = setInterval(load, 10 * 1000);
});

onBeforeUnmount(() => {
  if (timer) clearInterval(timer);
  timer = null;
});
</script>

<template>
  <div class="welcome" v-loading="loading">
    <!-- 统计卡片行：核心数字一眼可见，点击进入对应页面 -->
    <el-row :gutter="16" class="stat-row">
      <el-col :xs="12" :sm="6">
        <el-card shadow="hover" class="stat-card clickable" @click="go('/hosts/monitor')">
          <div class="stat-value">{{ hosts.length }}</div>
          <div class="stat-label">主机总数</div>
          <div class="stat-sub">
            在线 {{ online }} / 离线 {{ offline }}
          </div>
        </el-card>
      </el-col>
      <el-col :xs="12" :sm="6">
        <el-card
          shadow="hover"
          class="stat-card clickable"
          :class="{ 'stat-warn': alerts > 0 }"
          @click="go('/hosts/monitor')"
        >
          <div class="stat-value" :class="{ 'text-danger': alerts > 0 }">{{ alerts }}</div>
          <div class="stat-label">磁盘告警</div>
          <div class="stat-sub text-overflow" :title="alertHosts.join('、')">
            {{ alertHosts.length ? alertHosts.join("、") : "无告警主机" }}
          </div>
        </el-card>
      </el-col>
      <el-col :xs="12" :sm="6">
        <el-card shadow="hover" class="stat-card clickable" @click="go('/command/index')">
          <div class="stat-value">{{ cmdStats.reduce((s, i) => s + i.value, 0) }}</div>
          <div class="stat-label">命令总数</div>
          <div class="stat-sub">
            失败 {{ cmdStats[1].value }} / 执行中 {{ cmdStats[2].value }}
          </div>
        </el-card>
      </el-col>
      <el-col :xs="12" :sm="6">
        <el-card shadow="hover" class="stat-card">
          <div class="stat-value" :class="brokerOk ? 'text-success' : 'text-danger'">
            {{ brokerOk ? "正常" : "断开" }}
          </div>
          <div class="stat-label">Broker 链路</div>
          <div class="stat-sub">
            <template v-if="stats?.broker?.last_msg_age_sec !== null && stats?.broker?.last_msg_age_sec !== undefined">
              最近消息 {{ ageText(stats.broker.last_msg_age_sec) }}
            </template>
            <template v-else>暂无消息</template>
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
              <el-button link type="primary" @click="go('/command/index')">
                命令中心 →
              </el-button>
            </div>
          </template>
          <el-table
            :data="recentCmds"
            size="small"
            height="100%"
            class="cmd-table"
            @row-click="(r: any) => go('/command/index')"
          >
            <el-table-column prop="created_at" label="时间" width="170">
              <template #default="{ row }">{{ tsText(row.created_at) }}</template>
            </el-table-column>
            <el-table-column prop="pod" label="主机" min-width="120" show-overflow-tooltip />
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
              <template #default="{ row }">
                {{ row.elapsed_ms !== null && row.elapsed_ms !== undefined ? `${row.elapsed_ms}ms` : "-" }}
              </template>
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
            <div class="quick-link" @click="go('/hosts/monitor')">
              <el-icon size="22"><component :is="useRenderIcon(MonitorIcon)" /></el-icon>
              <span>主机总览</span>
            </div>
            <div class="quick-link" @click="go('/command/index')">
              <el-icon size="22"><component :is="useRenderIcon(CommandIcon)" /></el-icon>
              <span>命令中心</span>
            </div>
            <div class="quick-link" @click="go('/audit/index')">
              <el-icon size="22"><component :is="useRenderIcon(AuditIcon)" /></el-icon>
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
              @click="goHost(pod)"
            >
              <el-tag type="danger" size="small" effect="plain">离线</el-tag>
              <span class="pod-name">{{ pod }}</span>
            </div>
            <div class="stat-sub" style="margin-top: 8px">
              共 {{ offline }} 台离线，
              <el-link type="primary" :underline="false" @click="go('/hosts/monitor')">
                查看全部 →
              </el-link>
            </div>
          </template>
          <div v-else class="all-online">全部主机在线</div>
        </el-card>

        <el-card shadow="never" class="panel grow">
          <template #header><span>系统状态</span></template>
          <div class="sys-row">
            <span>服务版本</span><span>{{ health?.version ?? "-" }}</span>
          </div>
          <div class="sys-row">
            <span>协议版本</span><span>{{ health ? `v${health.proto_ver}` : "-" }}</span>
          </div>
          <div class="sys-row">
            <span>心跳样本</span><span>{{ stats?.storage?.heartbeats ?? "-" }}</span>
          </div>
          <div class="sys-row">
            <span>运行时长</span><span>{{ stats ? durText(stats.uptime_sec) : "-" }}</span>
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
