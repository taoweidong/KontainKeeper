# KontainKeeper 通信协议 v4（MQTT）

> `proto_ver = 4`。v1 是自研 WebSocket 帧协议（`ws://.../ws/agent`），已随 MQTT 迁移整体删除；
> v2 引入 MQTT 主题布局但保留 token 接入认证；**v3 去掉 token：Broker 匿名开放，
> 接入管控改为服务端 `KK_AGENT_IPS` 白名单（上行帧统一携带自报 `ip` 字段）**；
> **v4 把管理对象从「K8S 容器 IDE」扩为任意 Linux 主机：`status` 帧新增
> `env`/`group`/`labels`/`caps`（服务端另可接收 `docker` 摘要），全部是可选字段**，
> 主题布局、QoS/retain 语义与其余帧结构一个字节都没动。
> 历史内容见 git 记录。双端 `PROTO_VER` 必须一致：`agent/src/kk_agent/config.py` 与
> `server/src/kk_server/__init__.py`。
>
> **v3 → v4 有兼容窗口**（见 §3.1.1）：窗口期内 v3 帧照常受理，抬版本号本身不会让存量
> Agent 掉线。

传输层是标准 **MQTT 3.1.1**（paho-mqtt 双端同库）。连接可靠性、重连退避、离线命令排队、
在线判定（LWT）**全部由 Broker 负责**，不在本协议里重复实现。

## 1. 主题布局

前缀默认 `kk/v1`，双端共用同一个环境变量 `KK_TOPIC_PREFIX`（Agent 与 Server 均读取此键）配置，必须一致。
Agent 启动时校验前缀**不得含 MQTT 通配符 `+` / `#`**（含之即拒绝启动）：含通配符会让本机订阅到
其他主机的命令主题（QR29-A11）。`/` 是前缀自身的层级分隔，合法。

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
| `result` | A→S | 1 | 否 | 结果必须送达，但不需要保留历史；**唯一的例外**是 `rc=-3` 的失败终态走 QoS0（见 §3.3） |
| `cmd` | S→A | 1 | 否 | 命令必须送达；离线期间由 Broker 持久会话排队，重连自动补投 |

三条容易踩的语义：

- **`hb` 绝不 retain**。retained 心跳在服务端每次建立订阅时回放，而 `record_hb` 会因帧内
  `ts` 把陈旧数据写成「刚刚上报」——500 台能一次灌进 500 条幽灵点。
- **QoS1 的 `result` 不要做 `is_connected()` 预检再丢弃**。paho 的 `publish()` 本身会把 QoS1
  消息投入 out-queue、重连后自动重发；前置判断等于把离线排队能力自己短路掉。成功判定应放宽为
  `rc in (MQTT_ERR_SUCCESS, MQTT_ERR_NO_CONN)`。
- **服务端不得假设 `result` 恒为 QoS1**。失败终态故意降级为 QoS0 是为了抢在自己积压前面
  （out-queue 满时 QoS1 终态会被同一次 `QUEUE_SIZE` 一起拒收）。服务端按 QoS1 订阅即可正常
  收到 QoS0 帧（投递 QoS 取二者较小值），协议**帧结构不变**，因此不需要升 `proto_ver`。

## 2. 连接与在线判定

- Agent 用 **持久会话**（`clean_session=False`）+ 唯一 `client_id`，订阅自己的 `cmd` 主题（QoS1）。
- **LWT（遗嘱）**：连接时注册 `status`（`online=false`）为 retained 遗嘱。Agent 异常掉线时由
  Broker 自动发布，服务端**不需要**心跳超时猜测，消除半开连接误判。
- 上线即发布 `status`（`online=true`）。服务端重启后靠 retained `status` 立刻恢复全量在线视图。
- 多实例服务端：client_id 必须逐实例唯一（共用会被 Broker 互踢），见 §6 共享订阅说明。

### 2.1 接入管控（IP 白名单）与连接返回码

v1 的 WebSocket close code（`4400/4401/4402/4403/4404`）**已随 WS 删除**。v3 的接入管控
不依赖 MQTT 鉴权（Broker 匿名开放），而是服务端白名单：

