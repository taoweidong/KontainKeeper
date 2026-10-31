// 菜单级 RBAC 纯函数（kkMenu.ts）的单测：过滤口径与元数据合并是全站侧边栏的
// 渲染根，fail-closed 语义必须锁死。
import { describe, expect, it } from "vitest";

import {
  applyMenuMeta,
  collectRouteNames,
  filterMenusByAllowed,
  type KkMenuMeta
} from "./kkMenu";

type Node = {
  name?: string;
  meta?: Record<string, any>;
  children?: Node[];
};

const tree: Node[] = [
  {
    name: "Home",
    meta: { title: "首页", icon: "ep/home-filled", rank: 0 },
    children: [
      { name: "Welcome", meta: { title: "首页" }, children: [] }
    ]
  },
  {
    name: "Hosts",
    meta: { title: "主机管理", rank: 1 },
    children: [
      { name: "HostMonitor", meta: { title: "主机总览" }, children: [] },
      { name: "HostDetail", meta: { title: "主机详情" }, children: [] }
    ]
  }
];

describe("collectRouteNames", () => {
  it("收集全部层级 name（含 remaining 无关，本函数只管传入的树）", () => {
    expect(collectRouteNames(tree)).toEqual(
      new Set(["Home", "Welcome", "Hosts", "HostMonitor", "HostDetail"])
    );
  });
});

describe("filterMenusByAllowed", () => {
  it("只保留允许集内的叶子，空分组随之消失", () => {
    const out = filterMenusByAllowed(
      tree,
      new Set(["Home", "Welcome", "Hosts", "HostMonitor"])
    );
    expect(out.map(n => n.name)).toEqual(["Home", "Hosts"]);
    expect(out[1].children!.map(n => n.name)).toEqual(["HostMonitor"]);
  });

  it("子项全被过滤时父分组也不显示（分组名在集合内也不留空壳）", () => {
    const out = filterMenusByAllowed(tree, new Set(["Hosts"]));
    expect(out).toEqual([]);
  });

  it("空集合 → 空树（fail-closed）", () => {
    expect(filterMenusByAllowed(tree, new Set())).toEqual([]);
  });

  it("不修改入参（handleWholeMenus 复用静态声明对象）", () => {
    const snapshot = JSON.stringify(tree);
    filterMenusByAllowed(tree, new Set(["Welcome"]));
    expect(JSON.stringify(tree)).toBe(snapshot);
  });
});

describe("applyMenuMeta", () => {
  const menus: KkMenuMeta[] = [
    { name: "Hosts", title: "机队", icon: "ep/cpu", rank: 9, show_link: true },
    { name: "HostDetail", title: "主机详情", icon: "", rank: 3, show_link: false }
  ];

  it("合并后端元数据（title/icon/rank/showLink），其余节点原样", () => {
    const out = applyMenuMeta(tree, menus);
    const hosts = out.find(n => n.name === "Hosts")!;
    expect(hosts.meta).toMatchObject({
      title: "机队",
      icon: "ep/cpu",
      rank: 9
    });
    // 找不到元数据的节点保持原值
    expect(out.find(n => n.name === "Home")!.meta!.title).toBe("首页");
  });

  it("show_link=false 落 meta.showLink（详情页不出侧边栏但仍可跳转）", () => {
    const out = applyMenuMeta(tree, menus);
    const detail = out
      .find(n => n.name === "Hosts")!
      .children!.find(n => n.name === "HostDetail")!;
    expect(detail.meta!.showLink).toBe(false);
  });

  it("不修改入参", () => {
    const snapshot = JSON.stringify(tree);
    applyMenuMeta(tree, menus);
    expect(JSON.stringify(tree)).toBe(snapshot);
  });
});
