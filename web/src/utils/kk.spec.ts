/** utils/kk.ts 的单元测试：业务页共用的格式化与错误取词。
 *
 *  锁两类语义：① 「没读到」与「读到 0」必须是两个样子（QR-W16 红线）；
 *  ② 坏值（NaN/Infinity、422 数组 detail）不允许把垃圾透传给界面（QR29-W5/W11）。
 */
import {
  describe,
  expect,
  it
} from "vitest";

import {
  ageText,
  argvText,
  durText,
  elapsedText,
  errText,
  mbText,
  nOr,
  numText,
  statusLabel,
  statusType,
  tsText,
  upgradeSkipText
} from "./kk";

describe("tsText / ageText / durText：时间与时长", () => {
  it("空值一律显示 -（0 也是空：时间戳 0 不是有效时刻）", () => {
    expect(tsText(undefined)).toBe("-");
    expect(tsText(null)).toBe("-");
    expect(tsText(0)).toBe("-");
    expect(ageText(null)).toBe("-");
    expect(durText(undefined)).toBe("-");
  });

  it("ageText 按秒/分/时/天分档", () => {
    expect(ageText(30)).toBe("30 秒前");
    expect(ageText(120)).toBe("2 分钟前");
    expect(ageText(7200)).toBe("2 小时前");
    expect(ageText(3 * 86400)).toBe("3 天前");
    expect(ageText(-5)).toBe("0 秒前"); // 负值不越界
  });

  it("durText 是区间语义（与 ageText 的相对措辞不同）", () => {
    expect(durText(45)).toBe("45 秒");
    expect(durText(90)).toBe("1 分 30 秒");
    expect(durText(3661)).toBe("1 小时 1 分");
    expect(durText(90061)).toBe("1 天 1 小时");
  });

  it("有效时间戳可格式化（locale 细节不断言，只断言不是占位）", () => {
    expect(tsText(1690000000)).not.toBe("-");
  });
});

describe("mbText / numText / elapsedText：数值读数", () => {
  it("mbText 换 GB 并保留两位", () => {
    expect(mbText(512)).toBe("512 MB");
    expect(mbText(2048)).toBe("2.00 GB");
    expect(mbText(null)).toBe("-");
  });

  it("numText：非有限数（NaN/Infinity）按「没读到」显示 -，不许渲染 NaN（QR29-W5）", () => {
    expect(numText(3.14159)).toBe("3.1");
    expect(numText(0)).toBe("0.0");          // 读到 0 是真值，不是「没读到」
    expect(numText(NaN)).toBe("-");
    expect(numText(Infinity)).toBe("-");
    expect(numText(null)).toBe("-");
    expect(numText(undefined)).toBe("-");
    expect(numText(12.34, 2)).toBe("12.34");
  });

  it("elapsedText 毫秒转秒两位", () => {
    expect(elapsedText(1234)).toBe("1.23 s");
    expect(elapsedText(null)).toBe("-");
  });
});

describe("nOr：请求失败与真值 0 必须可区分（QR-W16）", () => {
  it("读到过给真值，没读到给 —（区别于数字 0）", () => {
    expect(nOr(true, 0)).toBe(0);
    expect(nOr(false, 0)).toBe("—");
    expect(nOr(true, 7)).toBe(7);
  });
});

describe("errText：后端错误取词（QR29-W11）", () => {
  it("字符串 detail 原样透传（后端文案是中文）", () => {
    expect(
      errText({ response: { data: { detail: "用户名或密码错误" } } })
    ).toBe("用户名或密码错误");
  });

  it("FastAPI 422 的数组 detail 逐条取 msg 拼接，不显示 [object Object]", () => {
    const e = {
      response: {
        data: {
          detail: [
            { loc: ["body", "pods"], msg: "field required", type: "value_error" },
            { loc: ["body", "timeout"], msg: "ensure value >= 1", type: "value_error" }
          ]
        }
      }
    };
    expect(errText(e)).toBe("field required；ensure value >= 1");
  });

  it("对象型 detail 降级为 JSON 文本而非对象", () => {
    const e = { response: { data: { detail: { why: "nope" } } } };
    expect(errText(e)).toBe('{"why":"nope"}');
  });

  it("没有 detail 时回落 message，再回落 String(e)", () => {
    expect(errText(new Error("boom"))).toBe("boom");
    expect(errText({ message: "" })).toBe(String({ message: "" }));
    expect(errText("plain string")).toBe("plain string");
  });
});

describe("statusType / statusLabel / upgradeSkipText：状态词汇", () => {
  it("状态 → Element Plus 标签类型五档", () => {
    expect(statusType("done")).toBe("success");
    expect(statusType("failed")).toBe("danger");
    expect(statusType("timeout")).toBe("danger");
    expect(statusType("running")).toBe("primary");
    expect(statusType("sent")).toBe("warning");
    expect(statusType("queued")).toBe("info");     // 未知值兜底
  });

  it("状态中文标签覆盖终态与非终态，未知原样回显", () => {
    expect(statusLabel("done")).toBe("已完成");
    expect(statusLabel("lost")).toBe("结果丢失");
    expect(statusLabel("mystery")).toBe("mystery");
  });

  it("升级跳过原因与后端 reason 码一一对应，未知原因原样透传便于排障", () => {
    expect(upgradeSkipText("already_latest")).toBe("已是最新");
    expect(upgradeSkipText("no_broker")).toBe("服务端未连 Broker");
    expect(upgradeSkipText("weird_reason")).toBe("weird_reason");
    expect(upgradeSkipText(undefined)).toBe("未知原因");
  });
});

describe("argvText：命令内容预览的统一出口（QR29-E.2）", () => {
  it("数组空格连接；items 结构以「采集项:」点名；其余对象 JSON 化", () => {
    expect(argvText(["/bin/ls", "-l", "a b"])).toBe("/bin/ls -l a b");
    expect(argvText({ items: ["cpu", "disk"] })).toBe("采集项: cpu, disk");
    expect(argvText({ other: 1 })).toBe('{"other":1}');
  });

  it("字符串先试 JSON.parse（D.4 后只剩历史脏数据）：合法 JSON 解析、失败原样、空串兜 -", () => {
    expect(argvText('["df", "-h"]')).toBe("df -h");
    expect(argvText("df -h | sort")).toBe("df -h | sort");
    expect(argvText("")).toBe("-");
  });

  it("空值族全兜 -：null / undefined / 0 都不该渲染成字符串", () => {
    expect(argvText(null)).toBe("-");
    expect(argvText(undefined)).toBe("-");
  });
});
