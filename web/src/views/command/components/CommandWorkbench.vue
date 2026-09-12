<script setup lang="ts">
/** 命令中心双栏骨架（A5.3）：左执行 / 右结果，一屏完成主链路。

原布局是表单卡片 + 历史表上下堆叠，下发后必须向下滚动才能看到结果；
批量 500 台时历史表很高，来回滚动成本大。改为两栏：
- 左栏 #form：目标主机 / 执行内容 / 下发按钮（各页面自己那半边）
- 右栏 #result：本次下发进度 + 执行历史

窄屏（≤1200px）由 .kk-side 折叠为上下，仍无横向滚动。
*/
defineOptions({ name: "CommandWorkbench" });
</script>

<template>
  <div class="kk-page">
    <div class="kk-side kk-page__body">
      <div class="kk-col kk-col--form">
        <el-card shadow="never" class="kk-card kk-fill">
          <slot name="form" />
        </el-card>
      </div>
      <div class="kk-col kk-col--result">
        <slot name="result" />
      </div>
    </div>
  </div>
</template>

<style scoped>
/* 左窄右宽：表单字段不长（命令输入框除外），结果表需要横向空间 */
.kk-col--form {
  display: flex;
  flex: 0 0 34%;
  min-width: 320px;
  flex-direction: column;
}

.kk-col--result {
  display: flex;
  flex: 1;
  min-width: 0;
  flex-direction: column;
}

.kk-fill {
  display: flex;
  flex: 1;
  flex-direction: column;
  min-height: 0;
}

.kk-fill :deep(.el-card__body) {
  display: flex;
  flex: 1;
  flex-direction: column;
  min-height: 0;
}

@media (max-width: 1200px) {
  .kk-col--form {
    flex: 1 1 auto;
  }
}
</style>
