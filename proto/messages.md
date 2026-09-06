# KontainKeeper 通信协议 v2（MQTT）

> `proto_ver = 2`。v1 是自研 WebSocket 帧协议（`ws://.../ws/agent`），已随 MQTT 迁移整体删除；
> 历史内容见 git 记录。双端 `PROTO_VER` 必须一致：`agent/src/kk_agent/config.py` 与
> `server/src/kk_server/__init__.py`。

传输层是标准 **MQTT 3.1.1**（paho-mqtt 双端同库）。连接可靠性、重连退避、离线命令排队、
在线判定（LWT）**全部由 Broker 负责**，不在本协议里重复实现。

## 1. 主题布局

前缀默认 `kk/v1`，双端共用同一个环境变量 `KK_TOPIC_PREFIX`（Agent 与 Server 均读取此键）配置，必须一致。

```
kk/v1/{host}/status    Agent → Server   在线状态（retain + LWT）
kk/v1/{host}/hb        Agent → Server   心跳指标
kk/v1/{host}/result    Agent → Server   命令结果（分块）
kk/v1/{host}/cmd       Server → Agent   命令下发
kk/v1/{host}/cmd_ack   —— 仅登记主题位，未实现（见 §6）
```

`{host}` 即主机标识（数据库列名仍为 `pod`，历史命名，见 AGENTS.md 术语约定）。

### 1.1 QoS 与 retain 语义（这张表是协议的硬约束）

| 主题 | 方向 | QoS | Retain | 理由 |
|---|---|---|---|---|
| `status` | A→S | 1 | **是** | 新订阅者（服务端重启/扩容）必须立刻拿到全量在线现状；同时作为 LWT 主题 |
| `hb` | A→S | 0 | **否** | 指标真相在数据库，Broker 只做搬运。retain 会让服务端每次建订阅时整批回放陈旧心跳，落库成幽灵数据点 |
| `result` | A→S | 1 | 否 | 结果必须送达，但不需要保留历史 |
| `cmd` | S→A | 1 | 否 | 命令必须送达；离线期间由 Broker 持久会话排队，重连自动补投 |

两条容易踩的语义：

- **`hb` 绝不 retain**。retained 心跳在服务端每次建立订阅时回放，而 `record_hb` 会因帧内
  `ts` 把陈旧数据写成「刚刚上报」——500 台能一次灌进 500 条幽灵点。
- **QoS1 的 `result` 不要做 `is_connected()` 预检再丢弃**。paho 的 `publish()` 本身会把 QoS1
  消息投入 out-queue、重连后自动重发；前置判断等于把离线排队能力自己短路掉。成功判定应放宽为
  `rc in (MQTT_ERR_SUCCESS, MQTT_ERR_NO_CONN)`。

## 2. 连接与在线判定

- Agent 用 **持久会话**（`clean_session=False`）+ 唯一 `client_id`，订阅自己的 `cmd` 主题（QoS1）。
- **LWT（遗嘱）**：连接时注册 `status`（`online=false`）为 retained 遗嘱。Agent 异常掉线时由
  Broker 自动发布，服务端**不需要**心跳超时猜测，消除半开连接误判。
- 上线即发布 `status`（`online=true`）。服务端重启后靠 retained `status` 立刻恢复全量在线视图。
- 多实例服务端：client_id 必须逐实例唯一（共用会被 Broker 互踢），见 §6 共享订阅说明。

### 2.1 鉴权与连接返回码

v1 的 WebSocket close code（`4400/4401/4402/4403/4404`）**已随 WS 删除**，改用 MQTT 标准连接返回码
与 Broker 鉴权：

| 场景 | 语义 |
|---|---|
| MQTT CONNACK `rc != 0` | 连接被拒。常见：`4` 用户名密码错误、`5` 未授权（生产应告警，token 配置有误） |
| `status` 帧 token 无效 | 服务端拒绝该主机注册并审计（`result_mismatch` 同类路径） |
| `status` 帧 `proto_ver` 不匹配 | 服务端拒收该帧并审计，需升级 Agent |
| ACL 越权（发布到别人的主题） | Broker 直接拒绝（Mosquitto `pattern readwrite kk/v1/%u/#`） |