| 场景 | 语义 |
|---|---|
| MQTT CONNACK `rc != 0` | 连接被拒（网络/ Broker 不可达，生产应告警） |
| 上行帧自报 `ip` 不在 `KK_AGENT_IPS` 白名单 | 服务端拒绝该帧（主机不注册 / 指标不入库）并审计 `ip_rejected` |
| `status` 帧 `proto_ver` 不在服务端兼容窗口内 | 服务端拒收该帧并审计 `proto_mismatch`，需升级 Agent（窗口语义见 §3.1.1） |

白名单规则：

- **动态白名单（QR1124）**：运行时名单存 DB（`kk_ip_rules`），经「系统管理 → IP 白名单」
  界面配置（增删 / 启停 / 备注），变更**即时生效无需重启**（服务端刷新桥接与 REST 两处
  快照，多实例 30s 内收敛）。`KK_AGENT_IPS`（逗号分隔 IP / CIDR，如 `10.0.0.0/24,192.168.1.5`，
  单 IP 自动 /32、IPv6 /128）退为**启动种子**：首次启动播种进 DB（缺则补、管理员改过不重置），
  此后 DB 为运行时真相源。**名单为空 = 放行全部**，仅用于开发/测试；`KK_ENV=production`
  时 env 与 DB 启用规则**皆空**才拒绝启动（校验在 lifespan 建库后，`ensure_whitelist_configured`）。
- **拦截记录**：白名单外上报除审计 `ip_rejected`（60s 全局节流，安全溯源链）外，另按源 IP
  聚合落 `kk_ip_rejects`（每 IP 60s 节流 upsert + 30 天保留 + 行数上限），「系统管理 → 拦截记录」
  页可查可导出。审计节流纪律（QR1120-S1）不变。
- MQTT 经 Broker 中转拿不到发布者真实 TCP 源 IP，白名单基于 Agent **自报值**：
  `KK_ADVERTISE_IP` 显式覆盖 > UDP connect 自动探测出口地址。适合内网可信环境；
  需要更强边界时在网络层限制 1883 端口可达范围，**或启用 Broker 鉴权（推荐，见下）**。
- 可选加固——Broker 鉴权：Broker 端启用 `password_file` + `acl_file`（下行 cmd 主题
  只允许服务端账号发布，关闭匿名下行通道）后，双端配 `KK_MQTT_USERNAME` /
  `KK_MQTT_PASSWORD`（Agent TLS 另有 `KK_TLS_CA`）。凭据不影响主题布局与帧格式，
  改过 `KK_TOPIC_PREFIX` 需同步 aclfile；启用步骤见 docs/deployment.md「启用 MQTT 鉴权」。

## 3. Agent → Server

### 3.1 `kk/v1/{host}/status`（QoS1，retain，兼作 LWT）

```json
{"online":true,"host":"web-01","ip":"10.0.0.5","agent_ver":"0.4.0","proto_ver":4,
 "image":"vscode-server:1.2","interval":60,"reason":"online","ts":1690000000,
 "env":{"os":"Ubuntu 22.04.3 LTS","kernel":"5.15.0-118","arch":"x86_64",
        "virt":"vm","in_container":false,"runtime":"","hostname":"web-01",
        "ips":["10.0.0.5"]},
 "group":"ops-web","labels":{"role":"web","idc":"sh-1"},
 "caps":{"shell":true},"docker":{"total":12,"running":11,"unhealthy":1}}
```

| 字段 | 说明 |
|---|---|
| `online` | 上线 `true`；LWT 或主动下线为 `false` |
| `host` | 主机标识 |
| `ip` | 自报出口 IP（`KK_ADVERTISE_IP` 覆盖 > 自动探测），服务端按 `KK_AGENT_IPS` 白名单校验；探测失败为空串（将被拒） |
| `proto_ver` | 必须落在服务端兼容窗口内：当前接受 `3` 或 `4`，配 `KK_DROP_PROTO_V3=1` 后只接受 `4`（§3.1.1） |
| `reason` | 可读原因：`online` / `offline` / `updating`（自更新前自报）/ LWT 触发时为空 |
| `ts` | Unix 秒 |
| `env` / `group` / `labels` / `caps` / `docker` | **v4 新增，全部可选**，见 §3.1.1 |

> **`reason=updating`（B6.1）**：`os.execv` 自更新会直接替换进程、断开 MQTT，Broker 随即
> 补发 LWT（`reason` 为空）。Agent 在 execv 前先发一帧 `online=false, reason=updating`
> 并**等 PUBACK**再替换进程，服务端据此把「正在升级」与「容器停了」区分开。
> 服务端对「空 reason 覆盖刚写入的 `updating`」有 120s 宽限保留，避免紧跟着的 LWT 把它抹白。
> 这里的「等 PUBACK」是**真等**：paho 的 `wait_for_publish()` 超时是静默返回，Agent 侧还要
> 再问一次 `is_published()`，否则「已入队」会被当成「已送达」，execv 一来帧就随进程一起没了。

