<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { ElMessage } from "element-plus";

import { listAudit, parseDetail, type AuditRow } from "@/api/audit";
import { exportAudit } from "@/api/exporting";
import { downloadBlob, errText, fileStamp, tsText } from "@/utils/kk";
import { useSeq } from "@/utils/kkPoll";

defineOptions({ name: "AuditLog" });

const loading = ref(false);
const rows = ref<AuditRow[]>([]);
const total = ref(0);
const keyword = ref("");
const limit = ref(200);
const offset = ref(0);

const pageNo = computed({
  get: () => Math.floor(offset.value / limit.value) + 1,
  set: (v: number) => {
    offset.value = (v - 1) * limit.value;
  }
});

/** 分页 / 每页条数 / 关键字防抖 / 刷新按钮都是手动入口：连点两页时迟到的回包会把
 *  后点的那一页盖掉（页码停在 B、列表却是 A），所以赋值前校验这一轮还是不是最新一轮（QR-W4）。 */
const beginLoad = useSeq();

async function load() {
  const isLatest = beginLoad();
  loading.value = true;
  try {
    // 关键字下推到后端（store._audit_filters）：与导出共用同一套语义，所见即所得；
    // 前端过滤会让 total/分页与导出范围对不上（评审 P1）
    const data = await listAudit({
      limit: limit.value,
      offset: offset.value,
      keyword: keyword.value.trim() || undefined
    });
    if (!isLatest()) return;
    rows.value = data.items;
    total.value = data.total;
  } catch (e: any) {
    if (!isLatest()) return;
    ElMessage.error("加载审计日志失败：" + errText(e));
  } finally {
    if (isLatest()) loading.value = false;
  }
}

// 300ms 防抖：输入停顿才触发查询，避免每敲一个字符打一次后端
let kwTimer: ReturnType<typeof setTimeout> | null = null;
watch(keyword, () => {
  if (kwTimer) clearTimeout(kwTimer);
  kwTimer = setTimeout(() => {
    offset.value = 0;
    load();
  }, 300);
});
onBeforeUnmount(() => {
  if (kwTimer) clearTimeout(kwTimer);
});

function onPageChange(p: number) {
  offset.value = (p - 1) * limit.value;
  load();
}

/** 每页条数变化由下面的 watch 统一重置 offset 并重载，这里只赋值——
 *  两处都调 load() 会在一次操作里打两遍后端。 */
function onSizeChange(s: number) {
  limit.value = s;
}

watch(limit, () => {
  offset.value = 0;
  load();
});

const exporting = ref(false);

/** 导出带上当前条数上限与关键字：审计是追溯凭证，导出范围必须与页面一致。 */
async function onExport() {
  exporting.value = true;
  try {
    const blob = await exportAudit({
      keyword: keyword.value.trim() || undefined,
      limit: limit.value
    });
    downloadBlob(blob, `审计日志_${fileStamp()}.csv`);
  } catch (e: any) {
    ElMessage.error("导出失败：" + errText(e));
  } finally {
    exporting.value = false;
  }
}

const actionType = (a: string): "success" | "danger" | "warning" | "info" => {
  if (a.includes("fail") || a.includes("blocked") || a.includes("rejected")) return "danger";
  if (a.includes("mismatch") || a.includes("timeout")) return "warning";
  if (a.includes("ok") || a.includes("create") || a.includes("restore")) return "success";
  return "info";
};

onMounted(load);
</script>

<template>
  <el-card shadow="never" class="kk-card">
    <template #header>
      <div class="kk-toolbar">
        <span><b>审计日志</b></span>
        <div class="kk-actions">
          <el-input
            v-model="keyword"
            placeholder="按操作者 / 动作 / 明细过滤"
            clearable
            style="width: 240px"
          />
          <el-select v-model="limit" style="width: 130px" @change="load">
            <el-option label="最近 100 条" :value="100" />
            <el-option label="最近 200 条" :value="200" />
            <el-option label="最近 500 条" :value="500" />
          </el-select>
          <el-button :loading="loading" @click="load()">刷新</el-button>
          <el-button type="primary" :loading="exporting" @click="onExport">
            导出 CSV
          </el-button>
        </div>
      </div>
    </template>

    <div class="kk-page__body">
      <el-table v-loading="loading" :data="rows" size="small" class="kk-fill-table">
      <el-table-column prop="id" label="#" width="80" />
      <el-table-column label="时间" width="170">
        <template #default="{ row }">{{ tsText(row.ts) }}</template>
      </el-table-column>
      <el-table-column prop="actor" label="操作者" width="140" />
      <el-table-column label="动作" width="180">
        <template #default="{ row }">
          <el-tag :type="actionType(row.action)" size="small">{{ row.action }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="明细" min-width="320">
        <template #default="{ row }">
          <span
            v-for="(v, k) in parseDetail(row.detail)"
            :key="String(k)"
            class="kk-kv"
          >
            <b>{{ k }}</b>: {{ typeof v === "object" ? JSON.stringify(v) : v }}
          </span>
          <span v-if="!row.detail" class="kk-sub">-</span>
        </template>
      </el-table-column>
        <template #empty>
          <el-empty description="暂无审计记录" />
        </template>
      </el-table>
    </div>

    <div class="kk-pager">
      <el-pagination
        v-model:current-page="pageNo"
        :page-size="limit"
        :total="total"
        :page-sizes="[100, 200, 500]"
        layout="total, sizes, prev, pager, next, jumper"
        background
        small
        @size-change="onSizeChange"
        @current-change="onPageChange"
      />
    </div>
  </el-card>
</template>

<!-- 通用类（kk-toolbar/kk-actions/kk-kv/kk-pager/kk-sub）统一在 style/kk.scss -->
