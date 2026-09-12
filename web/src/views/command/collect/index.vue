<script setup lang="ts">
/** 采集面板：按项勾选指标下发 collect 命令。

与命令面板共用 CommandWorkbench 双栏骨架（A5.3）：左栏换成采集项勾选，
右栏是同一个执行历史（含分页 / 批次 / 导出）。
*/
import { computed, onMounted, reactive, ref } from "vue";
import { useRoute } from "vue-router";
import { ElMessage } from "element-plus";

import { createCommand, listCollectItems } from "@/api/commands";
import { listHosts, type HostSummary } from "@/api/containers";
import { confirmDispatch } from "@/utils/kkConfirm";
import CommandHistory from "../components/CommandHistory.vue";
import CommandWorkbench from "../components/CommandWorkbench.vue";
import HostPicker from "../components/HostPicker.vue";

defineOptions({ name: "CommandCollect" });

const route = useRoute();

const hosts = ref<HostSummary[]>([]);
const items = ref<string[]>([]);
const submitting = ref(false);
const history = ref<InstanceType<typeof CommandHistory>>();

const collectForm = reactive({
  pods: [] as string[],
  items: ["cpu", "mem", "disk"] as string[]
});

const picked = computed(() => hosts.value.filter(h => collectForm.pods.includes(h.pod)));

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
  if (!(await confirmDispatch(picked.value, "下发采集"))) return;
  submitting.value = true;
  try {
    const res = await createCommand({
      pods: collectForm.pods,
      kind: "collect",
      items: collectForm.items
    });
    ElMessage.success(`已下发 ${res.items.length} 条采集命令`);
    history.value?.focusBatch(res.batch_id, res.items.map(i => i.id));
  } catch (e: any) {
    ElMessage.error("下发失败：" + errText(e));
  } finally {
    submitting.value = false;
  }
}

onMounted(async () => {
  // 总览页「批量采集」跳来时带上预选主机（?pods= 契约不变）
  const preset = String(route.query.pods || "");
  if (preset) collectForm.pods = preset.split(",").filter(Boolean);
  await Promise.all([loadHosts(), loadItems()]);
});
</script>

<template>
  <CommandWorkbench>
    <template #form>
      <el-form label-width="90px">
        <el-form-item label="目标主机">
          <HostPicker v-model:pods="collectForm.pods" />
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
    </template>

    <template #result>
      <CommandHistory ref="history" />
    </template>
  </CommandWorkbench>
</template>