生产 ACL 用 pattern 行按用户名展开（`%u` = 认证用户名 = 主机名），无需为 500 台写 500 段配置。

## 3. Agent → Server

### 3.1 `kk/v1/{host}/status`（QoS1，retain，兼作 LWT）

```json
{"online":true,"host":"web-01","token":"...","agent_ver":"0.1.0","proto_ver":2,
 "image":"vscode-server:1.2","interval":60,"reason":"online","ts":1690000000}
```

| 字段 | 说明 |
|---|---|
| `online` | 上线 `true`；LWT 或主动下线为 `false` |
| `host` | 主机标识，必须与 Broker 认证用户名一致 |
| `token` | 接入凭据，服务端校验未授权注册（配了 ACL 时是第二道防线） |
| `proto_ver` | 必须为 `2` |
| `reason` | 可读原因（`online` / `offline` / LWT 触发时为空） |
| `ts` | Unix 秒 |

### 3.2 `kk/v1/{host}/hb`（QoS0，**不 retain**）

```json
{"host":"web-01","ts":1690000000,"interval":60,"agent_ver":"0.1.0",
 "metrics":{
   "cpu":3.2,
   "load":"0.12 0.34 0.56",
   "mem_mb":500.0,"mem_total_mb":2000.0,"mem_pct":25.0,
   "disks":{"/":{"total_mb":2064.0,"used_mb":812.0,"pct":39.3}},
   "disk_io":{...},
   "net":{...},
   "procs_top":[{"pid":2,"name":"node","cpu":8.0,"mem_mb":117.2}],
   "users":[{"name":"dev","uid":1000,"procs":12,"vscode":true}],
   "sys":{...}
 },
 "custom":{"myplugin":{"any":"json"}}
}
```

- 默认 60s 一帧，带 ±10% 抖动（`random.uniform(0.9, 1.1)`，用于打散 500 台峰值）。
- `custom` 为自定义采集插件输出，可缺省；插件 `collect()` 超时（默认 5s，
  `KK_PLUGIN_TIMEOUT`）即被隔离到文件 mtime 变化重载为止，不影响整帧心跳。
- 心跳采集项可用 `KK_HB_ITEMS` 精简（逗号分隔，取值同 §4.1 `kind=collect` 白名单；
  空则全采 8 项），被精简的键整组缺省，服务端按可缺省字段处理。
- 采集来源现已统一为 **psutil**（不再手工解析 /proc），跨平台；`ts` 为 Unix 秒。
- 服务端对 `cpu` / `mem_mb` 落 Float 列前做数值规整（非数字值记 `NULL`）——
  插件写坏单个指标**不会让整帧心跳丢弃**。

### 3.3 `kk/v1/{host}/result`（QoS1，分块）

```json
{"id":"c-123","seq":0,"total":3,"out_b64":"...","done":false}
{"id":"c-123","seq":2,"total":3,"out_b64":"","done":true,
 "rc":0,"timed_out":false,"elapsed_ms":152,"truncated":false}
```

| 字段 | 说明 |
|---|---|
| `id` | 命令 ID，与 `cmd` 帧对应 |
| `seq` / `total` | 分块序号与总块数（48KB/块，输出上限 4MB 后截断） |
| `out_b64` | 该块输出的 base64（v1 的 `data_b64` 已改名） |
| `done` | 末块为 `true`，此时携带下方四个字段 |
| `rc` | 进程退出码；超时被杀为 `-1`；spawn 失败 `126/127`；**`-3` 表示结果分块未能全部送达**（out-queue 溢出），服务端据此置 `failed` |
| `timed_out` / `elapsed_ms` / `truncated` | 是否超时、耗时毫秒、输出是否被截断 |

关键收敛语义：**任一分块发送失败，Agent 必须补发一个 `rc=-3` 的失败终态**，
绝不让服务端那一行命令永远停在 `running`。

## 4. Server → Agent

### 4.1 `kk/v1/{host}/cmd`（QoS1）

