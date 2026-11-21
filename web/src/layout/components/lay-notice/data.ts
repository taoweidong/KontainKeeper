export interface ListItem {
  avatar: string;
  title: string;
  datetime: string;
  type: string;
  description: string;
  status?: "primary" | "success" | "warning" | "info" | "danger";
  extra?: string;
}

export interface TabItem {
  key: string;
  name: string;
  list: ListItem[];
  emptyText: string;
}

/** QR1122-W17（离线红线）：上游演示数据清退——原「消息」条目的头像指向
 *  外部 URL（xiaoxian521.github.io），运行时联网踩离线部署红线；且 7 条
 *  假消息让铃铛常驻 badge，运维以为真有通知。三个 tab 保留空态文案，
 *  将来接后端通知源时往 list 里填即可。 */
export const noticesData: TabItem[] = [
  {
    key: "1",
    name: "通知",
    list: [],
    emptyText: "暂无通知"
  },
  {
    key: "2",
    name: "消息",
    list: [],
    emptyText: "暂无消息"
  },
  {
    key: "3",
    name: "待办",
    list: [],
    emptyText: "暂无待办"
  }
];
