<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { ElMessage } from "element-plus";

import { listAudit, parseDetail, type AuditRow } from "@/api/audit";
import { tsText } from "@/utils/kk";

defineOptions({ name: "AuditLog" });

const loading = ref(false);
const rows = ref<AuditRow[]>([]);
const keyword = ref("");
const limit = ref(200);

const filtered = computed(() => {
  const kw = keyword.value.trim().toLowerCase();
  if (!kw) return rows.value;
  return rows.value.filter(
    r =>
      r.actor.toLowerCase().includes(kw) ||
      r.action.toLowerCase().includes(kw) ||
      (r.detail || "").toLowerCase().includes(kw)
  );
});

async function load() {
  loading.value = true;
  try {
    rows.value = (await listAudit(limit.value)).items;
  } catch (e: any) {
    ElMessage.error("加载审计日志失败：" + (e?.message ?? e));
  } finally {
    loading.value = false;
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
          <el-button :loading="loading" @click="load">刷新</el-button>
        </div>
      </div>
    </template>

    <el-table v-loading="loading" :data="filtered" size="small" height="calc(100vh - 240px)">
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
  </el-card>
</template>

<style scoped>
.kk-toolbar {
  display: flex;
  gap: 12px;
  align-items: center;
  justify-content: space-between;
}
.kk-actions {
  display: flex;
  gap: 10px;
  align-items: center;
}
.kk-kv {
  display: inline-block;
  margin-right: 14px;
  font-size: 12px;
  color: #606266;
}
.kk-sub {
  color: #909399;
}
</style>
