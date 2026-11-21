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
  <span
    v-else
    class="kk-sub kk-menu-icon-raw"
    title="未注册图标，侧边栏不会显示"
    >{{ icon || "-" }}</span
  >
</template>

<style scoped>
/* 长串溢出保护（QR1122-W7）：图标列宽 90 / 弹窗 append 位都不许被撑破；
   文本内容不变（opt1121-rbac 锁「显原串」），title 已给全值与提示 */
.kk-menu-icon-raw {
  display: inline-block;
  overflow: hidden;
  max-width: 100%;
  text-overflow: ellipsis;
  white-space: nowrap;
  vertical-align: bottom;
}
</style>
