<script setup lang="ts">
import { computed, onMounted, ref, watch } from "vue";
import { ElMessage } from "element-plus";
import {
  getHealth,
  getStats,
  type HealthResult,
  type StatsResult
} from "@/api/system";
import {
  ageText,
  durText,
  errText,
  statusLabel,
  statusType,
  tsText
} from "@/utils/kk";
import { usePolls, useSeq } from "@/utils/kkPoll";

defineOptions({ name: "SystemStats" });

/**
 * 系统统计（v4 方案 §7.3.7）：把 GET /api/system/stats 的四个组摊开成读数页。
 *
 * 定位不是「实时看板」而是**排障面板**：服务端与桥接的进程内计数器原先只在
 * /api/health 露出两三个，运维要判断「链路活不活 / 命令有没有积压 / 库涨得
 * 快不快」只能翻日志。所以每个计数器都配一句「什么值该担心」——光给数字等于没给。
 */

const stats = ref<StatsResult | null>(null);
const health = ref<HealthResult | null>(null);
const loading = ref(false);
const pollFailed = ref(false);
const lastLoadedAt = ref(0);
// 默认 30s：这页给的是计数与行数，不是秒级新鲜度，而 stats 背后是 5 次 count
// + 3 次聚合，比主机列表贵。
const interval = ref(30);
const { setPoll } = usePolls();
const beginLoad = useSeq();

/** 上一轮的库行数与读数时刻，只为算出「两次刷新之间涨了多少」 */
const prev = ref<{ hb: number; hrs: number; at: number } | null>(null);
const storageDelta = ref<{ hb: number; hourly: number; span: number } | null>(
  null
);

const broker = computed(() => stats.value?.broker ?? null);
const brokerUp = computed(() => Boolean(broker.value?.connected));

/** 四态分开说：没有读数 ≠ 服务端说桥接没启动 ≠ 断了 ≠ 连着（红线：未探测不可当成 0） */
const linkText = computed(() => {
  if (!stats.value) return "链路状态未知";
  if (!broker.value) return "桥接未启动";
  return brokerUp.value ? "Broker 已连接" : "Broker 已断开";
});
/** null = 服务端没有桥对象，与「计数器全是 0」是两件事（红线：未探测 ≠ 0） */
const counters = computed(() => broker.value?.stats ?? null);

const hosts = computed(() => stats.value?.hosts ?? { total: 0, online: 0 });
const offline = computed(() =>
  Math.max(0, hosts.value.total - hosts.value.online)
);

/** 请求失败时这些格子没有读数，填 0 就是造假：0 在这页意味着「服务端报了 0」 */
const dash = (v: number) => (stats.value ? v : "—");

const CMD_ORDER = [
  "pending",
  "queued",
  "sent",
  "running",
  "done",
  "failed",
  "timeout",
  "lost"
];
/** 后端只给有值的键；展示顺序固定，免得每轮刷新位置跳动 */
const cmdRows = computed(() => {
  const by = stats.value?.commands ?? {};
  const extra = Object.keys(by)
    .filter(k => !CMD_ORDER.includes(k))
    .sort();
  return [...CMD_ORDER.filter(k => k in by), ...extra].map(k => ({
    status: k,
    n: by[k]
  }));
});

const cmdInFlight = computed(() => {
  const by = stats.value?.commands ?? {};
  return ["pending", "queued", "sent", "running"].reduce(
    (acc, k) => acc + (by[k] ?? 0),
    0
  );
});

/**
 * 计数器清单。取值口径来自 `services/mqtt_bridge.py` 的累加位置，不在这里臆测。
 * 不在这份清单里的键会被原样补进表格（见 counterRows 的 extra 分支），
 * 但时间戳与刻意压掉的键除外。漏收不会报错，只会把已知计数器显示成「未收录」，
 * 所以动后端 `self.stats` 时必须同步这里。
 */