### 3.1.1 v4 元信息字段与兼容窗口

v4 只往 `status` 帧里**加可选字段**，不删不改语义——这是兼容窗口成立的前提：帧结构
没变，服务端只是少读几个键而已。

| 字段 | 类型 | 内容 | 缺省与容错 |
|---|---|---|---|
| `env` | object | 主机指纹：`os`（pretty name）、`kernel`、`arch`、`virt`（`metal`/`vm`/`container`，容器优先级最高）、`in_container`、`runtime`（容器内粗判：`docker`/`kubernetes`/`container`，非容器为空串）、`hostname`、`ips[]` | 任一项缺失即缺省；服务端只取 `virt/os/kernel/arch` 落标量列，坏值回落空串，`virt` 空则记 `container` |
| `group` | string | 单值分组（`KK_GROUP`，截 64 字符）——一个主机只属于一个组 | 空串 = 未分组 |
| `labels` | object | 多值维度 `k=v`，供总览多维筛选。Agent 侧由 `KK_LABELS` 逗号分隔解析：无 `=` 的片段收成 `k: ""`，重复 key 后者覆盖，key 截 64、value 截 128 字符 | 非 dict 当 `{}`；服务端入库前整列按 4096 字符封顶，超限截断并写 `_truncated` 标记（摘要视图不带它，详情侧才解析） |
| `caps` | object | 能力位声明，内置 Agent 目前只报 `{"shell": <KK_ALLOW_SHELL>}` | 同上封顶；**当前只上报、服务端未拿它做门禁**（QR-S35） |
| `docker` | object | 容器编排摘要 `{"total","running","unhealthy"}`，供总览一列 | 非 dict 当 `{}`；内置 Agent **暂不发送**，为编排侧预留 |

**QoS / retain / 主题布局与 v3 完全一致**（§1.1 那张表是硬约束，抬版本不动它）。
`hb`、`result`、`cmd` 三帧在 v4 里没有任何字段变化。

兼容窗口（`server/src/kk_server/__init__.py` 的 `ACCEPT_PROTO_VERS = (3, 4)`）：

- 窗口内：v3 帧照常解析落库，`proto_ver` 落 3，总览页据此标「待升级」。
- 关窗口：设 `KK_DROP_PROTO_V3=1`，窗口收缩成 `(4,)`，v3 帧走拒收 + 审计。
- **窗口是精确集合，不是「不低于某个版本都兼容」**：窗口外的帧——更老的 v1/v2，或还没
  开放的更新版本——按新语义解析会得到**错的 `online` 值**，比丢帧更危险，所以拒收而不是
  尽力解析（畸形值如 `"proto_ver":"v3"` 同样按窗口外处理）。
- 关窗口前必须看服务端统计里的 **`proto_v3_received`**（/system 页的「旧协议帧」）：
  它只在受理窗口内旧协议帧时累加，是「存量 Agent 是否全部升完」的唯一依据。它是
  **进程内累计值**，升级本身不会让它下降——正确读法是「存量 Agent 全部升级完 →
  重启服务端 → 它仍为 0」。**没看到 0 就关窗口等于一次全网闪断。**


### 3.2 `kk/v1/{host}/hb`（QoS0，**不 retain**）

```json
{"host":"web-01","ip":"10.0.0.5","ts":1690000000,"interval":60,"agent_ver":"0.3.0",
 "metrics":{
   "cpu":3.2,
   "load":"0.12 0.34 0.56",
   "mem_mb":500.0,"mem_total_mb":2000.0,"mem_pct":25.0,
   "disks":{"/":{"total_mb":2064.0,"used_mb":812.0,"pct":39.3}},
   "disk_io":{...},
   "net":{...},
   "procs_top":[{"pid":2,"name":"node","cpu":8.0,"mem_mb":117.2}],
   "users":[{"name":"dev","terminal":"pts/0","host":"","started":1690000000}],
   "sys":{...}
 },
 "custom":{"myplugin":{"any":"json"}}
}
```

