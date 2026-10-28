<script setup lang="ts">
/** 上传对话框内容体——本仓第一个经 `addDialog` 真实渲染的表单组件。
 *
 * 与 index.vue 的分工：这里只管**字段与校验**（rules 逐字镜像后端契约
 * `agent_update.py::upload_agent`——版本号首字符必须是数字，别的都不拦），
 * 提交动作留在 index.vue 的 `beforeSure` 里：校验不过就不调 `done()`，
 * 对话框保持打开、字段红字指路——这正是 Task 3 留下、本任务负责补验的
 * `beforeSure` 拦截路径。
 *
 * el-form 实例经 `registerForm` 回调交给父级：`addDialog` 的 `beforeSure`
 * 只拿得到 options，拿不到组件树，所以由内容体在挂载时把自己递出去；
 * watchEffect 让「挂载赋值 / 卸载清空」都同步（destroyOnClose 之类卸载路径
 * 若漏清，父级持有的就是死实例）。
 */
import { ref, watchEffect } from "vue";
import type { FormInstance, FormRules, UploadUserFile } from "element-plus";
import { message } from "@/utils/message";
import { mbText } from "@/utils/kk";

const props = defineProps<{
  /** 上传表单模型：版本 + 选中的二进制（真相留在父级，关闭/重开不残留半填状态） */
  form: {
    version: string;
    file: File | null;
    fileName: string;
    size: number;
  };
  /** 把 el-form 实例递给父级，供 beforeSure 跑 validate */
  registerForm?: (el?: FormInstance) => void;
}>();

/** 后端 MAX_BIN_BYTES = 64MB：前端先说，省一次注定 413 的往返 */
const MAX_BYTES = 64 * 1024 * 1024;

const formEl = ref<FormInstance>();
const fileList = ref<UploadUserFile[]>([]);

watchEffect(() => props.registerForm?.(formEl.value));

const rules: FormRules = {
  version: [
    { required: true, message: "请填写版本号", trigger: "blur" },
    {
      // 后端 upload_agent 只校验 version[0].isdigit()——规则逐字对齐，
      // 不发明更严的 semver 校验（版本号写法由运维自己定，拦太狠是添乱）
      pattern: /^\d[\w.+-]*$/,
      message: "版本号须以数字开头（如 1.2.3；带 v 前缀会被后端拒绝）",
      trigger: "blur"
    }
  ],
  file: [
    { required: true, message: "请选择 Agent 二进制文件", trigger: "change" }
  ]
};

const form = props.form;

/** 单槽位选择：el-upload 的多选队列在这里没有意义，最新一次选择覆盖旧值 */
function onChange(file: UploadUserFile, files: UploadUserFile[]) {
  const raw = file.raw;
  if (!raw) return;
  if (raw.size > MAX_BYTES) {
    message(`文件 ${file.name} 超过 64MB 上限，后端会直接 413，不用等它`, {
      type: "error"
    });
    fileList.value = files.filter(f => f.uid !== file.uid);
    return;
  }
  fileList.value = [file];
  form.file = raw;
  form.fileName = file.name;
  form.size = raw.size;
  formEl.value?.validateField("file");
}

function onRemove() {
  form.file = null;
  form.fileName = "";
  form.size = 0;
}
</script>

<template>
  <el-form
    ref="formEl"
    :model="form"
    :rules="rules"
    label-width="88px"
    class="pr-8"
  >
    <el-form-item label="版本号" prop="version">
      <el-input
        v-model="form.version"
        placeholder="如 1.2.3（首字符必须是数字，与后端契约一致）"
        clearable
      />
    </el-form-item>
    <el-form-item label="二进制" prop="file">
      <el-upload
        v-model:file-list="fileList"
        action=""
        :auto-upload="false"
        :on-change="onChange"
        :on-remove="onRemove"
      >
        <el-button>选择 kk-agent 二进制</el-button>
        <template #tip>
          <div class="el-upload__tip kk-sub">
            单文件上限 64MB；上传成功即替换待分发版本，服务端保留上一版供回滚。
          </div>
        </template>
      </el-upload>
    </el-form-item>
    <el-form-item v-if="form.fileName" label="已选文件">
      <span class="kk-num"
        >{{ form.fileName }} · {{ mbText(form.size / 1024 / 1024) }}</span
      >
    </el-form-item>
  </el-form>
</template>
