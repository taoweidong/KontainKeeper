<script setup lang="ts">
/** 命令中心双栏骨架（A5.3）：左执行 / 右结果，一屏完成主链路。

原布局是表单卡片 + 历史表上下堆叠，下发后必须向下滚动才能看到结果；
批量 500 台时历史表很高，来回滚动成本大。改为两栏：
- 左栏 #form：目标主机 / 执行内容 / 下发按钮（各页面自己那半边）
- 右栏 #result：本次下发进度 + 执行历史

右栏**不要求子元素撑高**：#result 那半边（CommandHistory）是开源列表页范式，
表格高度由 @pureadmin/table 的 adaptive 按视口自己量（见 style/kk.scss 的 .kk-main），
所以这里只给它一个不裁剪、可收缩的容器，不再期待它像左栏卡片那样被 flex 拉满。
左栏保留 el-card（表单需要卡片观感），`#form` / `#result` 的插槽约定不变。

窄屏（≤1200px）由 .kk-side 折叠为上下，仍无横向滚动。
*/
defineOptions({ name: "CommandWorkbench" });
</script>

<template>
  <div class="kk-page">
    <div class="kk-side kk-workbench__side">
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
/* 双栏容器占满页面剩余高度。原先这半边由共挂的 .kk-page__body 提供，但它同时带来
   `flex-direction: column`，与全局 .kk-side 的 row 同特异度、只能靠本文件的源序决胜
   （kk.scss 里那段「挪动顺序就把 P0-1 请回来」的层叠说明随这次摘除一起删除）。
   摘掉后方向只剩 .kk-side 一处来源，这里只补它缺的那半边：填充 + 允许收缩。 */
.kk-workbench__side {
  flex: 1;
  min-height: 0;
}

/* 左窄右宽：表单字段不长（命令输入框除外），结果表需要横向空间 */
.kk-col--form {
  display: flex;
  flex: 0 0 34%;
  min-width: 320px;
  flex-direction: column;
}

/* min-width: 0 是让这一栏真能收缩到 65.7% 而非被表格内容顶宽的承重项，勿删 */
.kk-col--result {
  display: flex;
  flex: 1;
  min-width: 0;
  flex-direction: column;
}

/* 只作用于左栏卡片：让表单卡跟着栏高填满，卡片下方不留空洞 */
.kk-fill {
  display: flex;
  flex: 1;
  flex-direction: column;
  min-height: 0;
}

/* 同上，仍是左栏专属：右栏没有 .kk-fill，也不该有（它的高度由 adaptive 自己定） */
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