- 默认 60s 一帧，带 ±10% 抖动（`random.uniform(0.9, 1.1)`，用于打散 500 台峰值）。
- `agent_ver` 是**无条件字段**（与 §3.1 status 帧同义，需求①「版本作为默认上报数据」）：
  **不受 `KK_HB_ITEMS` 精简影响** —— 被砍掉的只有 `metrics` 里的采集项。此前的文档只
  在 status 帧列出它，实现里却两帧都带；现在把「默认上报」从实现细节提升为契约，
  免得日后有人优化帧体积时先砍它。
- `custom` 为自定义采集插件输出，可缺省；插件 `collect()` 超时（默认 5s，
  `KK_PLUGIN_TIMEOUT`）**或输出不可 JSON 序列化**（QR-A11：在 collect 出口即拦，
  若拖到 publish_hb 的 `json.dumps` 才失败会让整帧心跳每周期一起丢）均按失败
  同款隔离到文件 mtime 变化重载为止，不影响整帧心跳。
- 心跳采集项可用 `KK_HB_ITEMS` 精简（逗号分隔，取值同 §4.1 `kind=collect` 白名单；
  空则全采 8 项），被精简的键整组缺省，服务端按可缺省字段处理。
- 采集来源现已统一为 **psutil**（不再手工解析 /proc），跨平台；`ts` 为 Unix 秒。
- 服务端对 `cpu` / `mem_mb` 落 Float 列前做数值规整（非数字值记 `NULL`）——
  插件写坏单个指标**不会让整帧心跳丢弃**。

### 3.3 `kk/v1/{host}/result`（QoS1 分块；失败终态 QoS0）

```json
{"id":"c-123","seq":0,"total":3,"out_b64":"...","done":false,"ip":"10.0.0.5"}
{"id":"c-123","seq":2,"total":3,"out_b64":"","done":true,"ip":"10.0.0.5",
 "rc":0,"timed_out":false,"elapsed_ms":152,"truncated":false}
```

| 字段 | 说明 |
|---|---|
| `id` | 命令 ID，与 `cmd` 帧对应 |
| `seq` / `total` | 分块序号与总块数（48KB/块，输出上限 4MB 后截断） |
| `out_b64` | 该块输出的 base64（v1 的 `data_b64` 已改名） |
| `ip` | v3 起所有上行帧统一携带的自报出口 IP，白名单校验与 status/hb 同一入口 |
| `done` | 末块为 `true`，此时携带下方四个字段 |
| `rc` | 进程退出码；超时被杀为 `-1`；spawn 失败 `126/127`；**未知或非字符串 `kind` 同样恒回 `127` 回执**（QR-A12：此前畸形 kind 不发任何帧，命令会在服务端挂到超时清扫）；**`-3` 表示 Agent 未能正常交出结果**——out-queue 溢出（分块没送达）或工作队列积压满（命令被限流拒绝，`out` 里带 `task queue full`），服务端据此置 `done` + `truncated=1`（QR1125 校正旧文档漂移：commands 表**无 `failed` 态**，失败语义由 `truncated` 旗标承担，见 `_store_commands.append_result`；`failed` 只属于 updates 台账） |
| `timed_out` / `elapsed_ms` / `truncated` | 是否超时、耗时毫秒、输出是否被截断 |

关键收敛语义：**任一分块发送失败，Agent 必须补发一个 `rc=-3` 的失败终态**，
绝不让服务端那一行命令永远停在 `running`。补发的终态走 **QoS0**：此时 QoS1 队列已经满了，
同规格的终态帧会被一起挡在门外，用「这一帧可能丢」换「不被自己的积压堵住」，真丢了还有
服务端的命令超时清扫兜底。副作用可接受：已入队的低 `seq` 分块若晚于终态到达，会被服务端的
`seq` 水位判为重复而丢弃，那一行只会「输出偏少」而不会错乱。

## 4. Server → Agent

### 4.1 `kk/v1/{host}/cmd`（QoS1）

```json
{"id":"c-123","kind":"shell","argv":["du","-sh","/workspace"],"timeout":30}
{"id":"c-124","kind":"collect","items":["cpu","mem","net"],"timeout":30}
{"id":"c-125","kind":"collect","items":["cpu"],"use_shell":true,"argv":["..."],"timeout":30}
{"id":"c-126","kind":"plugin_reload","timeout":30}
{"id":"up-web-01-1690000000","kind":"update","version":"0.4.0","sha256":"<hex>",
 "size":1234567,"url":"/api/system/agent/download","sig":"<hex, 可选>"}
```

