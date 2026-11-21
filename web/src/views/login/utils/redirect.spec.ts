/** QR1122-W18 回归锁：登录回跳目标的站内闸。
 *
 *  会话过期带 redirect 回原页是 UX 增量，但 redirect 来自 URL query（不可信）：
 *  外站/协议相对/登录页自身一律回落 fallback，防开放重定向与套娃。 */
import { describe, expect, it } from "vitest";

import { resolveRedirect } from "./redirect";

const FB = "/welcome";

describe("resolveRedirect：站内闸（QR1122-W18）", () => {
  it("站内路径原样回跳（含 query/hash 的 fullPath）", () => {
    expect(resolveRedirect({ redirect: "/hosts/monitor" }, FB)).toBe(
      "/hosts/monitor"
    );
    expect(resolveRedirect({ redirect: "/command/shell?pods=a,b" }, FB)).toBe(
      "/command/shell?pods=a,b"
    );
    expect(resolveRedirect({ redirect: "/hosts/detail/h1" }, FB)).toBe(
      "/hosts/detail/h1"
    );
  });

  it("外站 / 协议相对 / 非字符串一律回落（防开放重定向）", () => {
    expect(resolveRedirect({ redirect: "https://evil.example" }, FB)).toBe(FB);
    expect(resolveRedirect({ redirect: "//evil.example" }, FB)).toBe(FB);
    expect(resolveRedirect({ redirect: "http://evil.example" }, FB)).toBe(FB);
    expect(resolveRedirect({ redirect: 123 }, FB)).toBe(FB);
    expect(resolveRedirect({ redirect: ["/x"] }, FB)).toBe(FB);
  });

  it("/login 自身回落（防套娃）；空 query / 空串回落", () => {
    expect(resolveRedirect({ redirect: "/login" }, FB)).toBe(FB);
    expect(resolveRedirect({ redirect: "/login?redirect=/x" }, FB)).toBe(FB);
    expect(resolveRedirect({}, FB)).toBe(FB);
    expect(resolveRedirect(undefined, FB)).toBe(FB);
    expect(resolveRedirect(null, FB)).toBe(FB);
    expect(resolveRedirect({ redirect: "" }, FB)).toBe(FB);
  });
});
