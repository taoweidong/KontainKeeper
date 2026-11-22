<script setup lang="ts">
/** 菜单管理（RBAC）：编辑菜单展示元数据（标题/图标/排序/侧边栏显隐）与全局启停。
 *
 * 范围裁决（models/_store_rbac.py）：菜单树与前端静态路由一一对应，**不支持
 * 新建/删除菜单**——新建的菜单指向不存在的页面只会得到 404。这里改的是
 * 「怎么显示、给谁显示的开关」，不是「有哪些页面」。
 */
import { computed, onMounted, reactive, ref } from "vue";
import { message } from "@/utils/message";

import KkMenuIcon from "@/components/KkMenuIcon.vue";
import { useRenderIcon } from "@/components/ReIcon/src/hooks";
import { IconEdit, IconMenu, IconRetry } from "@/utils/kkIcons";
import { errText } from "@/utils/kk";
import { useSeq } from "@/utils/kkPoll";
import { listMenus, updateMenu, type RbacMenu } from "@/api/rbac";

defineOptions({ name: "SysMenu" });

const loading = ref(false);
const menus = ref<RbacMenu[]>([]);
/** 读到过真值没有（QR1120-W28 / QR-W16）：菜单表有内置种子，正常部署不可能为空——
 *  接口挂掉时显「菜单表为空」是把故障谎报成真空。 */
const read = ref(false);
/** 加载失败记账（QR1121-W17）：空态里给重试入口，不必整页刷新 */
const loadFailed = ref(false);

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

/** 手动请求竞态守卫（QR1130-W1，QR-W4 同族）：连续编辑/启停各自触发 load，
 *  慢响应后到会把菜单表换成旧快照——只有最新一次可写回。 */
const beginLoad = useSeq();

async function load() {
  const isLatest = beginLoad();
  loading.value = true;
  try {
    const items = (await listMenus()).items;
    if (!isLatest()) return;
    menus.value = items;
    read.value = true;
    loadFailed.value = false;
  } catch (e: any) {
    if (!isLatest()) return;
    loadFailed.value = true;
    message("加载菜单失败：" + errText(e), { type: "error" });
  } finally {
    if (isLatest()) loading.value = false;
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
    message("标题不能为空", { type: "warning" });
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
    message(`已更新菜单 ${dialog.target}`, { type: "success" });
    dialog.visible = false;
    await load();
  } catch (e: any) {
    message("保存失败：" + errText(e), { type: "error" });
  } finally {
    dialog.saving = false;
  }
}

onMounted(load);
</script>

<template>
  <div class="kk-page--grow kk-page--fill">
    <el-card shadow="never" class="kk-card">
      <template #header>
        <div class="kk-toolbar">
          <b
            ><el-icon class="kk-type-icon"
              ><component :is="useRenderIcon(IconMenu)" /></el-icon
            >菜单管理</b
          >
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
            <!-- tag 堆叠间距收口四档（QR1121）：最多三枚不再贴边 -->
            <el-tag v-if="row.built_in" class="kk-ml" size="small" type="info"
              >内置</el-tag
            >
            <!-- 停用是管理动作不是告警（QR1122-W13）：空心灰点，不挪用 danger 红 -->
            <span v-if="!row.enabled" class="kk-state kk-state--idle kk-ml"
              >已停用</span
            >
            <!-- 隐藏页是属性不是告警（QR1123-W11）：warning 琥珀→info 中性，
                 与 QR1122-W13「已停用」裁决同理 -->
            <el-tag
              v-else-if="!row.show_link"
              class="kk-ml"
              size="small"
              type="info"
            >
              不出侧边栏
            </el-tag>
          </template>
        </el-table-column>
        <!-- 图标预览列（QR1121）：icon 字段此前不渲染，填错字符串侧边栏才暴露；
             KkMenuIcon 离线白名单闸——已知显图标，未知显原串 + 提示 -->
        <el-table-column label="图标" width="90" show-overflow-tooltip>
          <template #default="{ row }">
            <KkMenuIcon :icon="row.icon" />
          </template>
        </el-table-column>
        <el-table-column
          prop="name"
          label="标识（路由 name）"
          min-width="140"
        />
        <el-table-column prop="path" label="路由路径" min-width="150" />
        <el-table-column
          prop="component"
          label="组件"
          min-width="180"
          show-overflow-tooltip
        />
        <el-table-column label="排序" width="70">
          <template #default="{ row }">
            <span class="kk-num">{{ row.rank }}</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="100">
          <template #default="{ row }">
            <el-button
              link
              type="primary"
              :icon="useRenderIcon(IconEdit)"
              @click="openEdit(row as RbacMenu)"
              >编辑</el-button
            >
          </template>
        </el-table-column>
        <template #empty>
          <el-empty
            :description="read ? '菜单表为空' : '没读到（菜单接口不可达）'"
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
        「不出侧边栏」的页面（主机详情 / 使用趋势）仍可通过列表跳转直达；
        「停用」对所有人隐藏（含 admin）。标识与内置标记不可修改——它们是
        前端路由的 name，改了就对不上页面。
      </p>
    </el-card>

    <!-- 宽度 min(92vw, Npx)（QR1120-W29）：固定 px 在窄屏会溢出视口 -->
    <el-dialog
      v-model="dialog.visible"
      :title="`编辑菜单 ${dialog.target}`"
      width="min(92vw, 480px)"
      :close-on-click-modal="false"
    >
      <!-- label-width 与 user/role 统一 90px（QR1121）；只读契约字段收为一行
           纯文本（原两行 disabled input 信息密度低），path/component 不可改的
           裁决不变 -->
      <el-form label-width="90px">
        <el-form-item label="标识 / 组件">
          <span class="kk-num kk-sub"
            >{{ dialog.target }} / {{ editingComponent }}</span
          >
        </el-form-item>
        <el-form-item label="标题">
          <el-input v-model="form.title" />
        </el-form-item>
        <el-form-item label="图标">
          <!-- 实时预览（QR1121）：输入框 append 位挂 KkMenuIcon，填错当场可见 -->
          <el-input
            v-model="form.icon"
            placeholder="如：ep/monitor（分组图标）"
          >
            <template #append>
              <KkMenuIcon :icon="form.icon" />
            </template>
          </el-input>
          <div class="kk-sub kk-icon-hint">
            仅已注册图标会在侧边栏渲染；离线部署不支持在线图标（含 : 的字符串）
          </div>
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

<style scoped>
/* 图标说明独占一行（QR1121）：el-form-item__content 是 flex 容器，
   不给 width:100% 会和输入框挤在同一行（collect 页失败横幅同族坑） */
.kk-icon-hint {
  width: 100%;
  margin-top: var(--kk-space-sm);
}
</style>
