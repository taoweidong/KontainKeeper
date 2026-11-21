<script setup lang="ts">
/** 采集面板：按项勾选指标下发 collect 命令。

与命令面板共用 CommandWorkbench 双栏骨架（A5.3）：左栏换成采集项勾选，
右栏是同一个执行历史（含分页 / 批次 / 导出）。
*/
import { computed, onMounted, reactive, ref } from "vue";
import { useRoute } from "vue-router";
import { message } from "@/utils/message";

import {
  createCommand,
  DEFAULT_COLLECT_ITEMS,
  listCollectItems
} from "@/api/commands";
import { listHosts, type HostSummary } from "@/api/containers";
import { useRenderIcon } from "@/components/ReIcon/src/hooks";
import { COLLECT_ITEM_ICONS, IconRetry, IconSend } from "@/utils/kkIcons";
import { collectItemText, errText } from "@/utils/kk";
import { confirmDispatch } from "@/utils/kkConfirm";
import CommandHistory from "../components/CommandHistory.vue";
import CommandWorkbench from "../components/CommandWorkbench.vue";
import HostPicker from "../components/HostPicker.vue";

defineOptions({ name: "CommandCollect" });

const route = useRoute();

const hosts = ref<HostSummary[]>([]);
/** 主机清单读到过没有：传给 HostPicker 区分「真空机队」与「接口挂了」（QR29-W4） */
const hostsRead = ref(false);
const items = ref<string[]>([]);
/** 采集项清单加载失败：界面必须说「加载失败」并给重试，绝不能摆一份硬编码清单
 *  冒充后端白名单——采后端没有的项，下发出去只会换回一批失败命令（FE-30）。 */
const itemsFailed = ref(false);
/** 清单在途（QR1121）：首屏短暂空白与「清单为空」必须可区分——
 *  loading 期间勾选区给遮罩、下发按钮灰着，不让学生对空气勾选。 */
const itemsLoading = ref(false);
const submitting = ref(false);
const history = ref<InstanceType<typeof CommandHistory>>();

const collectForm = reactive({
  pods: [] as string[],
  items: [] as string[]
});

const picked = computed(() =>
  hosts.value.filter(h => collectForm.pods.includes(h.pod))
);

async function loadHosts() {
  try {
    hosts.value = (await listHosts("summary")).items;
    hostsRead.value = true;
  } catch (e: any) {
    message("加载主机列表失败：" + errText(e), { type: "error" });
  }
}

/** 只在首次成功加载时预勾默认三项；此后重试只剔除后端不再支持的项，
 *  不把用户主动清空的勾选又填满。 */
let prefilled = false;

async function loadItems() {
  itemsLoading.value = true;
  try {
    const avail = (await listCollectItems()).items;
    items.value = avail;
    itemsFailed.value = false;
    collectForm.items = prefilled
      ? collectForm.items.filter(i => avail.includes(i))
      : DEFAULT_COLLECT_ITEMS.filter(i => avail.includes(i));
    prefilled = true;
  } catch (e: any) {
    items.value = [];
    // 预勾的三项属于已失效的清单，留着它们等于凭空下发
    collectForm.items = [];
    itemsFailed.value = true;
    message("加载采集项失败：" + errText(e), { type: "error" });
  } finally {
    itemsLoading.value = false;
  }
}

async function submitCollect() {
  if (!collectForm.pods.length)
    return message("请选择主机", { type: "warning" });
  if (!collectForm.items.length)
    return message("请勾选采集项", { type: "warning" });
  if (!(await confirmDispatch(picked.value, "下发采集"))) return;
  submitting.value = true;
  try {
    const res = await createCommand({
      pods: collectForm.pods,
      kind: "collect",
      items: collectForm.items
    });
    message(`已下发 ${res.items.length} 条采集命令`, { type: "success" });
    history.value?.focusBatch(
      res.batch_id,
      res.items.map(i => i.id)
    );
  } catch (e: any) {
    message("下发失败：" + errText(e), { type: "error" });
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
      <el-form label-width="88px">
        <el-form-item label="目标主机">
          <HostPicker
            v-model:pods="collectForm.pods"
            :hosts="hosts"
            :hosts-read="hostsRead"
          />
        </el-form-item>
        <el-form-item label="采集项">
          <!-- 中文文案 + 类别图标（QR1121-W3）：映射沉 kk.ts，未知项原串兜底；
               title 保留后端原串，排障时能对回白名单。loading 遮罩区分
               「清单在途」与「清单为空」（此前首屏短暂空白不可区分）。 -->
          <el-checkbox-group
            v-model="collectForm.items"
            v-loading="itemsLoading"
            class="kk-collect-items"
          >
            <el-checkbox v-for="it in items" :key="it" :value="it">
              <span class="kk-collect-item" :title="it">
                <el-icon v-if="COLLECT_ITEM_ICONS[it]" class="kk-type-icon">
                  <component :is="COLLECT_ITEM_ICONS[it]" />
                </el-icon>
                {{ collectItemText(it) }}
              </span>
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
            <el-button
              link
              type="primary"
              :icon="useRenderIcon(IconRetry)"
              @click="loadItems"
              >重试</el-button
            >
          </div>
        </el-form-item>
        <el-form-item>
          <!-- disabled 绑勾选数组 collectForm.items，不是可用清单 items：
               清单非空但一项没勾时按钮必须灰着（2026-10-31 点击流测试发现，
               原先绑错对象导致「全不勾仍可点」，只靠 submitCollect 的 toast 兜底）。 -->
          <el-button
            type="primary"
            :icon="useRenderIcon(IconSend)"
            :loading="submitting"
            :disabled="itemsLoading || !collectForm.items.length"
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

<style scoped>
/* 勾选区最小高（QR1121）：清单在途时 v-loading 的 spinner 有落点，
   不与「清单为空」混为一谈。图标口径收编全局 kk-type-icon（QR1123-W10） */
.kk-collect-items {
  min-height: 80px;
}
</style>
