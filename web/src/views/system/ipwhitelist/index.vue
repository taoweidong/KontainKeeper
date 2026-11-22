<script setup lang="ts">
/** IP 白名单管理（QR1124）：网段条目 CRUD / 启停 / 备注。
 *
 * 权限只控菜单（见 models/_store_rbac.py），本页 admin 专属——路由守卫拦非授权
 * 角色，接口侧另有 AdminUser 依赖兜底。env KK_AGENT_IPS 为启动种子，DB 为运行时
 * 真相源，变更即时生效无需重启（服务端 refresh_agent_ips 刷新桥接与 REST 快照）。
 * 空白名单 = 放行所有上报（ip_in_whitelist 开发语义）：停用/删除最后一条启用规则
 * 时显式警告，生产请至少保留一条。
 */
import { computed, onMounted, reactive, ref } from "vue";
import { ElMessageBox } from "element-plus";
import { message } from "@/utils/message";
import type { FormInstance, FormRules } from "element-plus";

import { useRenderIcon } from "@/components/ReIcon/src/hooks";
import {
  IconDelete,
  IconEdit,
  IconLock,
  IconPlus,
  IconRetry
} from "@/utils/kkIcons";
import { errText, tsText } from "@/utils/kk";
import { useSeq } from "@/utils/kkPoll";
import {
  createIpRule,
  deleteIpRule,
  listIpRules,
  updateIpRule,
  type IpRule
} from "@/api/ipwhitelist";

defineOptions({ name: "SysIpWhitelist" });

const loading = ref(false);
const rules = ref<IpRule[]>([]);
/** 读到过真值没有（QR-W16）：接口挂掉时空表不能说「还没有规则」 */
const read = ref(false);
const loadFailed = ref(false);

const enabledCount = computed(() => rules.value.filter(r => r.enabled).length);

const dialog = reactive({
  visible: false,
  mode: "create" as "create" | "edit",
  saving: false
});
const editingId = ref(0);
const form = reactive({ cidr: "", remark: "", enabled: true });
const formRef = ref<FormInstance>();

/** CIDR 轻量格式校验（UX 即时反馈；服务端 parse_ip_whitelist 才是权威闸）。
 *  认 IPv4(/0-32) 与 IPv6(/0-128) 的常见形态，单 IP 不带掩码也放行。 */
function isValidCidr(s: string): boolean {
  const v = s.trim();
  if (!v) return false;
  const [addr, prefix, ...rest] = v.split("/");
  if (rest.length) return false;
  const v4 = /^(\d{1,3}\.){3}\d{1,3}$/;
  const v6 = /^[0-9a-fA-F:]+$/;
  if (v4.test(addr)) {
    if (!addr.split(".").every(o => +o <= 255)) return false;
    return prefix === undefined || (+prefix >= 0 && +prefix <= 32);
  }
  if (v6.test(addr) && addr.includes(":")) {
    return prefix === undefined || (+prefix >= 0 && +prefix <= 128);
  }
  return false;
}

const rules_ = {
  cidr: [
    { required: true, message: "请输入 IP 或 CIDR", trigger: "blur" },
    {
      validator: (_r: any, v: string, cb: (e?: Error) => void) =>
        isValidCidr(v)
          ? cb()
          : cb(new Error("格式非法，如 10.0.0.0/24 或 192.168.1.5")),
      trigger: "blur"
    }
  ]
} as FormRules;

/** 手动请求竞态守卫（QR1130-W1，QR-W4 同族）：连续增删/启停各自触发 load，
 *  慢响应后到会把规则表换成旧快照（刚停用的规则「复活」）——只有最新一次可写回。 */
const beginLoad = useSeq();

async function load() {
  const isLatest = beginLoad();
  loading.value = true;
  try {
    // enabled 归一为布尔：后端返回整型 1/0，el-switch 的 :model-value 收到
    // 非布尔值会在初始化时误触发 change（弹出「停用规则」确认），必须转布尔
    const items = (await listIpRules()).items.map(r => ({
      ...r,
      enabled: !!r.enabled
    }));
    if (!isLatest()) return;
    rules.value = items;
    read.value = true;
    loadFailed.value = false;
  } catch (e: any) {
    if (!isLatest()) return;
    loadFailed.value = true;
    message("加载白名单失败：" + errText(e), { type: "error" });
  } finally {
    if (isLatest()) loading.value = false;
  }
}

function openCreate() {
  dialog.mode = "create";
  editingId.value = 0;
  form.cidr = "";
  form.remark = "";
  form.enabled = true;
  dialog.visible = true;
}

function openEdit(row: IpRule) {
  dialog.mode = "edit";
  editingId.value = row.id;
  form.cidr = row.cidr;
  form.remark = row.remark;
  form.enabled = row.enabled;
  dialog.visible = true;
}

async function submit() {
  const el = formRef.value;
  if (!el) return;
  await el.validate(async valid => {
    if (!valid) return;
    dialog.saving = true;
    try {
      if (dialog.mode === "create") {
        await createIpRule({ cidr: form.cidr.trim(), remark: form.remark });
        message(`已添加白名单 ${form.cidr.trim()}`, { type: "success" });
      } else {
        await updateIpRule(editingId.value, {
          remark: form.remark,
          enabled: form.enabled
        });
        message("已更新白名单规则", { type: "success" });
      }
      dialog.visible = false;
      await load();
    } catch (e: any) {
      message("保存失败：" + errText(e), { type: "error" });
    } finally {
      dialog.saving = false;
    }
  });
}

