<script setup lang="ts">
/** 菜单管理（RBAC）：编辑菜单展示元数据（标题/图标/排序/侧边栏显隐）与全局启停。
 *
 * 范围裁决（models/_store_rbac.py）：菜单树与前端静态路由一一对应，**不支持
 * 新建/删除菜单**——新建的菜单指向不存在的页面只会得到 404。这里改的是
 * 「怎么显示、给谁显示的开关」，不是「有哪些页面」。
 */
import { computed, onMounted, reactive, ref } from "vue";
import { ElMessage } from "element-plus";

import { errText } from "@/utils/kk";
import { listMenus, updateMenu, type RbacMenu } from "@/api/rbac";

defineOptions({ name: "SysMenu" });

const loading = ref(false);
const menus = ref<RbacMenu[]>([]);

const dialog = reactive({
  visible: false,
  saving: false,
  target: ""
});
const form = reactive({
  title: "",
  icon: "",
  rank: 99,
  show_link: true,
  enabled: true
});

/** 平表 → 树（el-table 的 tree-props 渲染）。children 只在非空时挂：
 *  空数组会让叶子行也出现展开箭头，展开后是一块「无数据」。 */
const menuTree = computed(() => {
  const byParent = new Map<string, RbacMenu[]>();
  for (const m of menus.value) {
    const list = byParent.get(m.parent) ?? [];
    list.push(m);
    byParent.set(m.parent, list);
  }
  const build = (parent: string): any[] =>
    (byParent.get(parent) ?? []).map(m => {
      const children = build(m.name);
      return children.length ? { ...m, children } : { ...m };
    });
  return build("");
});

const editingComponent = computed(
  () => menus.value.find(m => m.name === dialog.target)?.component ?? ""
);

async function load() {
  loading.value = true;
  try {
    menus.value = (await listMenus()).items;
  } catch (e: any) {
    ElMessage.error("加载菜单失败：" + errText(e));
  } finally {
    loading.value = false;
  }
}

function openEdit(row: RbacMenu) {
  dialog.target = row.name;
  form.title = row.title;
  form.icon = row.icon;
  form.rank = row.rank;
  form.show_link = row.show_link;
  form.enabled = row.enabled;
  dialog.visible = true;
}

async function submit() {
  if (!form.title.trim()) {
    ElMessage.warning("标题不能为空");
    return;
  }
  dialog.saving = true;
  try {
    await updateMenu(dialog.target, {
      title: form.title.trim(),
      icon: form.icon.trim(),
      rank: form.rank,
      show_link: form.show_link,
      enabled: form.enabled
    });
    ElMessage.success(`已更新菜单 ${dialog.target}`);
    dialog.visible = false;
    await load();
  } catch (e: any) {
    ElMessage.error("保存失败：" + errText(e));
  } finally {
    dialog.saving = false;
  }
}

onMounted(load);
</script>

<template>
  <div class="kk-page--grow">
    <el-card shadow="never" class="kk-card">
      <template #header>
        <div class="kk-toolbar">
          <b>菜单管理</b>
          <span class="kk-sub">
            改标题 / 图标 / 排序 / 显隐与启停；角色↔菜单的分配在「角色管理」页
          </span>
        </div>
      </template>

      <el-table
        v-loading="loading"
        :data="menuTree"
        size="small"
        row-key="name"
        :tree-props="{ children: 'children' }"
        default-expand-all
      >
        <el-table-column label="标题" min-width="200">
          <template #default="{ row }">
            {{ row.title }}
            <el-tag v-if="row.built_in" size="small" type="info">内置</el-tag>
            <el-tag v-if="!row.enabled" size="small" type="danger">已停用</el-tag>
            <el-tag v-else-if="!row.show_link" size="small" type="warning">
              不出侧边栏
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="name" label="标识（路由 name）" min-width="140" />
        <el-table-column prop="path" label="路由路径" min-width="150" />
        <el-table-column prop="component" label="组件" min-width="180" show-overflow-tooltip />
        <el-table-column prop="rank" label="排序" width="70" />
        <el-table-column label="操作" width="100">
          <template #default="{ row }">
            <el-button link type="primary" @click="openEdit(row as RbacMenu)"
              >编辑</el-button
            >
          </template>
        </el-table-column>
        <template #empty>
          <el-empty description="菜单表为空" :image-size="60" />
        </template>
      </el-table>
      <p class="kk-sub kk-mt">
        「不出侧边栏」的页面（主机详情 / 使用趋势）仍可通过列表跳转直达；
        「停用」对所有人隐藏（含 admin）。标识与内置标记不可修改——它们是
        前端路由的 name，改了就对不上页面。
      </p>
    </el-card>

    <el-dialog
      v-model="dialog.visible"
      :title="`编辑菜单 ${dialog.target}`"
      width="480px"
      :close-on-click-modal="false"
    >
      <el-form label-width="110px">
        <el-form-item label="标识 / 路径">
          <el-input :model-value="dialog.target" disabled />
        </el-form-item>
        <el-form-item label="组件">
          <!-- 只读：path/component 是路由契约，改它们必须与前端 views 同步 -->
          <el-input :model-value="editingComponent" disabled />
        </el-form-item>
        <el-form-item label="标题">
          <el-input v-model="form.title" />
        </el-form-item>
        <el-form-item label="图标">
          <el-input v-model="form.icon" placeholder="如：ep/monitor（分组图标）" />
        </el-form-item>
        <el-form-item label="排序">
          <el-input-number v-model="form.rank" :min="0" :max="999" />
        </el-form-item>
        <el-form-item label="侧边栏显示">
          <el-switch v-model="form.show_link" />
        </el-form-item>
        <el-form-item label="启用">
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