const COUNTER_META: {
  key: string;
  name: string;
  hint: string;
  alert?: boolean;
}[] = [
  { key: "status", name: "状态帧", hint: "长期为 0：没有 Agent 连上来" },
  {
    key: "hb",
    name: "心跳帧",
    hint: "停增而状态帧还在 = 采集侧断了，链路看着却「在线」"
  },
  {
    key: "result",
    name: "结果帧",
    hint: "停增且有命令在途 = 结果回不来，前端只会看到一直转圈"
  },
  {
    key: "rejected",
    name: "丢弃帧",
    hint: "涨就要查：白名单外 IP / 协议版本不在窗口 / 未知或跨主机的命令 id",
    alert: true
  },
  {
    key: "interval_violation",
    name: "间隔违规",
    hint: "非 0 = 有机器被配成低于服务端下限的上报间隔（500 台规模的唯一信号）",
    alert: true
  },
  {
    key: "cmd_published",
    name: "命令已发布",
    hint: "与结果帧一起看，才知道发出去的和回来的差多少"
  },
  {
    key: "cmd_failed",
    name: "命令发布失败",
    hint: "非 0 = 连主题都没发出去，是 Broker 侧问题，不是 Agent 的问题",
    alert: true
  },
  {
    key: "upgrade_pushed",
    name: "升级已投递",
    hint: "轮询与推送两条路径共用的出口计数"
  },
  {
    key: "upgrade_flushed",
    name: "上线补投",
    hint: "下发时主机离线（queued），到此刻才投出去——离线窗口的正常行为"
  },
  {
    key: "upgrade_done",
    name: "升级回执成功",
    hint: "与「升级已投递」相减就是在途未回；已终态的行不重复计数"
  },
  {
    key: "upgrade_failed",
    name: "升级回执失败",
    hint: "非 0 = 有机器收到了新版本但自更新没成，去看更新台账的 reason",
    alert: true
  },
  {
    key: "sweeps",
    name: "清扫轮次",
    hint: "不涨 = 周期任务没跑，超时命令与僵尸在线都不会再收敛"
  },
  {
    key: "swept_timeouts",
    name: "判超时",
    hint: "短时大量 = 一批 Agent 集体失联，或结果帧丢了"
  },
  {
    key: "swept_offline",
    name: "判离线",
    hint: "超过在线宽限期没帧的主机数，与顶部在线读数互相印证"
  },
  {
    key: "swept_updates",
    name: "升级收敛",
    hint: "丢了回执的在途升级靠它落终态，否则该主机的 in_flight 永久卡住"
  }
];

/** 后端有、但这页刻意**不展示**的键。分两类，理由不同：
 *  - 时间戳不是计数器：last_msg_ts / started_at 已经分别渲染成「最近一帧」和
 *    「已运行」，再列一行原始 epoch 只会让人误读成一个很大的计数；
 *  - proto_v3_received 是「能否关闭 v3 兼容窗口」的唯一依据，而服务端从未累加它
 *    （缺陷账本 QR-S31），恒为 0。照直显示会诱导运维在存量 Agent 还没升完时就关
 *    窗口，那是一次全网闪断。后端把累加补上之后，从这里删掉它就回到表格里。
 */
const SUPPRESSED = ["proto_v3_received", "last_msg_ts", "started_at"];

const counterRows = computed(() => {
  const c = counters.value;
  if (!c) return [];
  const known = COUNTER_META.map(m => ({
    ...m,
    value: c[m.key] ?? 0
  }));
  // 后端新增的键不许悄悄漏掉：原样列出并标明本页还没写说明
  const listed = [...COUNTER_META.map(m => m.key), ...SUPPRESSED];
  const extra = Object.keys(c)
    .filter(k => !listed.includes(k))
    .sort()
    .map(k => ({
      key: k,
      name: k,
      hint: "后端新增的计数器，本页尚未收录说明",
      value: c[k]
    }));
  return [...known, ...extra];
});

const nowSec = () => Math.floor(Date.now() / 1000);

/** 读数只报绝对时刻：computed 里没有秒级响应式时钟，写「N 秒前」会冻在求值那一刻骗人。 */
const syncText = computed(() => {
  if (pollFailed.value) return "刷新失败，数据可能已过期";
  if (!lastLoadedAt.value) return "尚未同步";
  return `已同步 · ${tsText(lastLoadedAt.value)}`;
});

