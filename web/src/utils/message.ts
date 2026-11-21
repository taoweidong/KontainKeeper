import type { VNode } from "vue";
import { isFunction } from "@pureadmin/utils";
import { type MessageHandler, ElMessage } from "element-plus";

type messageTypes = "info" | "success" | "warning" | "error";

interface MessageParams {
  /** 消息类型，可选 `info` 、`success` 、`warning` 、`error` ，默认 `info` */
  type?: messageTypes;
  /** 是否纯色，默认 `false` */
  plain?: boolean;
  /** 自定义图标，该属性会覆盖 `type` 的图标 */
  icon?: any;
  /** 是否将 `message` 属性作为 `HTML` 片段处理，默认 `false` */
  dangerouslyUseHTMLString?: boolean;
  /** 显示时间，单位为毫秒。设为 `0` 则不会自动关闭，`element-plus` 默认是 `3000` ，平台改成默认 `2000` */
  duration?: number;
  /** 是否显示关闭按钮，默认值 `false` */
  showClose?: boolean;
  /** `Message` 距离窗口顶部的偏移量，默认 `16` */
  offset?: number;
  /** 设置组件的根元素，默认 `document.body` */
  appendTo?: string | HTMLElement;
  /** 合并内容相同的消息，不支持 `VNode` 类型的消息，默认值 `false` */
  grouping?: boolean;
  /** 重复次数，类似于 `Badge` 。当和 `grouping` 属性一起使用时作为初始数量使用，默认值 `1` */
  repeatNum?: number;
  /** 关闭时的回调函数, 参数为被关闭的 `message` 实例 */
  onClose?: Function | null;
}

/**
 * `Message` 消息提示函数——全站 toast 唯一入口（QR1123-W5）。
 *
 * 原 `customClass: "pure-message"` 把 EP toast 改造成 antd 风白底黑字三层
 * 阴影，剥夺类型色，与裸 ElMessage 调用方形成双轨观感。摘掉该类后回归 EP
 * 类型色（success 绿底 / error 红底 / warning 琥珀 / info 灰蓝），暗色经 EP
 * dark css-vars 自适应——与全站「颜色派生 EP 变量」语言一致。
 * element-plus.scss 的 pure-message 覆盖与 dark.scss 补丁已随类删除。
 * `messageStyle`（el/antd）双风格词汇失去承载，一并收掉。
 */
const message = (
  message: string | VNode | (() => VNode),
  params?: MessageParams
): MessageHandler => {
  const {
    icon,
    type = "info",
    plain = false,
    dangerouslyUseHTMLString = false,
    duration = 2000,
    showClose = false,
    offset = 16,
    appendTo = document.body,
    grouping = false,
    repeatNum = 1,
    onClose
  } = params ?? {};

  return ElMessage({
    message,
    icon,
    type,
    plain,
    dangerouslyUseHTMLString,
    duration,
    showClose,
    offset,
    appendTo,
    grouping,
    repeatNum,
    onClose: () => (isFunction(onClose) ? onClose() : null)
  });
};

/**
 * 关闭所有 `Message` 消息提示函数
 */
const closeAllMessage = (): void => ElMessage.closeAll();

export { message, closeAllMessage };