```json
{"id":"c-123","kind":"shell","argv":["du","-sh","/workspace"],"timeout":30}
{"id":"c-124","kind":"collect","items":["cpu","mem","net"],"timeout":30}
{"id":"c-125","kind":"collect","items":["cpu"],"use_shell":true,"argv":["..."],"timeout":30}
{"id":"c-126","kind":"plugin_reload","timeout":30}
{"id":"u-web-01","kind":"update","version":"0.2.0","sha256":"<hex>",
 "size":1234567,"url":"/api/system/agent/download"}
```

| 字段 | 说明 |
|---|---|
| `id` | 命令 ID |
| `kind` | `shell` / `collect` / `plugin_reload` / `update` |
| `argv` | `kind=shell` 时为数组直传 exec（不经 shell 拼接），`timeout` 1–600s |
| `items` | `kind=collect` 必需，取自下方白名单 |
| `use_shell` | 允许管道等 shell 语法，受 Agent `KK_ALLOW_SHELL` 约束 |
| `update` 专用 | `version` / `sha256` / `size` / `url`（相对管理 API 基址） |

**采集项白名单（8 项，双端必须一致）**：

```
cpu, mem, disk, disk_io, net, proc, user, sys
```

- Agent 侧：`collector.ITEMS` / `ITEM_NAMES`
- Server 侧：`kk_server.config.COLLECT_ITEMS`
- 服务端校验 `items ⊆ COLLECT_ITEMS`，非法项返回 400 并列出白名单；未知项在 Agent 侧忽略。
- `kind=collect` 不经 shell，直接调 psutil 采集并返回结构化 JSON。

**命令排队**：Agent 离线时命令由 Broker 持久会话排队（QoS1），重连自动补投。
服务端**不保留** `pending_for` 补发逻辑（已删），`mark_sent` 语义即「已发布到 Broker」。

## 5. Agent 自更新 REST 接口

与 MQTT 通道并行，走 HTTP（与 v1 相同，仍适用）：

| 方法 | 路径 | 鉴权 | 说明 |
|---|---|---|---|
| POST | `/api/system/agent` | 管理员会话 | 上传新版本二进制（multipart: `file` + `version`），服务端算 `sha256` 并记录为最新 |
| GET | `/api/system/agent/latest?ver=<当前版本>` | Agent token | 返回 `{available, version, sha256, size, url}` |
| GET | `/api/system/agent/download` | Agent token | 流式下发最新二进制 |

安全边界：下载/查询仅需 Agent token；替换前强制 `sha256` 校验，可选 HMAC
（`KK_UPDATE_HMAC_KEY` + `KK_UPDATE_REQUIRE_SIG`）。**ed25519 签名决策不做**（需引入 pynacl，
而服务端被攻陷时攻击者同样能篡改清单里的 sha256，边际收益有限）。
`KK_UPDATE_INSECURE=1` 可关闭 TLS 校验（不推荐）。

## 6. 已登记但未实现

| 项 | 状态 | 说明 |
|---|---|---|
| `kk/v1/{host}/cmd_ack` | **仅登记主题位** | 曾计划用它做应用层 ACK 状态机（pending→sent→acked→running→done）。**已否决**：Broker 的 QoS1 + 持久会话已保证送达，再加一层是自研重复实现。改由服务端的 SQL 超时清扫器收敛（`sweep_command_timeouts`）。真需要时零迁移成本启用 |
| 共享订阅 `$share/{group}/...` | **默认不启用** | 解决多消费端分摊分发压力，本方案瓶颈在 DB 写入而非订阅分发；500 台单实例用不到。订阅拼装收敛在 `MqttBridge._sub_topics()`，加前缀即可扩容 |

## 7. 约束

- 单帧上限 16MB（Agent 解析器保护阈值）；心跳帧典型 2–4KB。
- 所有 `ts` 为 Unix 秒。
- 命令输出分块 48KB，总量超 4MB 截断并置 `truncated`。
- Agent 侧离线 out-queue 上限 `MAX_QUEUED = 512`（可配）；超出会被淘汰，
  因此大输出场景下 `send_result` 必须感知入队失败并回 `rc=-3`。
