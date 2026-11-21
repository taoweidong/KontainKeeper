<script setup lang="ts">
import { useBoolean } from "../../hooks/useBoolean";
import { IconSearch } from "@/utils/kkIcons";
import SearchModal from "./components/SearchModal.vue";

const { bool: show, toggle } = useBoolean();
function handleSearch() {
  toggle();
}
</script>

<template>
  <div>
    <div
      class="search-container w-[40px] h-[48px] flex-c cursor-pointer navbar-bg-hover"
      @click="handleSearch"
    >
      <!-- 词汇表收口（QR1122-W16）：原字符串图标绕过 kkIcons，同图标换组件导入 -->
      <IconifyIconOffline :icon="IconSearch" />
    </div>
    <SearchModal v-model:value="show" />
  </div>
</template>