| 字段 | 说明 |
|---|---|
| `id` | 命令 ID |
| `kind` | `shell` / `collect` / `plugin_reload` / `update` |
| `argv` | `kind=shell` 时为数组直传 exec（不经 shell 拼接），`timeout` 1–600s |
| `items` | `kind=collect` 必需，取自下方白名单 |
| `use_shell` | 允许管道等 shell 语法，受 Agent `KK_ALLOW_SHELL` 约束 |
| `update` 专用 | `version` / `sha256` / `size` / `url`（相对管理 API 基址；服务端配了 `KK_PUBLIC_URL` 则为绝对地址，见 A6.1）；`sig` 为对**二进制本体**的 HMAC-SHA256（QR-P0-1，服务端上传时计算，双端 `KK_UPDATE_HMAC_KEY` 一致时携带） |

> **`kind=update` 的 `id` 是** `updates` **台账主键**（`up-<host>-<ts>`），不是 `commands`
> 表的行：Agent 的更新回执按同一 id 回传，服务端据此把结果落到台账（A6.2）。用别的
> 形式生成 id（历史实现是 `u-<host>`）会让回执被判为「未知命令」而丢掉。
> 示例里的 `version` 恒为**比当前 `AGENT_VER` 更高**的版本 —— 它是「服务端推着 Agent 升级」
> 的方向，由 `tests/test_version_governance.py` 守住，防止文档随版本演进漂移。

> **推送更新签名策略（QR-P0-1，2026-09-29 起）**：v3 的 Broker 默认匿名开放，而
> `kind=update` 等价于远程替换二进制——Agent 对**推送路径**默认拒绝未签名清单：
> 配了 `KK_UPDATE_HMAC_KEY`（签名强制，与轮询路径同闸）或显式
> `KK_UPDATE_ALLOW_UNSIGNED=1`（内网可信逃生口）才放行。`KK_UPDATE_DISABLED=1`
> 同时关掉轮询与推送两条路，回执原因码 `update_disabled` / `unsigned_push_rejected`
> 可在服务端更新台账按台核验。HTTP 轮询路径（`/agent/latest`）不强制签名，行为不变。
> 另外 Agent 侧 dispatch 按 `id` 做有界去重（QR-A1）：QoS1 重发同一命令只执行一次。

**采集项白名单（8 项，双端必须一致）**：

```
cpu, mem, disk, disk_io, net, proc, user, sys
```

- Agent 侧：`collector.ITEMS` / `ITEM_NAMES`
- Server 侧：`kk_server.config.COLLECT_ITEMS`
- 服务端校验 `items ⊆ COLLECT_ITEMS`，非法项返回 400 并列出白名单；未知项在 Agent 侧忽略。
- `kind=collect` 不经 shell，直接调 psutil 采集并返回结构化 JSON。

**命令排队**：MQTT 通道下 Agent 离线时命令由 Broker 持久会话排队（QoS1），重连自动补投。
服务端**不保留** `pending_for` 补发逻辑（已删），`mark_sent_batch` 语义即「已发布到 Broker」。
QR1125 Phase 0 起另有 **DB 命令离线队列**（`commands.status='queued'`）服务 SSE 通道：下发时
主机离线 → queued，SSE 重连由 `flush_queued_commands` 补投（先转 sent 刷计时再推），超
`KK_CMD_QUEUE_TTL`（默认 24h）由 `sweep_queued_command_timeouts` 收敛——泛化 updates 队列先例，
与在途命令的 `timeout+slack` 清扫互不干扰（见 §8）。

## 5. Agent 自更新 REST 接口

与 MQTT 通道并行，走 HTTP：

