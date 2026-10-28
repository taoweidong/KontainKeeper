<script setup lang="ts">
/** 版本与更新（D2.4）：待分发版本的上传/回滚 + 落后主机一键升级（推送） + 升级台账。

把「上传新版本」前后要做的所有事拢到一屏：
1. 顶部读数栏：服务端当前待分发版本、落后台数、最近上传时间与轮询状态；
2. 升级台账（最近 50 条）置顶、行数少——**自然高度 + max-height 320 封顶，不套
   PureTableBar、不开 adaptive**（本页两张表同屏的结构裁决，详见 utils/hook.tsx 头注
   与下方模板注释：两张都 adaptive 会按视口互相抢高度，前一张撑开后一张就溢出）；
3. 下方主表是「落后主机清单」，勾选（跨页保留）后经「批量升级」推送目标版本；
4. 上传走对话框（本仓 addDialog 的第一次真实调用）、回滚走二次确认——都留回执：
   上传回显版本/sha256，回滚回显互换后的当前版本，升级回显受理与逐台跳过原因。
*/
import { reactive } from "vue";
import { ElMessageBox } from "element-plus";
import type { FormInstance } from "element-plus";

import { rollbackAgent, uploadAgent } from "@/api/agent";
import { ageText, errText, mbText } from "@/utils/kk";
import { message } from "@/utils/message";
import { PureTableBar } from "@/components/RePureTableBar";
import { addDialog } from "@/components/ReDialog";
import { useRenderIcon } from "@/components/ReIcon/src/hooks";
import UploadIcon from "~icons/ep/upload";
import RollbackIcon from "~icons/ep/refresh-left";
import UpgradeIcon from "~icons/ep/arrow-up";

import UploadFormView from "./form.vue";
import { useHostUpdate } from "./utils/hook";

defineOptions({ name: "HostUpdate" });

const {
  loading,
  current,
  outdated,
  pagedOutdated,
  updates,
  interval,
  selection,
  offlineSelected,
  upgrading,
  pollFailed,
  read,
  ledgerRead,
  hasLatest,
  fleetInfo,
  tableRef,
  columns,
  ledgerColumns,
  pagination,
  load,
  onSelect,
  onUpgrade,
  selectAllOutdated,
  clearSelection
} = useHostUpdate();

/** 上传对话框模型：对话框每次打开都重置，半填状态不跨次残留 */
const uploadForm = reactive({
  version: "",
  file: null as File | null,
  fileName: "",
  size: 0
});

/** el-form 实例由内容体（./form.vue）挂载时经 registerForm 递回来：
 *  addDialog 的 beforeSure 只拿得到 options，拿不到组件树。 */
let uploadFormEl: FormInstance | undefined;

function openUploadDialog() {
  uploadForm.version = "";
  uploadForm.file = null;
  uploadForm.fileName = "";
  uploadForm.size = 0;
  addDialog({
    title: "上传新版本",
    width: "560px",
    /** —— DialogProps 验证条目（Task 3 遗留、本任务补验的渲染缺口）——
     *  modalClass（v7 移植净增的 12 个之一，type.ts 声明「遮罩的自定义类名」）：
     *  声明时挂在遮罩 .el-overlay 元素上——浏览器里以「带该类的遮罩存在」实测；
     *  draggable：标题栏可拖动（mousedown→mousemove 位移），浏览器实测；
     *  closeOnClickModal：置 false 后点遮罩**不**关闭，防误点丢掉半填表单
     *  （上传写二进制，比一次普通确认贵）。成功/失败行为均在 task-8 报告记录。 */
    modalClass: "kk-upload-modal",
    draggable: true,
    closeOnClickModal: false,
    /** 确定按钮走上传网络请求：开 loading，成功才关对话框 */
    sureBtnLoading: true,
    props: {
      form: uploadForm,
      registerForm: (el?: FormInstance) => {
        uploadFormEl = el;
      }
    },
    contentRenderer: () => UploadFormView,
    beforeSure: (done, { closeLoading }) => {
      if (!uploadFormEl) {
        closeLoading?.();
        return;
      }
      uploadFormEl.validate(async valid => {
        if (!valid) {
          // 校验拦截：不调 done()，对话框保持打开，字段红字指路
          closeLoading?.();
          return;
        }
        if (!uploadForm.file) {
          message("请先选择二进制文件", { type: "warning" });
          closeLoading?.();
          return;
        }
        try {
          const r = await uploadAgent(uploadForm.file, uploadForm.version);
          message(
            `已上传 ${r.version}（${mbText(r.size / 1024 / 1024)} · sha256 ${r.sha256.slice(0, 12)}…），待分发版本已切换`,
            { type: "success" }
          );
          done();
          // 回执之后立刻刷读数：页头当前版本与落后台数要当场对上（不等下一轮轮询）
          load();
        } catch (e: any) {
          // 失败留在对话框里可改可重试——上传是运维动作，关掉重来成本高
          message("上传失败：" + errText(e), { type: "error" });
          closeLoading?.();
        }
      });
    }
  });
}

