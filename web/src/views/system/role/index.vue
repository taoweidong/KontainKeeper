<script setup lang="ts">
/** 角色管理（RBAC）：角色 CRUD + 菜单授权（el-tree 勾选，权限只控菜单）。
 *
 * admin 角色的授权固定为全量（服务端 409 兜底，这里按钮置灰）；
 * 菜单树节点 = 后端菜单表（与前端路由 name 一一对应），勾选即授权。
 */
import { computed, nextTick, onMounted, reactive, ref } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import type { FormInstance, FormRules } from "element-plus";
import type { ElTree } from "element-plus";

import { useRenderIcon } from "@/components/ReIcon/src/hooks";
import {
  IconDelete,
  IconEdit,
  IconPlus,
  IconRetry,
  IconRole
} from "@/utils/kkIcons";
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
/** 读到过真值没有（QR1120-W28 / QR-W16）：接口挂掉时空表不能说「还没有角色」，
 *  内置角色明明存在——那是把故障谎报成真空。 */
const read = ref(false);
/** 加载失败记账（QR1121-W17）：空态里给重试入口，不必整页刷新 */
const loadFailed = ref(false);

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
    read.value = true;
    loadFailed.value = false;
  } catch (e: any) {
    loadFailed.value = true;
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

/** 可见菜单明细（QR1121）：「N 项」hover 列出菜单标题，核对授权不必开弹窗。
 *  菜单表里查不到的 name 原样回显（授权可能指向已改名的历史菜单）。 */
function menuTitles(names: string[]): string {
  const byName = new Map(menus.value.map(m => [m.name, m.title]));
  return names.map(n => byName.get(n) || n).join("、");
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
          try {
            await updateRole(form.name, { menus: menuNames });
          } catch (e: any) {
            // 两段式无原子性（QR1120-W32）：角色已建成、授权这一步挂了。
            // 此时重试「新增」必撞 409，提示必须指向「编辑该角色补授权」，
            // 并关框刷新让那个无授权角色立刻可见可编辑。
            ElMessage.error(
              `角色 ${form.label} 已创建，但菜单授权失败：${errText(e)}。` +
                `请在列表中「编辑」该角色补授权（直接重试新增会撞 409）。`
            );
            dialog.visible = false;
            await load();
            return;
          }
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
  // 二次确认（QR1120-W23）：删角色会连带移除菜单授权，不可逆。
  // 范式对齐 host/update 的回滚确认：取消走 catch 静默返回。
  try {
    await ElMessageBox.confirm(
      `确认删除角色「${row.label}」（${row.name}）？该角色的菜单授权将一并移除，操作不可恢复。`,
      "删除角色",
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
          <b
            ><el-icon class="kk-type-icon"
              ><component :is="useRenderIcon(IconRole)" /></el-icon
            >角色管理</b
          >
          <div class="kk-actions">
            <el-button
              type="primary"
              :icon="useRenderIcon(IconPlus)"
              @click="openCreate"
              >新增角色</el-button
            >
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
        <el-table-column
          prop="remark"
          label="备注"
          min-width="180"
          show-overflow-tooltip
        />
        <el-table-column label="可见菜单" min-width="200">
          <template #default="{ row }">
            <span v-if="row.name === 'admin'" class="kk-sub">全部（固定）</span>
            <span v-else-if="!row.menus.length" class="kk-sub">
              无（该角色登录后侧边栏为空）
            </span>
            <!-- 「N 项」hover 列明细（QR1121）：核对授权不必开弹窗 -->
            <el-tooltip v-else :content="menuTitles(row.menus)" placement="top">
              <span class="kk-num">{{ row.menus.length }} 项</span>
            </el-tooltip>
          </template>
        </el-table-column>
        <el-table-column label="用户数" width="90">
          <template #default="{ row }">
            <span class="kk-num">{{ row.users }}</span>
          </template>
        </el-table-column>
        <el-table-column label="创建时间" width="180">
          <template #default="{ row }">
            <span class="kk-num">{{ tsText(row.created) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="180">
          <template #default="{ row }">
            <el-button
              link
              type="primary"
              :icon="useRenderIcon(IconEdit)"
              :disabled="row.name === 'admin'"
              @click="openEdit(row as RbacRole)"
              >编辑</el-button
            >
            <el-button
              link
              type="danger"
              :icon="useRenderIcon(IconDelete)"
              :disabled="row.built_in || row.users > 0"
              @click="onRemove(row as RbacRole)"
              >删除</el-button
            >
          </template>
        </el-table-column>
        <template #empty>
          <el-empty
            :description="read ? '还没有角色' : '没读到（角色接口不可达）'"
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
        admin
        角色的授权固定为全部菜单，防止把自己改没后无从恢复；内置角色不可删除，
        仍有用户使用的角色须先移走用户。授权改动对已登录用户在下一次刷新后生效。
      </p>
    </el-card>

    <!-- 宽度 min(92vw, Npx)（QR1120-W29）：固定 px 在窄屏会溢出视口 -->
    <el-dialog
      v-model="dialog.visible"
      :title="
        dialog.mode === 'create' ? '新增角色' : `编辑角色 ${dialog.target}`
      "
      width="min(92vw, 520px)"
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
          <!-- 树高超锚点内滚（QR1121）：default-expand-all + 菜单增长后
               弹窗 body 可能被撑出视口（窄屏 92vw 生效时尤其），参照 HostPicker
               抽屉表格的 --kk-drawer-h 范式 -->
          <div class="kk-role-tree">
            <el-tree
              ref="menuTreeRef"
              :data="menuTree"
              node-key="name"
              show-checkbox
              default-expand-all
              :props="{ label: 'label', children: 'children' }"
              style="width: 100%"
            />
          </div>
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

<style scoped>
/* 授权树内滚（QR1121）：高度锚点与抽屉表格同族，数值只在 kk.scss 维护 */
.kk-role-tree {
  width: 100%;
  max-height: var(--kk-drawer-h);
  overflow: auto;
}
</style>