async function load(silent = false) {
  const isLatest = beginLoad();
  if (!silent) loading.value = true;
  try {
    // health 只多给版本号，取失败不该拖垮整页
    const [s, h] = await Promise.all([
      getStats(),
      getHealth().catch(() => null)
    ]);
    if (!isLatest()) return;
    if (prev.value) {
      const span = Math.max(1, nowSec() - prev.value.at);
      storageDelta.value = {
        hb: s.storage.heartbeats - prev.value.hb,
        hourly: s.storage.hourly - prev.value.hrs,
        span
      };
    }
    prev.value = {
      hb: s.storage.heartbeats,
      hrs: s.storage.hourly,
      at: nowSec()
    };
    stats.value = s;
    health.value = h;
    lastLoadedAt.value = nowSec();
    pollFailed.value = false;
  } catch (e: any) {
    if (!isLatest()) return;
    // 与其余轮询页同一条红线：自动刷新失败不刷 toast，只把同步读数变冷（W5）。
    // 手动刷新失败同样要变冷——数据确实过期了，区别只在多一条 toast 说明原因。
    pollFailed.value = true;
    if (!silent) ElMessage.error("加载系统统计失败：" + errText(e));
  } finally {
    if (isLatest()) loading.value = false;
  }
}

function restartTimer() {
  // 0 = 不自动刷新；同名 key 是覆盖语义，不会叠加定时器
  setPoll("system-stats", () => load(true), interval.value * 1000);
}

watch(interval, restartTimer);

onMounted(() => {
  load();
  restartTimer();
});
</script>