/** 表格内快速启停：停用最后一条启用规则前警告「将放行所有上报」。
 *  switch 用 :model-value 单向绑定，取消时 row.enabled 不变、开关不回弹。 */
async function onToggle(row: IpRule, val: boolean) {
  if (!val && enabledCount.value <= 1) {
    try {
      await ElMessageBox.confirm(
        "停用最后一条启用规则后白名单为空，将放行所有上报（仅开发/测试适用）。确认停用？",
        "停用规则",
        {
          type: "warning",
          confirmButtonText: "确认停用",
          cancelButtonText: "取消"
        }
      );
    } catch {
      return;
    }
  }
  try {
    await updateIpRule(row.id, { enabled: val });
    message(val ? "已启用规则" : "已停用规则", { type: "success" });
    await load();
  } catch (e: any) {
    message("操作失败：" + errText(e), { type: "error" });
  }
}

async function onRemove(row: IpRule) {
  const lastEnabled = row.enabled && enabledCount.value <= 1;
  try {
    await ElMessageBox.confirm(
      `确认删除白名单条目「${row.cidr}」？删除后该网段 Agent 立即被拒。` +
        (lastEnabled
          ? "这是最后一条启用规则，删除后白名单为空将放行所有上报。"
          : ""),
      "删除规则",
      {
        type: "warning",
        confirmButtonText: "确认删除",
        cancelButtonText: "取消"
      }
    );
  } catch {
    return;
  }
  try {
    await deleteIpRule(row.id);
    message(`已删除白名单 ${row.cidr}`, { type: "success" });
    await load();
  } catch (e: any) {
    message("删除失败：" + errText(e), { type: "error" });
  }
}

/** 范围标签：单 IP（/32 或 /128 或无掩码）vs 网段 */
function scopeLabel(cidr: string): string {
  const [addr, prefix] = cidr.split("/");
  const single =
    prefix === undefined ||
    (addr.includes(":") ? prefix === "128" : prefix === "32");
  return single ? "单 IP" : "网段";
}

onMounted(load);
</script>

<template>
  <div class="kk-page--grow">
    <el-card shadow="never" class="kk-card">
      <template #header>
        <div class="kk-toolbar">
          <b
            ><el-icon class="kk-type-icon"
              ><component :is="useRenderIcon(IconLock)" /></el-icon
            >IP 白名单</b
          >
          <div class="kk-actions">
            <el-button
              type="primary"
              :icon="useRenderIcon(IconPlus)"
              @click="openCreate"
              >新增规则</el-button
            >
          </div>
        </div>
      </template>

      <el-table v-loading="loading" :data="rules" size="small">
        <el-table-column label="IP / CIDR" min-width="160">
          <template #default="{ row }">
            <span class="kk-num">{{ row.cidr }}</span>
          </template>
        </el-table-column>
        <el-table-column label="范围" width="90">
          <template #default="{ row }">
            <el-tag
              :type="scopeLabel(row.cidr) === '单 IP' ? 'info' : 'primary'"
              size="small"
            >
              {{ scopeLabel(row.cidr) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="启用" width="90">
          <template #default="{ row }">
            <el-switch
              :model-value="row.enabled"
              @change="v => onToggle(row as IpRule, Boolean(v))"
            />
          </template>
        </el-table-column>
        <el-table-column
          prop="remark"
          label="备注"
          min-width="160"
          show-overflow-tooltip
        />
        <el-table-column label="创建时间" width="180">
          <template #default="{ row }">
            <span class="kk-num">{{ tsText(row.created) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="160">
          <template #default="{ row }">
            <el-button
              link
              type="primary"
              :icon="useRenderIcon(IconEdit)"
              @click="openEdit(row as IpRule)"
              >编辑</el-button
            >
            <el-button
              link
              type="danger"
              :icon="useRenderIcon(IconDelete)"
              @click="onRemove(row as IpRule)"
              >删除</el-button
            >
          </template>
        </el-table-column>
        <template #empty>
          <el-empty
            :description="
              read ? '还没有白名单规则' : '没读到（白名单接口不可达）'
            "
            :image-size="60"
          >
            <el-button
              v-if="loadFailed && !loading"
              :icon="useRenderIcon(IconRetry)"
              @click="load()"
              >重试</el-button
            >
          </el-empty>
        </template>
      </el-table>
      <p class="kk-sub kk-mt">
        env KK_AGENT_IPS 为启动种子，此处为运行时真相源，变更即时生效无需重启。
        空白名单 = 放行所有上报（仅开发/测试）；生产请至少保留一条启用规则。
      </p>
    </el-card>

    <el-dialog
      v-model="dialog.visible"
      :title="
        dialog.mode === 'create' ? '新增白名单规则' : `编辑规则 ${form.cidr}`
      "
      width="min(92vw, 460px)"
      :close-on-click-modal="false"
    >
      <el-form ref="formRef" :model="form" :rules="rules_" label-width="90px">
        <el-form-item label="IP / CIDR" prop="cidr">
          <el-input
            v-model="form.cidr"
            :disabled="dialog.mode === 'edit'"
            placeholder="如 10.0.0.0/24 或 192.168.1.5"
          />
        </el-form-item>
        <el-form-item label="备注">
          <el-input v-model="form.remark" placeholder="用途说明（可选）" />
        </el-form-item>
        <el-form-item v-if="dialog.mode === 'edit'" label="启用">
          <el-switch v-model="form.enabled" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialog.visible = false">取消</el-button>
        <el-button type="primary" :loading="dialog.saving" @click="submit">
          保存
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>