/** 回滚待分发版本：当前 ↔ 上一版互换，再调一次即撤销（后端 B2 语义，原话进确认框）。 */
async function onRollback() {
  const cur = current.value?.version || "";
  if (!cur) {
    message("还没有待分发版本，无从回滚", { type: "warning" });
    return;
  }
  try {
    await ElMessageBox.confirm(
      `把待分发版本 ${cur} 与上一版互换。只影响**新上线或重启的 Agent**——` +
        "已在跑的不会因此降级（版本比较只升不降）；再点一次回滚即撤销本次互换。确认执行？",
      "回滚待分发版本",
      { type: "warning", confirmButtonText: "确认回滚" }
    );
  } catch {
    return;
  }
  try {
    const r = await rollbackAgent();
    message(`已回滚：待分发版本现为 ${r.version}（此前是 ${cur}）`, {
      type: "success"
    });
    load();
  } catch (e: any) {
    // 404「没有可回滚的版本」也是回执的一种：后端说没有 .prev，就让它这么显示
    message("回滚失败：" + errText(e), { type: "error" });
  }
}
</script>

<template>
  <div v-loading="loading" class="kk-main">
    <!-- 页头读数条（W5：pollFailed 进这条读数、不弹 toast；与搜索条同构挂 kk-search-form） -->
    <div class="kk-search-form bg-bg_color w-full overflow-auto pl-8 pt-3 pb-1">
      <div class="kk-toolbar">
        <div class="kk-stat">
          <span
            >当前版本 <b>{{ fleetInfo.version }}</b></span
          >
          <span>
            落后主机
            <b :class="fleetInfo.cls">
              {{ fleetInfo.outdated }}
            </b>
            / {{ fleetInfo.total }}</span
          >
          <span v-if="current?.uploaded_at" class="kk-sub">
            {{
              ageText(
                Math.max(0, Math.floor(Date.now() / 1000) - current.uploaded_at)
              )
            }}上传
          </span>
          <span class="kk-sync" :class="{ 'kk-sync--stale': pollFailed }">
            {{
              pollFailed
                ? "自动刷新失败，读数可能已过期"
                : `每 ${interval} 秒自动刷新`
            }}
          </span>
        </div>
        <div class="kk-actions">
          <el-select v-model="interval" style="width: 120px">
            <el-option label="5 秒" :value="5" />
            <el-option label="10 秒" :value="10" />
            <el-option label="30 秒" :value="30" />
          </el-select>
        </div>
      </div>
    </div>

    <!-- 升级台账（Step 1 裁决：上方、行数少、自然高度 max-height 320 封顶；
         不套 PureTableBar、不开 adaptive——同屏两张表都 adaptive 会互相抢视口高度。
         「主机/状态」与下方主表同名**不冲突**：列设置唯一性只需在同一 bar 内成立 -->
    <div class="bg-bg_color w-full mb-2 pl-8 pr-8 pt-3 pb-3">
      <div class="kk-toolbar">
        <b>升级台账（最近 50 条）</b>
      </div>
      <pure-table
        :data="updates"
        :columns="ledgerColumns"
        row-key="id"
        size="small"
        align-whole="center"
        table-layout="auto"
        :max-height="320"
        :header-cell-style="{
          background: 'var(--el-fill-color-light)',
          color: 'var(--el-text-color-primary)'
        }"
      >
        <template #empty>
          {{ ledgerRead ? "暂无升级记录" : "没读到（升级台账接口不可达）" }}
        </template>
      </pure-table>
    </div>

    <!-- 选中横幅：取代 .kk-sticky-bar；按 kk.scss 的配对说明**摘掉了 .kk-batch**
         （两条同挂会把 12px(顶)+8px(底) 叠在一个元素上、间距翻倍）。
         批量升级（推送）按钮按 Step 2 上移到下方 bar 的 #buttons -->
    <div class="kk-select-banner">
      <span class="kk-sub">已选 {{ selection.length }} 台</span>
      <el-button :disabled="!outdated.length" @click="selectAllOutdated">
        选中所有落后主机
      </el-button>
      <el-button :disabled="!selection.length" @click="clearSelection">
        清空选择
      </el-button>
      <el-tooltip
        v-if="offlineSelected"
        placement="top"
        content="选中的离线主机将标记为 queued，桥接在它们上线后自动补投——无需重试"
      >
        <el-tag type="info" size="small"
          >含 {{ offlineSelected }} 台离线</el-tag
        >
      </el-tooltip>
    </div>

    <PureTableBar
      title="待升级主机（落后于待分发版本）"
      :columns="columns"
      :table-ref="tableRef?.getTableRef()"
      @refresh="load()"
    >
      <template #buttons>
        <el-button
          type="primary"
          :icon="useRenderIcon(UploadIcon)"
          @click="openUploadDialog"
          >上传新版本</el-button
        >
        <el-button
          :icon="useRenderIcon(RollbackIcon)"
          :disabled="!hasLatest"
          @click="onRollback"
          >回滚待分发</el-button
        >
        <el-button
          type="warning"
          :icon="useRenderIcon(UpgradeIcon)"
          :disabled="!selection.length || !hasLatest"
          :loading="upgrading"
          @click="onUpgrade"
          >批量升级</el-button
        >
      </template>
      <template v-slot="{ size, dynamicColumns }">
        <!-- 结构偏离说明（两张表同屏，Task 8 Step 1 裁决，别当硬编码遗留删）：
             台账表在上、用自然高度 + max-height 320 封顶（不开 adaptive）；
             本主表在下方开 adaptive——它按 innerHeight - wrapper.top - offsetBottom
             定高，台账把它下推得越多它越矮，两张都 adaptive 时第二张的 top 依赖
             第一张的实渲高度、互相抢视口（P1-1/R7 的成因）。
             offsetBottom 是本页实测值 132（1920×1080 与 1366×768 两档复测，
             pager.bottom 996≤999 / 684≤687；台账/横幅/搜索条一改高就得复测）。 -->
        <!-- :pagination 必须直连 reactive，不能写 { ...pagination, size }：
             @pureadmin/table 对传入对象**就地**写 currentPage/pageSize，展开成
             一次性快照后写入无人读 → 翻页全废。本页是前端分页：pure-table 写的
             currentPage 由 pagedOutdated 这个 computed 消费（切片即翻页，不打后端），
             所以模板也不绑 @page-current-change——没有任何东西需要重新请求。
             勾选跨页由 row-key=pod + 勾选列 reserveSelection 保住，
             @select/@select-all 的第一参数是表格全量勾选集合（含其他页）。 -->
        <pure-table
          ref="tableRef"
          row-key="pod"
          adaptive
          :adaptive-config="{ offsetBottom: 132 }"
          align-whole="center"
          table-layout="auto"
          :size="size"
          :data="pagedOutdated"
          :columns="dynamicColumns"
          :pagination="pagination"
          :header-cell-style="{
            background: 'var(--el-fill-color-light)',
            color: 'var(--el-text-color-primary)'
          }"
          @select="onSelect"
          @select-all="onSelect"
        >
          <template #empty>
            <el-empty
              class="min-h-200"
              :image-size="60"
              :description="
                !read
                  ? '没读到（主机与版本接口不可达）'
                  : hasLatest
                    ? '所有主机都是最新版本'
                    : '还没有待分发版本，先去上传 Agent 二进制'
              "
            />
          </template>
        </pure-table>
      </template>
    </PureTableBar>
  </div>
</template>
