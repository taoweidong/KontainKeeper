/** QR1123-W5 回归锁：全站 toast 唯一入口的委托形状。
 *
 *  message() 摘掉 pure-message customClass 后回归 EP 类型色；这里锁：
 *  ① 不再携带 pure-message（antd 白底黑字风格的承载类已删）；
 *  ② 平台默认值保持（duration 2000 / offset 16 / type info）；
 *  ③ params 透传与 onClose 包装。ElMessage 桩掉，不触真实 DOM。
 *  jsdom：默认 appendTo=document.body 需要 document 存在。 */
// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from "vitest";

const h = vi.hoisted(() => ({ elMessage: vi.fn(), closeAll: vi.fn() }));

vi.mock("element-plus", () => ({
  ElMessage: Object.assign(h.elMessage, { closeAll: h.closeAll })
}));

import { message, closeAllMessage } from "./message";

beforeEach(() => {
  h.elMessage.mockReset();
  h.elMessage.mockReturnValue({ close: vi.fn() });
});

describe("message：EP 委托形状（QR1123-W5）", () => {
  it("无参调用：type info / duration 2000 / offset 16，无 pure-message", () => {
    message("提示");
    const arg = h.elMessage.mock.calls[0][0];
    expect(arg.message).toBe("提示");
    expect(arg.type).toBe("info");
    expect(arg.duration).toBe(2000);
    expect(arg.offset).toBe(16);
    expect(arg.customClass).toBeUndefined();
  });

  it("params 透传：type/duration/showClose/grouping", () => {
    message("失败", { type: "error", duration: 4000, showClose: true });
    const arg = h.elMessage.mock.calls[0][0];
    expect(arg.type).toBe("error");
    expect(arg.duration).toBe(4000);
    expect(arg.showClose).toBe(true);
    expect(arg.customClass).toBeUndefined();
  });

  it("onClose 包装：回调被调用；非函数不抛", () => {
    const cb = vi.fn();
    message("x", { onClose: cb });
    h.elMessage.mock.calls[0][0].onClose();
    expect(cb).toHaveBeenCalledTimes(1);

    message("y", { onClose: null });
    expect(() => h.elMessage.mock.calls[1][0].onClose()).not.toThrow();
  });

  it("closeAllMessage 委托 ElMessage.closeAll", () => {
    closeAllMessage();
    expect(h.closeAll).toHaveBeenCalledTimes(1);
  });
});
