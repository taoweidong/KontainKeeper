/** 批量下发二次确认（A5.5）。

批量下发前必须让用户看到「在线 X 台 · 离线 Y 台」以及离线主机会被 Broker 排队
补投这件事——MQTT 离线队列是本项目的核心卖点，但用户感知不到时，「先没反应、
过一会儿突然出结果」会被误判为系统异常。

单台不弹：高频单机操作不该被对话框打断。
*/
import { ElMessageBox } from "element-plus";

export type ConfirmTarget = { pod: string; online: boolean };

export async function confirmDispatch(targets: ConfirmTarget[], action = "下发命令") {
  if (targets.length <= 1) return true;
  const online = targets.filter(t => t.online);
  const offline = targets.filter(t => !t.online);
  const offlinePreview = offline
    .slice(0, 3)
    .map(t => t.pod)
    .join("、");
  const more = offline.length > 3 ? ` 等 ${offline.length} 台` : "";
  const lines = [
    `确认向 ${targets.length} 台主机${action}？`,
    `在线 ${online.length} 台 · 离线 ${offline.length} 台${
      offline.length ? `（${offlinePreview}${more}）` : ""
    }`
  ];
  if (offline.length) {
    lines.push("离线主机的命令将由 Broker 排队，重连后自动补投，结果会稍后出现。");
  }
  try {
    await ElMessageBox.confirm(lines.join("<br/>"), "批量下发确认", {
      dangerouslyUseHTMLString: true,
      confirmButtonText: "确认下发",
      cancelButtonText: "取消",
      type: "warning"
    });
    return true;
  } catch {
    return false;
  }
}
