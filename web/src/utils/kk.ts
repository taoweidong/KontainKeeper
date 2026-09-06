/** KontainKeeper 业务页共用的格式化与状态映射。

后端所有时间字段都是秒级 Unix 时间戳（int），这里统一成中文可读格式。
状态色遵循运维直觉：在线/成功=绿，离线/失败=红，执行中=蓝，等待=灰。
*/
export const tsText = (ts?: number | null): string => {
  if (!ts) return "-";
  return new Date(ts * 1000).toLocaleString("zh-CN", { hour12: false });
};

export const ageText = (sec?: number | null): string => {
  if (sec === null || sec === undefined) return "-";
  if (sec < 60) return `${Math.max(0, Math.round(sec))} 秒前`;
  if (sec < 3600) return `${Math.floor(sec / 60)} 分钟前`;
  if (sec < 86400) return `${Math.floor(sec / 3600)} 小时前`;
  return `${Math.floor(sec / 86400)} 天前`;
};

/** 时长格式化（非相对时间）：用于运行时长、执行耗时等区间语义 */
export const durText = (sec?: number | null): string => {
  if (sec === null || sec === undefined) return "-";
  const s = Math.max(0, Math.round(sec));
  if (s < 60) return `${s} 秒`;
  if (s < 3600) return `${Math.floor(s / 60)} 分 ${s % 60} 秒`;
  if (s < 86400) return `${Math.floor(s / 3600)} 小时 ${Math.floor((s % 3600) / 60)} 分`;
  return `${Math.floor(s / 86400)} 天 ${Math.floor((s % 86400) / 3600)} 小时`;
};

export const mbText = (mb?: number | null): string => {
  if (mb === null || mb === undefined) return "-";
  return mb >= 1024 ? `${(mb / 1024).toFixed(2)} GB` : `${Math.round(mb)} MB`;
};

export const numText = (v?: number | null, digits = 1): string =>
  v === null || v === undefined ? "-" : Number(v).toFixed(digits);

/** 命令状态 → Element Plus 标签类型 */
export const statusType = (s: string): "success" | "danger" | "info" | "warning" | "primary" => {
  switch (s) {
    case "done":
      return "success";
    case "failed":
    case "timeout":
    case "lost":
      return "danger";
    case "running":
      return "primary";
    case "sent":
      return "warning";
    default:
      return "info";
  }
};

export const statusLabel = (s: string): string => {
  return {
    pending: "待下发",
    sent: "已下发",
    running: "执行中",
    done: "已完成",
    failed: "失败",
    timeout: "超时",
    lost: "结果丢失"
  }[s] || s;
};
