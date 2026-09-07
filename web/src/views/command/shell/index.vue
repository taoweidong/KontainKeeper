<script setup lang="ts">
/** 命令面板：shell 命令下发（cmdline / argv 两种输入），下发后历史卡片立即刷新。
 *
 * 执行语义（内网安全可控，不做注入限制，尽量灵活）：
 * - cmdline 恒走 sh -c：管道、重定向、&&、glob、变量展开等全部 shell 语法可用；
 * - argv 恒直 exec（不经 shell）：用于参数含空格等特殊字符的精确执行；
 * 唯一安全约束是服务端黑名单（rm -rf /、mkfs、reboot 等，KK_CMD_BLACKLIST 可配），
 * 两种形态都会按 shell 语义校验；Agent 侧 KK_ALLOW_SHELL=0 可彻底关闭 sh -c。
 */
import { onMounted, reactive, ref } from "vue";
import { useRoute } from "vue-router";
import { ElMessage } from "element-plus";

import { createCommand } from "@/api/commands";
import { listHosts, type HostSummary } from "@/api/containers";
import CommandHistory from "../components/CommandHistory.vue";

defineOptions({ name: "CommandShell" });

const route = useRoute();

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

async function submitShell() {
  if (!shellForm.pods.length) return ElMessage.warning("请选择主机");
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
    history.value?.reload();
  } catch (e: any) {
    ElMessage.error("下发失败：" + errText(e));
  } finally {
    submitting.value = false;
  }
}

onMounted(async () => {
  // 总览页「批量执行命令」跳来时带上预选主机
  const preset = String(route.query.pods || "");
  if (preset) shellForm.pods = preset.split(",").filter(Boolean);
  await loadHosts();
});
</script>

<template>
  <div>
    <el-card shadow="never" class="kk-card">
      <el-form label-width="90px">
        <el-form-item label="目标主机">
          <el-select
            v-model="shellForm.pods"
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
        <el-form-item label="输入方式">
          <el-radio-group v-model="shellForm.mode">
            <el-radio value="cmdline">命令行（经 sh -c，支持全部 shell 语法）</el-radio>
            <el-radio value="argv">argv 数组（不经 shell，精确传参）</el-radio>
          </el-radio-group>
        </el-form-item>
        <el-form-item v-if="shellForm.mode === 'cmdline'" label="命令行">
          <div style="width: 100%">
            <el-input
              v-model="shellForm.cmdline"
              placeholder="与在主机上直接敲命令一致：cd /root && pwd、ps aux | grep node、ls /var/log/*.log 均可"
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
        </el-form-item>
      </el-form>
    </el-card>

    <CommandHistory ref="history" />
  </div>
</template>