| 方法 | 路径 | 鉴权 | 说明 |
|---|---|---|---|
| POST | `/api/system/agent` | 管理员会话（AdminUser） | 上传新版本二进制（multipart: `file` + `version`），服务端算 `sha256` 并记录为最新 |
| POST | `/api/system/agent/rollback` | 管理员会话（AdminUser） | 回滚**待分发**的二进制到上一版（B2/P2-7；只换服务端待分发版，已在跑的 Agent 不降级） |
| GET | `/api/system/updates?limit=` | 登录会话 | 自更新台账（A6.2）：各状态计数 + 最近明细，「这版升了多少、谁失败了」逐台可核验 |
| GET | `/api/system/agent/current` | 登录会话 | 服务端当前待分发版本 `{version, sha256, size, uploaded_at, hosts_total, hosts_outdated}`（D1.2；与下一条**刻意分离**：鉴权与语义都不同） |
| POST | `/api/system/agent/upgrade` | 管理员会话（AdminUser） | 受控批量升级（D2.2）：人工选机推送，每台在 `updates` 台账写一行，台账主键即 cmd 帧 `id` |
| GET | `/api/system/agent/latest?ver=<当前版本>` | 请求源 IP ∈ `KK_AGENT_IPS` | 返回 `{available, version, sha256, size, url, sig, policy}`（`sig` 仅在服务端配了 `KK_UPDATE_HMAC_KEY` 时非空；响应恒带 `policy` 自描述） |
| GET | `/api/system/agent/download` | 请求源 IP ∈ `KK_AGENT_IPS` | 流式下发最新二进制（下载留审计，QR-S7） |

> **自更新策略由服务端 `KK_UPDATE_MODE` 控制（默认 `manual`）**：`manual` 下
> `/agent/latest` 恒答「无需升级」（`available=false`——这是策略而非故障），升级走
> 管理员 `POST /api/system/agent/upgrade` 选机推送；`auto` 才由各 Agent 按
> `KK_UPDATE_INTERVAL` 自动轮询拉齐。

安全边界（v3）：下载/查询按**请求真实 TCP 源 IP**校验白名单（比 MQTT 侧的自报 ip 可靠；
经反向代理时源 IP 会变成代理地址，自更新地址应配置为内网直连地址）；上传仍需管理员会话。
替换前强制 `sha256` 校验，可选 HMAC（`KK_UPDATE_HMAC_KEY` + `KK_UPDATE_REQUIRE_SIG`）。
**ed25519 签名决策不做**（需引入 pynacl，而服务端被攻陷时攻击者同样能篡改清单里的
sha256，边际收益有限）。`KK_UPDATE_INSECURE=1` 可关闭 TLS 校验（不推荐）。
服务端上传时若配了 `KK_UPDATE_HMAC_KEY` 会对二进制本体计算 HMAC 并写入清单 `sig`
（QR-P0-1）：推送路径由 Agent 强制验签，端到端防伪造更新。

## 6. 已登记但未实现

| 项 | 状态 | 说明 |
|---|---|---|
| `kk/v1/{host}/cmd_ack` | **仅登记主题位** | 曾计划用它做应用层 ACK 状态机（pending→sent→acked→running→done）。**已否决**：Broker 的 QoS1 + 持久会话已保证送达，再加一层是自研重复实现。改由服务端的 SQL 超时清扫器收敛（`sweep_command_timeouts`）。真需要时零迁移成本启用 |
| 共享订阅 `$share/{group}/...` | **默认不启用** | 解决多消费端分摊分发压力，本方案瓶颈在 DB 写入而非订阅分发；500 台单实例用不到。订阅拼装收敛在 `MqttBridge._sub_topics()`，加前缀即可扩容 |

## 7. 约束

- 帧尺寸由应用层分块机制约束：result 固定 48KB/块（base64 后约 64KB），心跳帧典型 2–4KB。
  Mosquitto 2.0.x 的 `message_size_limit` **默认 0 = 不限制**（上限仅 MQTT 协议级 256MB），
  仓库交付的 `docker/mosquitto/mosquitto.conf` 亦未设置该项——Broker 对单帧并无 1MB 上限。
  **但读帧方在 `json.loads` 前有应用层尺寸预检（QR1120-S2/A12，QR1125 校正旧文档漂移）**：
  服务端 `MqttBridge._route_message` 预检 `MAX_FRAME_BYTES = 1MB`（超限计 `oversized_dropped`
  并丢弃，HTTP ingest 侧 `frame_gate.decode_frame` 同口径回 413）；Agent `transport.py` 对入站
  cmd 预检 `MAX_CMD_BYTES = 64KB`。部署侧如需 Broker 级硬上限，仍可在 mosquitto.conf 显式配置
  `message_size_limit`。
