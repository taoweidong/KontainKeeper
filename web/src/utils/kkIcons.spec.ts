/** kkIcons 的离线红线锁（QR1121-W5）：菜单 icon 预览必须先过 menuIconKnown。
 *
 *  含 `:` 的字符串会走 IconifyIconOnline 运行时联网（离线部署红线），
 *  未注册的字符串渲染为空白——两者都必须被拦在渲染链之外。
 *  node 环境即可测（纯函数 + 注册名单）；offlineIcon 的 addIcon 副作用
 *  在 node 下 getSvgInfo 有守卫，不碰 DOM。
 */
import { describe, expect, it } from "vitest";

import { menuIconKnown, REGISTERED_MENU_ICONS } from "./kkIcons";

describe("menuIconKnown：离线白名单闸（QR1121-W5）", () => {
  it("含 : 的字符串恒 false（在线图标会联网，离线部署红线）", () => {
    expect(menuIconKnown("ep:monitor")).toBe(false);
    expect(menuIconKnown("ri:search-line")).toBe(false);
    expect(menuIconKnown("ep:home-filled")).toBe(false);
  });

  it("未注册字符串 false（侧边栏渲染为空白，预览应显原串）", () => {
    expect(menuIconKnown("ep/not-exists")).toBe(false);
    expect(menuIconKnown("")).toBe(false);
    expect(menuIconKnown("随便填的")).toBe(false);
  });

  it("8 个已注册菜单图标 true（与后端菜单种子 + offlineIcon 名单对账）", () => {
    const names = [
      "ep/home-filled",
      "ep/monitor",
      "ep/operation",
      "ep/document",
      "ep/odometer",
      "ep/setting",
      "ri/search-line",
      "ri/information-line"
    ];
    expect(REGISTERED_MENU_ICONS.size).toBe(names.length);
    for (const n of names) {
      expect(REGISTERED_MENU_ICONS.has(n)).toBe(true);
      expect(menuIconKnown(n)).toBe(true);
    }
  });

  it("非字符串入参不抛错、按未注册处理", () => {
    expect(menuIconKnown(null as unknown as string)).toBe(false);
    expect(menuIconKnown(undefined as unknown as string)).toBe(false);
  });
});
