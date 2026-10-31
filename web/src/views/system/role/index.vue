<script setup lang="ts">
/** 角色管理（RBAC）：角色 CRUD + 菜单授权（el-tree 勾选，权限只控菜单）。
 *
 * admin 角色的授权固定为全量（服务端 409 兜底，这里按钮置灰）；
 * 菜单树节点 = 后端菜单表（与前端路由 name 一一对应），勾选即授权。
 */
import { computed, nextTick, onMounted, reactive, ref } from "vue";
import { ElMessage } from "element-plus";
import type { FormInstance, FormRules } from "element-plus";
import type { ElTree } from "element-plus";

import { errText, tsText } from "@/utils/kk";
import {
  createRole,
  deleteRole,
  listMenus,
  listRoles,
  updateRole,
  type RbacMenu,
  type RbacRole
} from "@/api/rbac";

defineOptions({ name: "SysRole" });

const loading = ref(false);
const roles = ref<RbacRole[]>([]);
const menus = ref<RbacMenu[]>([]);

const dialog = reactive({
  visible: false,
  mode: "create" as "create" | "edit",
  saving: false,
  target: "" as string
});
const form = reactive({
  name: "",
  label: "",
  remark: ""
});
const formRef = ref<FormInstance>();
const rules: FormRules = {
  name: [
    { required: true, message: "请输入角色标识", trigger: "blur" },
    {
      pattern: /^[A-Za-z0-9_-]{2,32}$/,
      message: "字母/数字/-/_，长度 2-32",
      trigger: "blur"
    }
  ],
  label: [{ required: true, message: "请输入角色名", trigger: "blur" }]
};

const menuTreeRef = ref<InstanceType<typeof ElTree>>();

/** 菜单表 → 树（el-tree 的 data）；叶子缺失的父节点也要给占位以便勾选 */
const menuTree = computed(() => {
  const byParent = new Map<string, RbacMenu[]>();
  for (const m of menus.value) {
    const list = byParent.get(m.parent) ?? [];
    list.push(m);
    byParent.set(m.parent, list);
  }
  const build = (parent: string): any[] =>
    (byParent.get(parent) ?? []).map(m => ({
      ...m,
      label: `${m.title}（${m.name}）`,
      children: build(m.name)
    }));
  return build("");
});

async function load() {
  loading.value = true;
  try {
    const [r, m] = await Promise.all([listRoles(), listMenus()]);
    roles.value = r.items;
    menus.value = m.items;
  } catch (e: any) {
    ElMessage.error("加载角色列表失败：" + errText(e));
  } finally {
    loading.value = false;
  }
}

function openCreate() {
  dialog.mode = "create";
  dialog.target = "";
  form.name = "";
  form.label = "";
  form.remark = "";
  dialog.visible = true;
  resetTree([]);
}

function openEdit(row: RbacRole) {
  dialog.mode = "edit";
  dialog.target = row.name;
  form.name = row.name;
  form.label = row.label;
  form.remark = row.remark;
  dialog.visible = true;
  resetTree(row.menus);
}

function resetTree(checked: string[]) {
  void nextTick(() => {
    menuTreeRef.value?.setCheckedKeys(checked, false);
  });
}

async function submit() {
  const el = formRef.value;
  if (!el) return;
  await el.validate(async valid => {
    if (!valid) return;
    const checked = menuTreeRef.value?.getCheckedKeys(false) as string[];
    const half = menuTreeRef.value?.getHalfCheckedKeys() as string[];
    const menuNames = [...new Set([...checked, ...half])];
    dialog.saving = true;
    try {
      if (dialog.mode === "create") {
        await createRole({
          name: form.name,
          label: form.label,
          remark: form.remark
        });
        if (menuNames.length) {
          await updateRole(form.name, { menus: menuNames });
        }
        ElMessage.success(`已创建角色 ${form.label}`);
      } else {
        await updateRole(dialog.target, {
          label: form.label,
          remark: form.remark,
          menus: menuNames
        });
        ElMessage.success(`已更新角色 ${form.label}`);
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

async function onRemove(row: RbacRole) {
  try {
    await deleteRole(row.name);
    ElMessage.success(`已删除角色 ${row.label}`);
    await load();
  } catch (e: any) {
    ElMessage.error("删除失败：" + errText(e));
  }
}

onMounted(load);
</script>

<template>
  <div class="kk-page--grow">
    <el-card shadow="never" class="kk-card">
      <template #header>
        <div class="kk-toolbar">
          <b>角色管理</b>
          <div class="kk-actions">
            <el-button type="primary" @click="openCreate">新增角色</el-button>
          </div>
        </div>
      </template>

      <el-table v-loading="loading" :data="roles" size="small">
        <el-table-column label="角色" min-width="120">
          <template #default="{ row }">
            {{ row.label }}
            <el-tag v-if="row.built_in" size="small" type="info">内置</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="name" label="标识" width="140" />
        <el-table-column prop="remark" label="备注" min-width="180" />
        <el-table-column label="可见菜单" min-width="200">
          <template #default="{ row }">
            <span v-if="row.name === 'admin'" class="kk-sub">全部（固定）</span>
            <span v-else-if="!row.menus.length" class="kk-sub">
              无（该角色登录后侧边栏为空）
            </span>
            <span v-else>{{ row.menus.length }} 项</span>
          </template>
        </el-table-column>
        <el-table-column prop="users" label="用户数" width="90" />
        <el-table-column label="创建时间" width="180">
          <template #default="{ row }">{{ tsText(row.created) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="180">
          <template #default="{ row }">
            <el-button
              link
              type="primary"
              :disabled="row.name === 'admin'"
              @click="openEdit(row as RbacRole)"
              >编辑</el-button
            >
            <el-button
              link
              type="danger"
              :disabled="row.built_in || row.users > 0"
              @click="onRemove(row as RbacRole)"
              >删除</el-button
            >
          </template>
        </el-table-column>
        <template #empty>
          <el-empty description="还没有角色" :image-size="60" />
        </template>
      </el-table>
      <p class="kk-sub kk-mt">
        admin 角色的授权固定为全部菜单，防止把自己改没后无从恢复；内置角色不可删除，
        仍有用户使用的角色须先移走用户。授权改动对已登录用户在下一次刷新后生效。
      </p>
    </el-card>

    <el-dialog
      v-model="dialog.visible"
      :title="dialog.mode === 'create' ? '新增角色' : `编辑角色 ${dialog.target}`"
      width="520px"
      :close-on-click-modal="false"
    >
      <el-form
        ref="formRef"
        :model="form"
        :rules="rules"
        label-width="90px"
        :disabled="dialog.mode === 'edit' && dialog.target === 'admin'"
      >
        <el-form-item label="角色标识" prop="name">
          <el-input
            v-model="form.name"
            :disabled="dialog.mode === 'edit'"
            placeholder="字母/数字/-/_，用于接口关联"
          />
        </el-form-item>
        <el-form-item label="角色名" prop="label">
          <el-input v-model="form.label" placeholder="界面显示名，如：运维" />
        </el-form-item>
        <el-form-item label="备注" prop="remark">
          <el-input v-model="form.remark" type="textarea" :rows="2" />
        </el-form-item>
        <el-form-item label="菜单授权">
          <el-tree
            ref="menuTreeRef"
            :data="menuTree"
            node-key="name"
            show-checkbox
            default-expand-all
            :props="{ label: 'label', children: 'children' }"
            style="width: 100%"
          />
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
