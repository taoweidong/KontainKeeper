<script setup lang="ts">
/** 采集面板：按项勾选指标下发 collect 命令，下发后历史卡片立即刷新。 */
import { onMounted, reactive, ref } from "vue";
import { useRoute } from "vue-router";
import { ElMessage } from "element-plus";

import { createCommand, listCollectItems } from "@/api/commands";
import { listHosts, type HostSummary } from "@/api/containers";
import CommandHistory from "../components/CommandHistory.vue";

defineOptions({ name: "CommandCollect" });

const route = useRoute();

const hosts = ref<HostSummary[]>([]);
const items = ref<string[]>([]);
const submitting = ref(false);
const history = ref<InstanceType<typeof CommandHistory>>();

const collectForm = reactive({ pods: [] as string[], items: ["cpu", "mem", "disk"] as string[] });

async function loadHosts() {
  try {
    hosts.value = (await listHosts("summary")).items;
  } catch (e: any) {
    ElMessage.error("加载主机列表失败：" + (e?.message ?? e));
  }
}

async function loadItems() {
  try {
    items.value = (await listCollectItems()).items;
  } catch {
    items.value = ["cpu", "mem", "disk", "disk_io", "net", "proc", "user", "sys"];
  }
}

function errText(e: any): string {
  return e?.response?.data?.detail ?? e?.message ?? String(e);
}

async function submitCollect() {
  if (!collectForm.pods.length) return ElMessage.warning("请选择主机");
  if (!collectForm.items.length) return ElMessage.warning("请勾选采集项");
  submitting.value = true;
  try {
    const res = await createCommand({
      pods: collectForm.pods,
      kind: "collect",
      items: collectForm.items
    });
    ElMessage.success(`已下发 ${res.items.length} 条采集命令`);
    history.value?.reload();
  } catch (e: any) {
    ElMessage.error("下发失败：" + errText(e));
  } finally {
    submitting.value = false;
  }
}

onMounted(async () => {
  // 总览页「批量采集」跳来时带上预选主机
  const preset = String(route.query.pods || "");
  if (preset) collectForm.pods = preset.split(",").filter(Boolean);
  await Promise.all([loadHosts(), loadItems()]);
});
</script>

<template>
  <div>
    <el-card shadow="never" class="kk-card">
      <el-form label-width="90px">
        <el-form-item label="目标主机">
          <el-select
            v-model="collectForm.pods"
            multiple
            filterable
            collapse-tags
            collapse-tags-tooltip
            placeholder="选择主机（可多选）"
            style="width: 100%"
          >
            <el-option
              v-for="h in hosts"
              :key="h.pod"
              :label="h.online ? h.pod : h.pod + '（离线）'"
              :value="h.pod"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="采集项">
          <el-checkbox-group v-model="collectForm.items">
            <el-checkbox v-for="it in items" :key="it" :label="it" :value="it">
              {{ it }}
            </el-checkbox>
          </el-checkbox-group>
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :loading="submitting" @click="submitCollect">
            下发采集（{{ collectForm.pods.length }} 台）
          </el-button>
        </el-form-item>
      </el-form>
    </el-card>

    <CommandHistory ref="history" />
  </div>
</template>