- 所有 `ts` 为 Unix 秒。
- 命令输出分块 48KB，总量超 4MB 截断并置 `truncated`。
- Agent 侧离线 out-queue 上限 `MAX_QUEUED = 128`（`KK_MAX_QUEUED` 可配，预算口径见
  `transport.py` 注释）；队列里只可能有 status 与 result 分块，128 × ≈64KB ≈ 8MB 是断线窗口
  内单台 Agent 的额外驻留上限。超出会被淘汰，因此大输出场景下 `send_result` 必须感知入队失败
  并回 `rc=-3`（终态走 QoS0，见 §3.3）。
  回归锁：`agent/tests/test_transport.py::test_max_queued_budget_stays_inside_agent_envelope`。

## 8. HTTP/SSE 传输绑定（QR1125 Phase 0，与 MQTT 并存）

去 Broker 的轻量级直连通道（设计见 [`docs/design-transport.md`](../docs/design-transport.md)）。
**帧格式 v4 不变，仅传输绑定变**；Phase 0 与 MQTT 双通道并存写同一 store，MqttBridge 原样保留，
可灰度可回滚。Phase 0 上行 ingest 需 bridge 存在（`KK_MQTT_URL` 配置）；无 MQTT 独立运行留后续
FrameProcessor 抽取阶段。

### 8.1 上行：HTTP push（Agent → Server，出站 POST）

| 方法 | 路径 | 鉴权 | 帧体 |
|---|---|---|---|
| POST | `/api/v1/ingest/status` | 请求真实 TCP 源 IP ∈ 白名单 | §3.1 status 帧 |
| POST | `/api/v1/ingest/hb` | 同上 | §3.2 hb 帧 |
| POST | `/api/v1/ingest/result` | 同上 | §3.3 result 帧 |

- 鉴权用 `agent_ip_auth`（**真实 TCP 源 IP**，比 MQTT 自报 ip 更安全；不读 X-Forwarded-For，
  反代口径同 §5 自更新）。帧闸门 `frame_gate`：尺寸 ≤1MB（413）/ JSON 合法（422）/ host ≤120（422）；
  proto 兼容窗口在 `_on_status` 内自然生效。
- 落库**组合复用** `MqttBridge._on_status/_on_hb/_on_result`（与 MQTT 共享 store/stats/per-cid 锁/
  升级三连）。响应 200=已受理（内部拒收如未知主机/proto 窗外仍 200，计 `rejected`）；bridge 未配 → 503。
- 计数器 `ingest_status/ingest_hb/ingest_result` 与 MQTT 的 `status/hb/result` 并行累加。

### 8.2 下行：SSE 命令通道（Server → Agent，Agent 出站 GET 长连接）

| 方法 | 路径 | 鉴权 | 说明 |
|---|---|---|---|
| GET | `/api/v1/commands/stream?host=<host>` | 请求真实 TCP 源 IP ∈ 白名单 | `text/event-stream`，主机不开入站端口 |

- 事件格式 `id: <命令id>\ndata: <cmd 帧 JSON>\n\n`（cmd 帧同 §4.1）；`retry: 3000` 告知重连延迟；
  每 15s `: ping` 注释帧保活。**nginx 须 `proxy_buffering off`**（响应带 `X-Accel-Buffering: no`）。
- 连接建立即 `flush_queued_commands` 补投 DB queued 命令；断连 → `set_online(host, False)` 秒级离线
  （`mark_stale_offline` 180s 双保险）。连接表单进程内存态，多实例靠 DB queued + 重连 flush 保正确性。
- 下行路由 `get_downlink`：主机有 SSE 连接 → 直推 SSE；否则回落 `bridge.dispatch_command`（paho）。
  **Phase 0 升级帧（`kind=update`）仍走 MQTT**，SSE 升级推送留 Phase 1+。
- 命令收敛沿用 §6 裁决（不新增应用层 ACK）：`sweep_command_timeouts` + result 幂等；queued 命令走
  独立 `sweep_queued_command_timeouts`（`KK_CMD_QUEUE_TTL`）。

### 8.3 与 MQTT 的语义对照

| 能力 | MQTT 通道 | HTTP/SSE 通道 |
|---|---|---|
| 离线判定 | Broker LWT + sweep 兜底 | SSE 断连回调秒级 + sweep 兜底 |
| 上线真相 | retained status + DB | DB（`set_online`/心跳回置） |
| 命令离线排队 | Broker 持久会话 QoS1 | DB `queued` 态 + 重连 flush |
| 接入鉴权 | 自报 ip 白名单 | 真实 TCP 源 IP 白名单 |
| 至少一次幂等 | QoS1 重投 + last_seq 水位 | 同（result 处理器复用） |
