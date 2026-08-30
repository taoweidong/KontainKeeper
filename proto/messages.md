# KontainKeeper 通信协议 v1

容器内 Agent 与管理服务端之间的 WebSocket JSON 帧协议。
- 传输：`ws://` 或 `wss://`，路径 `/ws/agent`，仅容器→服务端出站方向发起
- 所有帧为 UTF-8 JSON 文本帧，含 `t` 类型字段
- `proto_ver = 1`；不匹配时服务端以 close code 4402 拒绝

## 连接生命周期

```
agent                          server
  │── connect + handshake ──────►│
  │── hello ────────────────────►│  校验 token / proto_ver，注册 pod
  │◄── (可选 cfg) ───────────────┤  服务端可下发运行参数
  │◄════════ 长连接 ═════════════►│
  │── hb (每 interval 秒) ──────►│  指标 + 自定义采集
  │◄── cmd ──────────────────────┤  管理员下发命令
  │── cmd_result (1..n 帧) ─────►│  分块回传，末帧带 done/rc
  │── bye / close ──────────────►│  断开；指数退避重连后重新 hello
```

Close codes：`4400` 握手超时/格式错误，`4401` token 无效，`4402` 协议版本不匹配，`4403` 被同名新连接替换。

## Agent → Server

### hello（连接后第一帧）

```json
{"t":"hello","id":"a1b2c3","proto_ver":1,"pod":"vscode-7f9c-x2",
 "image":"vscode-server:1.2","agent_ver":"0.1.0","token":"...","interval":60}
```

### hb（心跳+指标，默认 60s，带 ±10% 抖动）

```json
{"t":"hb","ts":1690000000,"interval":60,
 "metrics":{
   "cpu":3.2,                       // 区间平均 CPU%（/proc/stat 差分）
   "load":"0.12 0.34 0.56",
   "mem_mb":500.0,"mem_total_mb":2000.0,"mem_pct":25.0,
   "disks":{"/":{"total_mb":2064.0,"used_mb":812.0,"pct":39.3}},
   "procs_top":[{"pid":2,"name":"node","cpu":8.0,"mem_mb":117.2}],
   "users":[{"name":"dev","uid":1000,"procs":12,"vscode":true}]
 },
 "custom":{"myplugin":{"any":"json"}}   // 自定义采集插件输出，可缺省
}
```

### cmd_result（命令结果，分块回传）

```json
{"t":"cmd_result","id":"c-123","seq":0,"data_b64":"...","done":false}
{"t":"cmd_result","id":"c-123","seq":3,"data_b64":"","done":true,
 "rc":0,"timed_out":false,"elapsed_ms":152}
```

- 原始输出按 48KB 分块 base64；总输出超过 4MB 截断
- `rc`: 进程退出码；`timed_out`: 是否超时被杀（此时 rc=-1）
- spawn 失败：`rc=126/127`，错误信息在输出内

### bye（可选，Agent 主动退出前）

```json
{"t":"bye","reason":"stopping"}
```

## Server → Agent

### cmd（命令下发）

```json
{"t":"cmd","id":"c-123","kind":"shell","argv":["du","-sh","/workspace"],"timeout":30}
{"t":"cmd","id":"c-124","kind":"plugin_reload"}
```

- `kind=shell`: `argv` 为数组直传 exec（不经 shell 拼接），`timeout` 1–600s
- `kind=plugin_reload`: 立即重扫插件目录并采集，结果以 cmd_result 回传摘要
- Agent 离线时命令入库为 `pending`，重连 hello 后由服务端补发

### cfg（运行参数调整，可选）

```json
{"t":"cfg","interval":120}
```

### upgrade（服务端推送新版本，Agent 自更新）

Agent 连接 `hello` 时若版本落后，或服务端在连接存活期间发布了新版本（Agent 亦按
`KK_UPDATE_INTERVAL` 定时轮询），服务端下发该帧，Agent 立即下载并自更新：

```json
{"t":"upgrade","version":"0.2.0","sha256":"<hex>","size":1234567,
 "url":"/api/system/agent/download"}
```

- `url` 相对管理 API 基址（`KK_UPDATE_URL`，缺省由 `KK_SERVER` 的 ws/wss 推导为 http/https）
- Agent 下载后用 `sha256` 校验一致才原子替换自身二进制并 `execv` 自重启
- 源码形态运行（`python -m kk_agent`）时只下载校验、不自动替换解释器

## Agent 自更新 REST 接口

Agent 二进制作为独立服务运行，内置版本监控与自动更新，无需人工执行更新命令。

| 方法 | 路径 | 鉴权 | 说明 |
|---|---|---|---|
| POST | `/api/system/agent` | 管理员会话 | 上传新版本二进制（multipart: `file` + `version`），服务端计算 `sha256` 落盘并记录为最新 |
| GET | `/api/system/agent/latest?ver=<当前版本>` | Agent token | 返回 `{available, version, sha256, size, url}`；版本不落后或尚未配置则返回 `available:false` |
| GET | `/api/system/agent/download` | Agent token | 流式下发最新二进制（与 WebSocket `hello` 同一 `KK_AGENT_TOKENS` 令牌池） |

安全边界：下载/查询仅需 Agent token（与 hello 同源，避免明文暴露）；二进制替换前强制 `sha256`
校验（纯标准库，防损坏/篡改）；`KK_UPDATE_INSECURE=1` 可关闭 TLS 校验（不推荐）。

## 心跳字段与采集来源（Agent 端承诺）

| 字段 | 来源 |
|---|---|
| cpu | `/proc/stat` 两次采样差分 |
| load | `/proc/loadavg` |
| mem_* | `/proc/meminfo`（MemTotal/MemAvailable） |
| disks | `statvfs(KK_DISK_PATHS)`，默认 `/,/workspace` |
| procs_top | `/proc/<pid>/stat` utime+stime 差分 + rss，按 CPU 取前 5 |
| users | `/etc/passwd` × `/proc/<pid>/status` Uid 计数 × `~/.vscode-server` 存在性 |

## 约束

- 单帧上限 16MB（Agent 解析器保护阈值）
- 心跳帧典型 < 2KB
- 所有 `ts` 为 Unix 秒
