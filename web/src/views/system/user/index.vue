<script setup lang="ts">
/** 用户管理（RBAC）：账号 CRUD / 角色分配 / 启停 / 重置密码。
 *
 * 权限只控菜单（见 models/_store_rbac.py），本页本身是 admin 专属——
 * 路由守卫拦非授权角色，接口侧另有 AdminUser 依赖兜底。
 */
import { onMounted, reactive, ref } from "vue";
import { ElMessageBox } from "element-plus";
import { message } from "@/utils/message";
import type { FormInstance, FormRules } from "element-plus";

import { useRenderIcon } from "@/components/ReIcon/src/hooks";
import {
  IconDelete,
  IconEdit,
  IconPlus,
  IconRetry,
  IconUser
} from "@/utils/kkIcons";
import { errText, tsText } from "@/utils/kk";
import { useSeq } from "@/utils/kkPoll";
import { useUserStoreHook } from "@/store/modules/user";
import {
  createUser,
  deleteUser,
  listRoles,
  listUsers,
  updateUser,
  type RbacRole,
  type RbacUser
} from "@/api/rbac";

defineOptions({ name: "SysUser" });

const loading = ref(false);
const users = ref<RbacUser[]>([]);
const roles = ref<RbacRole[]>([]);
const me = ref("");
/** 读到过真值没有（QR1120-W28 / QR-W16）：接口挂掉时空表不能说「还没有用户」——
 *  那是把故障谎报成真空，admin 会以为账号被删光了。 */
const read = ref(false);
/** 加载失败记账（QR1121-W17）：空态里给重试入口，不必整页刷新（collect 页同范式） */
const loadFailed = ref(false);

const dialog = reactive({
  visible: false,
  mode: "create" as "create" | "edit",
  saving: false
});
const form = reactive({
  username: "",
  password: "",
  role: "operator",
  enabled: true
});
const formRef = ref<FormInstance>();
const rules: FormRules = {
  username: [
    { required: true, message: "请输入用户名", trigger: "blur" },
    { min: 2, max: 64, message: "长度 2-64 个字符", trigger: "blur" }
  ],
  password: [
    { required: true, message: "请输入密码", trigger: "blur" },
    { min: 6, message: "至少 6 位", trigger: "blur" }
  ]
};

/** 编辑态的校验口径：用户名只读、密码可留空（不重置） */
const editRules: FormRules = {
  password: [{ min: 6, message: "至少 6 位", trigger: "blur" }]
};

/** 手动请求竞态守卫（QR1130-W1，QR-W4 同族）：连续增删改各自触发 load，
 *  慢响应后到会把列表换成旧快照（刚删的用户「复活」）——只有最新一次可写回。 */
const beginLoad = useSeq();

async function load() {
  const isLatest = beginLoad();
  loading.value = true;
  try {
    const [u, r] = await Promise.all([listUsers(), listRoles()]);
    if (!isLatest()) return;
    users.value = u.items;
    roles.value = r.items;
    read.value = true;
    loadFailed.value = false;
  } catch (e: any) {
    if (!isLatest()) return;
    loadFailed.value = true;
    message("加载用户列表失败：" + errText(e), { type: "error" });
  } finally {
    if (isLatest()) loading.value = false;
  }
}

function openCreate() {
  dialog.mode = "create";
  form.username = "";
  form.password = "";
  form.role = roles.value.some(r => r.name === "operator")
    ? "operator"
    : (roles.value[0]?.name ?? "operator");
  form.enabled = true;
  dialog.visible = true;
}

