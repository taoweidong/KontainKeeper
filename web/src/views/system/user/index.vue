<script setup lang="ts">
/** 用户管理（RBAC）：账号 CRUD / 角色分配 / 启停 / 重置密码。
 *
 * 权限只控菜单（见 models/_store_rbac.py），本页本身是 admin 专属——
 * 路由守卫拦非授权角色，接口侧另有 AdminUser 依赖兜底。
 */
import { onMounted, reactive, ref } from "vue";
import { ElMessage } from "element-plus";
import type { FormInstance, FormRules } from "element-plus";

import { errText, tsText } from "@/utils/kk";
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

async function load() {
  loading.value = true;
  try {
    const [u, r] = await Promise.all([listUsers(), listRoles()]);
    users.value = u.items;
    roles.value = r.items;
  } catch (e: any) {
    ElMessage.error("加载用户列表失败：" + errText(e));
  } finally {
    loading.value = false;
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
        ElMessage.success(`已创建用户 ${form.username}`);
      } else {
        await updateUser(form.username, {
          role: form.role,
          enabled: form.enabled,
          ...(form.password ? { password: form.password } : {})
        });
        ElMessage.success(`已更新用户 ${form.username}`);
      }
      dialog.visible = false;
      await load();
    } catch (e: any) {
      ElMessage.error("保存失败：" + errText(e));
    } finally {
      dialog.saving = false;
    }
  });
}

async function onRemove(row: RbacUser) {
  try {
    await deleteUser(row.username);
    ElMessage.success(`已删除用户 ${row.username}`);
    await load();
  } catch (e: any) {
    ElMessage.error("删除失败：" + errText(e));
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
          <b>用户管理</b>
          <div class="kk-actions">
            <el-button type="primary" @click="openCreate">新增用户</el-button>
          </div>
        </div>
      </template>

      <el-table v-loading="loading" :data="users" size="small">
        <el-table-column prop="username" label="用户名" min-width="140" />
        <el-table-column label="角色" width="160">
          <template #default="{ row }">
            <el-tag :type="row.role === 'admin' ? 'danger' : 'info'" size="small">
              {{ row.role_label }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="100">
          <template #default="{ row }">
            <span
              class="kk-state"
              :class="{ 'kk-state--idle': !row.enabled }"
            >
              {{ row.enabled ? "启用" : "已禁用" }}
            </span>
          </template>
        </el-table-column>
        <el-table-column label="创建时间" width="180">
          <template #default="{ row }">{{ tsText(row.created) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="200">
          <template #default="{ row }">
            <el-button link type="primary" @click="openEdit(row as RbacUser)"
              >编辑</el-button
            >
            <el-button
              link
              type="danger"
              :disabled="row.username === me"
              @click="onRemove(row as RbacUser)"
              >删除</el-button
            >
          </template>
        </el-table-column>
        <template #empty>
          <el-empty description="还没有用户" :image-size="60" />
        </template>
      </el-table>
      <p class="kk-sub kk-mt">
        禁用即时生效：登录被拒（403）且已有会话被吊销；最后一个启用的管理员不可
        禁用、降级或删除。
      </p>
    </el-card>

    <el-dialog
      v-model="dialog.visible"
      :title="dialog.mode === 'create' ? '新增用户' : `编辑用户 ${form.username}`"
      width="460px"
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
