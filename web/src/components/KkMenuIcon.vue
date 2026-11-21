<script setup lang="ts">
import { useRenderIcon } from "@/components/ReIcon/src/hooks";
import { menuIconKnown } from "@/utils/kkIcons";

/** 菜单 icon 的离线安全预览（QR1121）：菜单管理页表格列与弹窗共用。
 *  已知（addIcon 注册过、不含 `:`）才走渲染链；未知字符串显原串 + 提示，
 *  绝不递给 useRenderIcon——含 `:` 会触发 IconifyIconOnline 运行时联网。 */
defineProps<{ icon: string }>();
</script>

<template>
  <el-icon v-if="menuIconKnown(icon)">
    <component :is="useRenderIcon(icon)" />
  </el-icon>
  <span v-else class="kk-sub" title="未注册图标，侧边栏不会显示">{{
    icon || "-"
  }}</span>
</template>
