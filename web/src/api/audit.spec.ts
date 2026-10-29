/** api/audit.ts 的单元测试：审计查询端点 + detail 字段的解析降级。
 *
 *  锁住的语义：
 *  - listAudit 的 keyword/actor/action/limit/offset 全部下推后端（与导出同源，QR-S36）；
 *  - parseDetail 是「审计是追溯凭证」的展示前提：后端 detail 是 JSON 字符串，
 *    坏值必须降级成可读文本（`{原文}`）或包成键值（`{值}`），不许把裸标量当对象返回
 *    （Object.entries 会拿到空表，等于把证据显示成空白），也不许抛（QR29-W11 同族）。
 *    注：`errText` 的可读化已由 utils/kk.spec.ts 锁住，这里不重复。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";

const httpMock = vi.hoisted(() => ({ request: vi.fn() }));
vi.mock("@/utils/http", () => ({ http: httpMock }));

import { listAudit, parseDetail } from "./audit";

beforeEach(() => {
  httpMock.request.mockReset();
  httpMock.request.mockResolvedValue({ items: [], total: 0, offset: 0, limit: 200 });
});

describe("listAudit", () => {
  it("无参数时 params 位存在但为 undefined；关键字/操作者/动作/分页一起下推，offset=0 与 total=0 是真值不被改写", async () => {
    await listAudit();
    expect(httpMock.request).toHaveBeenCalledWith("get", "/api/audit", { params: undefined });

    httpMock.request.mockResolvedValue({ items: [], total: 0, offset: 0, limit: 200 });
    const res = await listAudit({ keyword: "login", actor: "admin", action: "cmd.create", limit: 100, offset: 0 });

    expect(httpMock.request.mock.calls[1][2]).toEqual({
      params: { keyword: "login", actor: "admin", action: "cmd.create", limit: 100, offset: 0 }
    });
    expect(res.total).toBe(0);
  });
});

describe("parseDetail：坏值不许进界面", () => {
  it("合法 JSON 对象（含嵌套）解析成对象，不提前字符串化", () => {
    expect(parseDetail('{"cmd":"id","pods":["a"]}')).toEqual({ cmd: "id", pods: ["a"] });
    expect(parseDetail('{"a":{"b":1}}')).toEqual({ a: { b: 1 } });   // 展示层决定怎么铺
  });

  it("空/缺字段返回空对象；非 JSON 文本降级为 {原文} 保留取证线索", () => {
    expect(parseDetail(undefined)).toEqual({});
    expect(parseDetail(null as any)).toEqual({});
    expect(parseDetail("")).toEqual({});
    expect(parseDetail("not json at all")).toEqual({ 原文: "not json at all" });
  });

  it("JSON 标量包成 {值}，0 与 false 也算「读到的真值」；字面量 null 走 {值: null}", () => {
    expect(parseDetail("42")).toEqual({ 值: 42 });
    expect(parseDetail('"text"')).toEqual({ 值: "text" });
    expect(parseDetail("true")).toEqual({ 值: true });
    expect(parseDetail("0")).toEqual({ 值: 0 });
    expect(parseDetail("false")).toEqual({ 值: false });
    expect(parseDetail("null")).toEqual({ 值: null });
  });

  it("数组 detail 原样返回（typeof 是 object）：现状登记，只保证不抛、不丢原文", () => {
    const v = parseDetail('[{"k":1}]');
    expect(Array.isArray(v)).toBe(true);
    // 后端 detail 恒为 dict，数组只可能来自脏数据；展示层会按索引键渲染成 0/1 两行。
  });
});
