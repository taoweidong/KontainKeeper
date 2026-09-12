<script setup lang="ts">
/** 命令面板：shell 命令下发（cmdline / argv 两种输入）。

执行语义（内网安全可控，不做注入限制，尽量灵活）：
- cmdline 恒走 sh -c：管道、重定向、&&、glob、变量展开等全部 shell 语法可用；
- argv 恒直 exec（不经 shell）：用于参数含空格等特殊字符的精确执行；
唯一安全约束是服务端黑名单（rm -rf /、mkfs、reboot 等，KK_CMD_BLACKLIST 可配），
两种形态都会按 shell 语义校验；Agent 侧 KK_ALLOW_SHELL=0 可彻底关闭 sh -c。

布局（A5.3）：左执行 / 右结果，下发后结果就在同一屏，不需要滚动。
*/
import { computed, onMounted, reactive, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { ElMessage } from "element-plus";

import { createCommand } from "@/api/commands";
import { listHosts, type HostSummary } from "@/api/containers";
import { confirmDispatch } from "@/utils/kkConfirm";
import CommandHistory from "../components/CommandHistory.vue";
import CommandWorkbench from "../components/CommandWorkbench.vue";
import HostPicker from "../components/HostPicker.vue";

defineOptions({ name: "CommandShell" });

const route = useRoute();
const router = useRouter();

const hosts = ref<HostSummary[]>([]);
const submitting = ref(false);
const history = ref<InstanceType<typeof CommandHistory>>();

const shellForm = reactive({
  pods: [] as string[],
  mode: "cmdline" as "cmdline" | "argv",
  cmdline: "",
  argvText: "",
  timeout: 30
});

/** 选中主机的在线情况：确认框要如实告诉用户离线主机会排队补投 */
const picked = computed(() => hosts.value.filter(h => shellForm.pods.includes(h.pod)));

async function loadHosts() {
  try {
    hosts.value = (await listHosts("summary")).items;
  } catch (e: any) {
    ElMessage.error("加载主机列表失败：" + (e?.message ?? e));
  }
}

function errText(e: any): string {
  return e?.response?.data?.detail ?? e?.message ?? String(e);
}

/** 输入状态同步到 query（replace 不污染后退栈）：刷新与分享不丢 */
watch(
  () => ({ ...shellForm }),
  v => {
    router.replace({
      query: {
        ...route.query,
        pods: v.pods.join(",") || undefined,
        mode: v.mode,
        cmdline: v.cmdline || undefined,
        timeout: String(v.timeout)
      }
    });
  },
  { deep: true }
);

async function submitShell() {
  if (!shellForm.pods.length) return ElMessage.warning("请选择主机");
  if (!(await confirmDispatch(picked.value))) return;
  submitting.value = true;
  try {
    const body: Record<string, any> = {
      pods: shellForm.pods,
      kind: "shell",
      timeout: shellForm.timeout
    };
    if (shellForm.mode === "argv") {
      const argv = shellForm.argvText
        .split("\n")
        .map(s => s.trim())
        .filter(Boolean);
      if (!argv.length) return ElMessage.warning("argv 模式至少填一行参数");
      body.argv = argv;
      body.use_shell = false;
    } else {
      if (!shellForm.cmdline.trim()) return ElMessage.warning("请填写命令行");
      body.cmdline = shellForm.cmdline.trim();
      body.use_shell = true;
    }
    const res = await createCommand(body as any);
    ElMessage.success(`已下发 ${res.items.length} 条命令`);
    // 定位到该批次：下发即看到进度，不需要滚动去找
    history.value?.focusBatch(res.batch_id, res.items.map(i => i.id));
  } catch (e: any) {
    ElMessage.error("下发失败：" + errText(e));
  } finally {
    submitting.value = false;
  }
}

onMounted(async () => {
  // 总览页「批量执行命令」跳来时带上预选主机（?pods= 契约不变）
  const q = route.query;
  const preset = String(q.pods || "");
  if (preset) shellForm.pods = preset.split(",").filter(Boolean);
  if (q.mode === "argv") shellForm.mode = "argv";
  if (q.cmdline) shellForm.cmdline = String(q.cmdline);
  if (q.timeout) shellForm.timeout = Number(q.timeout) || 30;
  await loadHosts();
});
</script>

<template>
  <CommandWorkbench>
    <template #form>
      <el-form label-width="90px">
        <el-form-item label="目标主机">
          <HostPicker v-model:pods="shellForm.pods" />
        </el-form-item>
        <el-form-item label="输入方式">
          <el-radio-group v-model="shellForm.mode">
            <el-radio value="cmdline">命令行（经 sh -c）</el-radio>
            <el-radio value="argv">argv 数组（不经 shell）</el-radio>
          </el-radio-group>
        </el-form-item>
        <el-form-item v-if="shellForm.mode === 'cmdline'" label="命令行">
          <div style="width: 100%">
            <el-input
              v-model="shellForm.cmdline"
              placeholder="与在主机上直接敲命令一致：cd /root && pwd、ps aux | grep node 均可"
              @keyup.enter="submitShell"
            />
            <div class="kk-sub">
              整条命令经 sh -c 执行，支持管道 / 重定向 / && / 变量展开；
              仅 rm -rf /、mkfs、reboot 等危险命令会被服务端黑名单拦截
            </div>
          </div>
        </el-form-item>
        <el-form-item v-else label="argv">
          <el-input
            v-model="shellForm.argvText"
            type="textarea"
            :rows="3"
            placeholder="每行一个参数，例如：&#10;/bin/ls&#10;-lh&#10;/var/log"
          />
        </el-form-item>
        <el-form-item label="超时">
          <el-input-number v-model="shellForm.timeout" :min="1" :max="600" /> 秒
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :loading="submitting" @click="submitShell">
            下发命令（{{ shellForm.pods.length }} 台）
          </el-button>
          <div class="kk-sub">Ctrl / Cmd + Enter 可直接下发</div>
        </el-form-item>
      </el-form>
    </template>

    <template #result>
      <CommandHistory ref="history" />
    </template>
  </CommandWorkbench>
</template>
