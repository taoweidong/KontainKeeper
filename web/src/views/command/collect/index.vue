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
import { errText } from "@/utils/kk";
import { confirmDispatch } from "@/utils/kkConfirm";
import CommandHistory from "../components/CommandHistory.vue";
import CommandWorkbench from "../components/CommandWorkbench.vue";
import HostPicker from "../components/HostPicker.vue";

defineOptions({ name: "CommandCollect" });

const route = useRoute();

const hosts = ref<HostSummary[]>([]);
const items = ref<string[]>([]);
/** 采集项清单加载失败：界面必须说「加载失败」并给重试，绝不能摆一份硬编码清单
 *  冒充后端白名单——采后端没有的项，下发出去只会换回一批失败命令（FE-30）。 */
const itemsFailed = ref(false);
const submitting = ref(false);
const history = ref<InstanceType<typeof CommandHistory>>();

const collectForm = reactive({
  pods: [] as string[],
  items: [] as string[]
});

/** 默认勾选的三项：只在后端确实支持时才预勾，不作为失败兜底 */
const DEFAULT_ITEMS = ["cpu", "mem", "disk"];

const picked = computed(() =>
  hosts.value.filter(h => collectForm.pods.includes(h.pod))
);

async function loadHosts() {
  try {
    hosts.value = (await listHosts("summary")).items;
  } catch (e: any) {
    ElMessage.error("加载主机列表失败：" + (e?.message ?? e));
  }
}

/** 只在首次成功加载时预勾默认三项；此后重试只剔除后端不再支持的项，
 *  不把用户主动清空的勾选又填满。 */
let prefilled = false;

async function loadItems() {
  try {
    const avail = (await listCollectItems()).items;
    items.value = avail;
    itemsFailed.value = false;
    collectForm.items = prefilled
      ? collectForm.items.filter(i => avail.includes(i))
      : DEFAULT_ITEMS.filter(i => avail.includes(i));
    prefilled = true;
  } catch (e: any) {
    items.value = [];
    // 预勾的三项属于已失效的清单，留着它们等于凭空下发
    collectForm.items = [];
    itemsFailed.value = true;
    ElMessage.error("加载采集项失败：" + errText(e));
  }
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
    history.value?.focusBatch(
      res.batch_id,
      res.items.map(i => i.id)
    );
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
          <HostPicker v-model:pods="collectForm.pods" :hosts="hosts" />
        </el-form-item>
        <el-form-item label="采集项">
          <el-checkbox-group v-model="collectForm.items">
            <el-checkbox v-for="it in items" :key="it" :value="it">
              {{ it }}
            </el-checkbox>
          </el-checkbox-group>
          <!-- el-form-item__content 是 flex 容器，不给 width:100% 这条读数会和
               勾选区挤在同一行（读不到、也难看）。 -->
          <div
            v-if="itemsFailed"
            class="kk-sync kk-sync--stale kk-mt"
            style="width: 100%"
          >
            采集项清单加载失败，下发范围无法校验。
            <el-button link type="primary" @click="loadItems">重试</el-button>
          </div>
        </el-form-item>
        <el-form-item>
          <el-button
            type="primary"
            :loading="submitting"
            :disabled="!items.length"
            @click="submitCollect"
          >
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