function openEdit(row: RbacUser) {
  dialog.mode = "edit";
  form.username = row.username;
  form.password = "";
  form.role = row.role;
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
        await createUser({
          username: form.username,
          password: form.password,
          role: form.role
        });
        message(`已创建用户 ${form.username}`, { type: "success" });
      } else {
        await updateUser(form.username, {
          role: form.role,
          enabled: form.enabled,
          ...(form.password ? { password: form.password } : {})
        });
        message(`已更新用户 ${form.username}`, { type: "success" });
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

async function onRemove(row: RbacUser) {
  // 二次确认（QR1120-W23）：删账号是不可逆动作，误点即删没有挽回余地。
  // 范式对齐 host/update 的回滚确认：取消走 catch 静默返回。
  try {
    await ElMessageBox.confirm(
      `确认删除用户「${row.username}」？删除后该账号立即无法登录、已有会话被吊销，操作不可恢复。`,
      "删除用户",
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
    await deleteUser(row.username);
    message(`已删除用户 ${row.username}`, { type: "success" });
    await load();
  } catch (e: any) {
    message("删除失败：" + errText(e), { type: "error" });
  }
}

onMounted(async () => {
  await load();
  // 当前登录名：按钮态用（不能删自己；守卫在接口侧还有一道）
  me.value = useUserStoreHook().username;
});
</script>

<template>
  <div class="kk-page--grow">
    <el-card shadow="never" class="kk-card">
      <template #header>
        <div class="kk-toolbar">
          <b
            ><el-icon class="kk-type-icon"
              ><component :is="useRenderIcon(IconUser)" /></el-icon
            >用户管理</b
          >
          <div class="kk-actions">
            <el-button
              type="primary"
              :icon="useRenderIcon(IconPlus)"
              @click="openCreate"
              >新增用户</el-button
            >
          </div>
        </div>
      </template>

      <el-table v-loading="loading" :data="users" size="small">
        <el-table-column prop="username" label="用户名" min-width="140" />
        <el-table-column label="角色" width="160">
          <template #default="{ row }">
            <!-- admin 用 primary 而非 danger（QR1121）：红色在本站状态语言里
                 专指告警/失败，特权角色是属性不是告警 -->
            <el-tag
              :type="row.role === 'admin' ? 'primary' : 'info'"
              size="small"
            >
              {{ row.role_label }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="100">
          <template #default="{ row }">
            <span class="kk-state" :class="{ 'kk-state--idle': !row.enabled }">
              {{ row.enabled ? "启用" : "已禁用" }}
            </span>
          </template>
        </el-table-column>
        <el-table-column label="创建时间" width="180">
          <template #default="{ row }">
            <span class="kk-num">{{ tsText(row.created) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="200">
          <template #default="{ row }">
            <el-button
              link
              type="primary"
              :icon="useRenderIcon(IconEdit)"
              @click="openEdit(row as RbacUser)"
              >编辑</el-button
            >
            <el-button
              link
              type="danger"
              :icon="useRenderIcon(IconDelete)"
              :disabled="row.username === me"
              @click="onRemove(row as RbacUser)"
              >删除</el-button
            >
          </template>
        </el-table-column>
        <template #empty>
          <el-empty
            :description="read ? '还没有用户' : '没读到（用户接口不可达）'"
            :image-size="60"
          >
            <!-- 失败重试（QR1121-W17）：W28 的三态文案不动，只补行动入口 -->
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
        禁用即时生效：登录被拒（403）且已有会话被吊销；最后一个启用的管理员不可
        禁用、降级或删除。
      </p>
    </el-card>

    <!-- 宽度 min(92vw, Npx)（QR1120-W29）：固定 px 在窄屏会溢出视口 -->
    <el-dialog
      v-model="dialog.visible"
      :title="
        dialog.mode === 'create' ? '新增用户' : `编辑用户 ${form.username}`
      "
      width="min(92vw, 460px)"
      :close-on-click-modal="false"
    >
      <el-form
        ref="formRef"
        :model="form"
        :rules="dialog.mode === 'create' ? rules : editRules"
        label-width="90px"
      >
        <el-form-item label="用户名" prop="username">
          <el-input
            v-model="form.username"
            :disabled="dialog.mode === 'edit'"
            placeholder="登录账号"
          />
        </el-form-item>
        <el-form-item
          label="密码"
          prop="password"
          :required="dialog.mode === 'create'"
        >
          <el-input
            v-model="form.password"
            type="password"
            show-password
            :placeholder="
              dialog.mode === 'create' ? '至少 6 位' : '留空 = 不修改密码'
            "
          />
        </el-form-item>
        <el-form-item label="角色" prop="role">
          <el-select v-model="form.role" style="width: 100%">
            <el-option
              v-for="r in roles"
              :key="r.name"
              :label="`${r.label}（${r.name}）`"
              :value="r.name"
            />
          </el-select>
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