<template>
  <div v-loading="loading">
    <el-card shadow="never" class="kk-card">
      <template #header>
        <div class="kk-band">
          <div class="kk-band__fleet">
            <span class="kk-state" :class="{ 'kk-state--idle': !brokerUp }">
              {{ linkText }}
            </span>
            <span class="kk-band__readout">
              主机
              <b>{{ dash(hosts.total) }}</b>
              <span class="kk-sub">在线 {{ dash(hosts.online) }}</span>
            </span>
            <span class="kk-band__readout">
              在途命令
              <b>{{ dash(cmdInFlight) }}</b>
            </span>
            <span
              class="kk-band__readout"
              :class="{
                'kk-band__readout--stale': (stats?.agents_outdated ?? 0) > 0
              }"
            >
              待升级
              <b>{{ stats?.agents_outdated ?? "—" }}</b>
            </span>
            <span class="kk-band__readout">
              最近一帧
              <b>{{ ageText(broker?.last_msg_age_sec) }}</b>
            </span>
          </div>
          <div class="kk-actions">
            <el-select v-model="interval" size="small" class="kk-interval">
              <el-option :value="0" label="停止刷新" />
              <el-option :value="10" label="10 秒" />
              <el-option :value="30" label="30 秒" />
              <el-option :value="60" label="60 秒" />
            </el-select>
            <el-button size="small" class="kk-ml" @click="load(false)">
              立即刷新
            </el-button>
            <span
              class="kk-sync kk-ml"
              :class="{ 'kk-sync--stale': pollFailed }"
            >
              {{ syncText }}
            </span>
          </div>
        </div>
      </template>

      <p class="kk-sub">
        服务端进程内的计数器与库行数，回答三件事：链路活不活、命令有没有积压、
        库涨得快不快。
        <template v-if="health">
          服务端 <b class="kk-num">{{ health.version || "未知" }}</b> · 协议
          <b class="kk-num">v{{ health.proto_ver }}</b>
          · 已运行
          <b class="kk-num">{{ durText(stats?.uptime_sec) }}</b>
        </template>
      </p>
    </el-card>

    <el-row :gutter="16">
      <el-col :xs="24" :md="12">
        <el-card shadow="never" class="kk-card">
          <h4 class="kk-h4">主机与 Agent 版本</h4>
          <el-descriptions :column="1" border size="small" class="kk-desc">
            <el-descriptions-item label="登记主机">
              <span class="kk-num">{{ dash(hosts.total) }}</span>
            </el-descriptions-item>
            <el-descriptions-item label="在线">
              <span class="kk-num">{{ dash(hosts.online) }}</span>
              <span class="kk-sub kk-ml">离线 {{ dash(offline) }}</span>
            </el-descriptions-item>
            <el-descriptions-item label="待分发版本">
              {{
                stats
                  ? stats.agent_latest_ver || "还没上传过版本"
                  : "—（没读到）"
              }}
            </el-descriptions-item>
            <el-descriptions-item label="落后于该版本">
              <span :class="{ 'kk-warn': (stats?.agents_outdated ?? 0) > 0 }">
                {{ stats?.agents_outdated ?? "—" }}
              </span>
              <span class="kk-sub kk-ml">比较由服务端做，前端只报数</span>
            </el-descriptions-item>
            <el-descriptions-item label="升级汇总">
              <span class="kk-kv">
                在途
                <b class="kk-num">{{ dash(stats?.updates?.in_flight ?? 0) }}</b>
              </span>
              <span class="kk-kv">
                完成 <b class="kk-num">{{ dash(stats?.updates?.done ?? 0) }}</b>
              </span>
              <span class="kk-kv">
                失败
                <b class="kk-num">{{ dash(stats?.updates?.failed ?? 0) }}</b>
              </span>
              <span class="kk-kv">
                超时
                <b class="kk-num">{{ dash(stats?.updates?.timeout ?? 0) }}</b>
              </span>
            </el-descriptions-item>
          </el-descriptions>
        </el-card>
      </el-col>

      <el-col :xs="24" :md="12">
        <el-card shadow="never" class="kk-card">
          <h4 class="kk-h4">命令积压（按状态）</h4>
          <div v-if="!stats" class="kk-sub">
            这一次请求没拿到读数，所以这里既不是「没有积压」也不是「全是 0」。
          </div>
          <div v-else-if="!cmdRows.length" class="kk-sub">
            还没有命令下发过。在「命令中心」下发后，这里才会出现状态分布。
          </div>
          <template v-else>
            <span v-for="r in cmdRows" :key="r.status" class="kk-kv">
              <el-tag size="small" :type="statusType(r.status)">
                {{ statusLabel(r.status) }}
              </el-tag>
              <b class="kk-num kk-ml">{{ r.n }}</b>
            </span>
            <p class="kk-sub kk-mt">
              「在途」= 待下发 + 已排队 + 已下发 +
              执行中。这个数字降不下去就是积压， 先去看下面的丢弃帧与判超时。
            </p>
          </template>
        </el-card>
      </el-col>
    </el-row>

    <el-card shadow="never" class="kk-card">
      <h4 class="kk-h4">库行数</h4>
      <span class="kk-kv">
        心跳明细
        <b class="kk-num">{{ stats?.storage?.heartbeats ?? "—" }}</b>
        <span v-if="storageDelta" class="kk-sub kk-ml">
          近 {{ storageDelta.span }}s {{ storageDelta.hb >= 0 ? "+" : ""
          }}{{ storageDelta.hb }} 行
        </span>
      </span>
      <span class="kk-kv">
        小时聚合
        <b class="kk-num">{{ stats?.storage?.hourly ?? "—" }}</b>
        <span v-if="storageDelta" class="kk-sub kk-ml">
          {{ storageDelta.hourly >= 0 ? "+" : "" }}{{ storageDelta.hourly }} 行
        </span>
      </span>
      <p class="kk-sub">
        单次读数看不出增速，所以增量按两次刷新的间隔算。心跳明细涨得比预期快，
        通常是某批机器把上报间隔调小了——对上「间隔违规」那一行就能定位。
      </p>
    </el-card>

    <el-card shadow="never" class="kk-card">
      <h4 class="kk-h4">Broker 计数器</h4>
      <div v-if="!stats" class="kk-sub">
        这一次请求没拿到读数——下面的表因此是空的，不代表计数器全是 0。
      </div>
      <div v-else-if="!counters" class="kk-sub">
        服务端没有 MQTT 桥对象（未装配或还没连上），这组数一个都没有——
        这跟「每个计数器都是 0」不是同一件事。
      </div>
      <el-table v-else :data="counterRows" size="small">
        <el-table-column prop="name" label="计数器" width="120" />
        <el-table-column label="值" width="90">
          <template #default="{ row }">
            <span class="kk-num" :class="{ 'kk-warn': row.alert && row.value }">
              {{ row.value }}
            </span>
          </template>
        </el-table-column>
        <el-table-column prop="hint" label="什么值该担心" min-width="260" />
      </el-table>
      <p class="kk-sub kk-mt">
        表里没有 <b>proto_v3_received</b>，这是刻意的：它是判断「能否关闭 v3
        兼容窗口」的唯一依据，而后端从未累加它（缺陷账本 <b>QR-S31</b>），恒为
        0。照直显示会诱导运维在存量 Agent 还没升完时就关窗口，
        那是一次全网闪断。累加修好之前，这页不给它读数的位置。
      </p>
    </el-card>
  </div>
</template>

<style scoped>
/* 刷新间隔下拉：宽度与其余页一致，但本页不用 el-select 的默认 100% 宽 */
.kk-interval {
  width: 108px;
}
</style>
