/** 登录成功后的回跳目标解析（QR1122-W18）。
 *
 *  会话过期登出时 logOut 把现场 fullPath 附在 /login?redirect=…，
 *  登录成功后回原页而不是总览——运维不再丢失勾选与正在看的详情。
 *
 *  安全闸（防开放重定向）：只认站内路径——`/` 开头且非 `//`（协议相对
 *  会跳外站）、非 /login 自身（防套娃）；其余一律回落 fallback。 */
export function resolveRedirect(
  query: Record<string, unknown> | undefined | null,
  fallback: string
): string {
  const r = query?.redirect;
  if (
    typeof r !== "string" ||
    !r.startsWith("/") ||
    r.startsWith("//") ||
    r.startsWith("/login")
  ) {
    return fallback;
  }
  return r;
}
