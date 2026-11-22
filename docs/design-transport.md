# 上报/采集机制轻量级 redesign 设计文档（QR1125）

> 状态：**设计评审稿**（纯文档，零代码改动）。本轮沉淀探索结论 + 可执行的分阶段迁移路径，
> 供评审后再实施；不 premature 改代码。实施估 2–3 人周，分 4 阶段独立可灰度可回滚。
>
> 关联：总体设计 [`design.md`](design.md)（尤其 §2「为什么是 MQTT」的历史教训）、
> 协议契约 [`proto/messages.md`](../proto/messages.md)（v4）、缺陷账本
> [`completion-plan-mqtt.md`](completion-plan-mqtt.md)。本文所有代码引用以当前工作树
> （HEAD `4bdca91`，QR1124/QR1126 已入树）为准，附 `文件:行号`。

---

## 1. 背景与目标

用户诉求：探索客户端上报 + 服务端采集机制，结合业界最新开源成果，**轻量级快速集成、减少依赖的服务**。

当前最显眼的依赖是独立 **Mosquitto Broker**：ubuntu 自建镜像（`docker/broker/Dockerfile:13-33`，
生产镜像清单不含 eclipse-mosquitto）+ 1883 端口（`docker/docker-compose.prod.yml:26`）+ 离线包
core tar（`docker/offline/manifest.txt:21`）。它是架构里唯一的独立中间件服务。

**目标**（按优先级）：

1. **减依赖**：去掉独立 Broker 部署单元，收敛到「FastAPI 服务端 + nginx + DB」既有栈。
2. **轻量快速集成**：复用现有 Python 栈（FastAPI/psutil），Agent 侧增量 <1MB，不引入 C 扩展/protobuf 编译链。
3. **离线内网合规**：生产镜像清单（python-3.12 / node-20 / nginx-1.20 / postgres:18.6 / redis / mongo / ubuntu，
   `AGENTS.md:20`）完全覆盖，无新镜像、无运行时网络依赖。
4. **安全模型不变**：保持「Agent 出站单向、主机不开入站端口」（`design.md:40-41`）；接入管控沿用
   IP 白名单（QR1124 动态名单），且 HTTP 真实源 IP 比 MQTT 自报 ip 更安全。
5. **可靠性不降级**：离线命令排队 + 重连补投、掉线判定、至少一次幂等——这些当前由 Broker 承担的职责，
   必须有等价补偿（§3.3 映射、§5 详设）。

**非目标**：不追求「零状态服务端」（上行无状态即可，下行连接态天然有状态）；不引入消息队列中间件
（NATS/Kafka 等违背减依赖）；不改协议帧格式（v4 字段不变，仅传输绑定变）。

---

## 2. 现状机制基线

### 2.1 Agent 侧（`agent/src/kk_agent/`）

| 模块 | 职责 | 关键事实（file:line） |
|---|---|---|
| `transport.py` | paho-mqtt 2.1.0 传输 | 重连退避 `RECONNECT_MIN/MAX=1/60`（:44-45,193），**paho 纯指数倍增、无抖动**（paho client.py:4531-4537）；keepalive 默认 60 下限 10（:231；config.py:60）；out-queue `MAX_QUEUED=128`≈8MB 显式预算（:38-43,192；config.py:99 下限 16）；LWT `will_set(status, offline帧, qos=1, retain=True)`（:189-191）；QoS 分级 `QOS_HB=0/QOS_CMD=1`（:36-37），hb QoS0 不 retain（:351）、status QoS1+retain（:298）、result QoS1（rc=-3 终态例外 QoS0，:353-364）；自报 ip 探测 `detect_outbound_ip` UDP connect 选路由不发包（:105-120），`KK_ADVERTISE_IP` 可覆盖（:145-146）；`announce_update` reason=updating **真等 PUBACK 上限 1s**（:51,316-337，wait 后必问 is_published :307-309）；publish 线程安全（main.py:7）；入站防线 cmd 64KB 预检（:55,417-426）；持久会话 `clean_session=False`（:168） |
| `collector.py` | psutil 采集 | 8 项 `HB_DEFAULT_ITEMS=["cpu","mem","disk","disk_io","net","proc","user","sys"]`（:332）；`collect_items()` 单项失败跳过留痕（:304-325,288-296）；速率差分负值钳 0（:101-113）；proc 双采样 0.3s（:160-174） |
| `plugin_loader.py` | 插件热加载 | mtime 比较（:121-133）；卡死隔离到 mtime 变化（:159-169）；collect/exec 超时 daemon 线程（:43-106）；单插件输出封顶 64KB（:40） |
| `main.py` | 单线程事件循环 | `SCHED_TICK_SEC=0.5` 单调钟调度（:35,300-317）；心跳一次性 daemon 线程 + busy Event 防重入（:211-215,230）；命令 LRU 去重 `DEDUP_MAX=512` seen_cids OrderedDict（:31,171,185-187）；结果分块 `CHUNK=48*1024`（base64 前，:27），任一块入队失败补发 rc=-3 urgent QoS0（:50-54,82-85）；心跳 ±10% 抖动（:307）；更新轮询 ±20% + 首拍 uniform 防惊群（:297,315） |
| `executor.py` | 命令执行 | argv 直 exec vs `sh -c`（use_shell 双闸 KK_ALLOW_SHELL，:54-61,222-224）；进程树回收 POSIX `os.killpg` TERM→3s→KILL（:78-89）/ Windows `taskkill /F /T`（:91-97）；超时钳 1..600 默认 30 畸形回落（:216-221）；输出封顶 4MB（:106-131）；积压 `MAX_PENDING=64` 溢出 rc=-3（:28,244-249） |
| `updater.py` | 自更新 | **纯标准库 urllib HTTP 轮询**（:29-30,219-240）；sha256 必需 + HMAC 同趟增量 `compare_digest`（:280-312）；execv 自更新 + `.kkbak` 回滚 + `os.replace` 原子替换（:569,373,389）；`apply_manifest_receipt` 轮询+推送唯一门禁（:463-481）；失败退避 `FAIL_BACKOFF=(300,600,1800)`（:60）；推送无 key 默认拒（:264-277，QR-P0-1）；二进制封顶 64MB（:35） |
| `config.py` | KK_* 配置 | `PROTO_VER=4`（:10）、`AGENT_VER="0.3.0"`（:7）；**Agent 用 `KK_SERVER`（:56）不是 KK_MQTT_URL**（后者是服务端键 server/config.py:152）；心跳键是 **`KK_INTERVAL` 下限 1s**（:81，非 KK_HB_INTERVAL）；**`KK_TRANSPORT` 当前不存在**（全仓 grep 零命中，MQTT 是唯一传输） |

打包：PyInstaller `--onefile`（`agent/build/build_binary.sh:11-17`，入口 `__main__.py:15-24`）。
**RSS 25–35MB 是文档预期口径非实测**（`design.md:77`、`AGENTS.md:24`；基准脚本 `scripts/bench_agent.py:25`
`TARGET_MB=40.0` 含余量）。

### 2.2 服务端（`server/src/kk_server/`）

**MqttBridge**（`services/mqtt_bridge.py`，705 行，自述「无状态桥」:1-19）：paho 网络线程 →
`loop.call_soon_threadsafe` 回事件循环落库。`start`（:150-155）/`stop` 先 disconnect 再 loop_stop（:157-167）。

`_route_message` 闸门顺序（**修正：proto 闸门不在此处**）：topic 解析 → host 长度 ≤120（:243）→
帧尺寸 ≤1MB（:250）→ JSON → 路由映射（:262）→ **IP 白名单**（:268 `ip_in_whitelist(self.agent_ips, body["ip"])`）→
帧归属（:279 reported==host）→ dispatch（:286）。**proto 版本闸门在 `_on_status` 内**（:374-386，
`proto not in accept_proto_vers` → rejected + proto_mismatch 审计）。

三类帧处理器（均 `async def (self, host, body)`，事件循环线程）：
- `_on_status`（:370-414）：proto 窗口校验 → v3 计数（:389-390）→ `store.set_online`（:395-406，v4 meta 只上线路径落库）→
  online 时 **升级三连** `finish_updates_reaching`（:411）+ `flush_queued_upgrades`（:413）+ `_maybe_push_upgrade`（:414）。
- `_on_hb`（:416-425）：未注册主机拒收（:417-422）→ `store.record_hb`（:423）→ `_check_interval`（:425,427-450，低于下限只审计+计数不阻断）。
- `_on_result`（:452-491）：`up-` 前缀分流 `_on_update_result`（:459-460）；否则 **per-cid asyncio.Lock 保序**（:462-468）→
  `get_command` → 归属校验 `cmd["pod"]!=host`（:476-484）→ `store.append_result`（:485）→ done 回收锁（:489-491）。

stats 计数器（:105-122，唯一写入口 `_bump` 加锁 :203-206）：`status/hb/result/rejected/cmd_published/cmd_failed/
upgrade_pushed/sweeps/swept_timeouts/swept_offline/upgrade_done/upgrade_failed/interval_violation/proto_v3_received/
bridge_errors/oversized_dropped/host_too_long_dropped/last_msg_ts/started_at` + 动态 `upgrade_flushed`（:678）/`swept_updates`（:702）。
**修正：无 `transport_mqtt_received` 键**（全仓 grep 零命中）——按传输维度治理需新增计数器（§6 Phase 2）。

sweep `SWEEP_INTERVAL=30`（:52,683-705）= `sweep_command_timeouts` + `mark_stale_offline(OFFLINE_GRACE)` +
`sweep_update_timeouts` + 死锁回收（600s 未用锁 pop :694-698）；由 main.py janitor 每 30s 调（main.py:91-95）。

**flush_queued_upgrades**（:655-680，DB 命令队列 + 重连补投的现成先例）：
```python
latest = await self.store.get_agent_latest()        # 无最新版本 return 0
rows = await self.store.queued_updates(host)
for row in rows:
    if row["to_version"] != latest_ver: continue    # 只补投最新版本
    if agent_ver and not version_lt(agent_ver, row["to_version"]): continue  # 已达标不投
    await self.store.mark_update_pending(row["id"]) # 先转 pending 刷计时再发帧（防重启重投窗口）
    await self.publish_update(host, row["id"], latest)
```
触发点：主机上线 `_on_status` online 分支（:413）。

**commands 状态机**（`models/_store_commands.py` + `controllers/commands.py`）：
- `create_commands_batch`（:27-45）：id `c-<hex12>`、batch `b-<hex16>`，初始 `status="pending"`。
- **status 实际取值（修正：commands 无 queued、服务端从不写 failed）**：`pending`（建行）→ `sent`
  （mark_sent_batch :161-179，语义=已交给 Broker）→ `running`（append_result）→ `done`（**rc=-3 也落 done + truncated=1**，:234-236）；
  `timeout`（sweep_command_timeouts :246-258，扫 pending/sent/running）；`lost`（cleanup 把超 1h 仍 sent/running 盖 lost，_store_maintenance.py:80-83）。
  **`queued`/`failed` 只属于 updates 表**（tables.py:156-157）。前端 kk.ts:138-144 映射 failed/queued 是防御性遗留。
- `append_result`（:181-244）：base64 SQL 拼接 `out_b64 + chunk`（:220）；**last_seq CASE 严格水位**（:227-232，绑规整 int，QR1120-S8）；out_tail 镜像列随块维护。
- `dispatch_command`（mqtt_bridge.py:543-577）：paho 非阻塞 publish（:563-564），`rc in (SUCCESS, NO_CONN)` 算成功（:569）；
  控制器先批量建行再逐条发布 + mark_sent_batch（commands.py:139-145）。

**updates 队列先例**（flush_queued_commands 的模板）：`queued_updates(pod)`（:324-329）/`mark_update_pending`
（:331-342，**queued→pending 且刷 created_at**，否则补投瞬间被 30min 窗口误判 timeout）/`finish_updates_reaching`（:344-370）/
`sweep_update_timeouts`（:372-388，**pending 30min / queued 7d** 双档）。批量建行分组手法见 agent_update.py:400-423
（按 (from_version, is_online) 分组，在线判定 `store.online_set(ONLINE_GRACE)`）。

**在线判定**（`models/_store_hosts.py`）：`set_online`（:197-271，离线帧恒用服务器时间，空 reason LWT 120s 内不覆盖 updating :22,246-250）；
`mark_stale_offline`（:468-479，**CASE 版 max(3*hb_interval, 180)**，`ONLINE_GRACE=180` tables.py:275）；
`record_hb` 双语句事务（:273-328，INSERT heartbeats + UPDATE containers，**心跳回置 online=1 是更强在线证据** :301-307 QR1120-S6，对抗旧会话 LWT 误杀）。

**IP 白名单（QR1124 后）**：MQTT 侧 `self.agent_ips` 快照校验（mqtt_bridge.py:268）+ `_on_ip_rejected` 按 IP 60s 节流（:339-360）；
**REST 真实 TCP 源 IP** `deps.agent_ip_auth`（deps.py:23-34，`request.client.host`，消费点 /agent/latest、/agent/download）；
`refresh_agent_ips`（deps.py:57-69，原子替换 bridge.agent_ips + app.state.agent_ips 两处）。

**REST 入口**：`create_app` 工厂（main.py:32-122），lifespan 顺序 setup→ensure_admin→seed_ip_rules→ensure_whitelist→refresh_agent_ips→bridge.start（:55-85）；
**无 WS 入口**（agent_ws.py/hub.py 已删，controllers/__init__.py:1）；路由清单 health/auth/containers/commands/exporting/audit/stats/rbac/ipwhitelist/agent_update（__init__.py:7-17）。

### 2.3 协议 v4（`proto/messages.md`）

主题 `kk/v1/{host}/{status,hb,result,cmd}`（:24-30，cmd_ack 仅登记主题位未实现，§6 否决应用层 ACK 改 sweep 收敛）；
QoS/retain 硬约束表（:36-41）；三类上行帧 status §3.1 / hb §3.2 / result §3.3（seq/total/out_b64/done/rc，48KB/块 4MB 截断）；
cmd 下行 §4.1（kind=shell/collect/plugin_reload/update）；**命令排队声明**（:257-258「Agent 离线时命令由 Broker 持久会话排队…
服务端不保留 pending_for 补发逻辑（已删）」）；兼容窗口 `ACCEPT_PROTO_VERS=(3,4)`（__init__.py:13），`KK_DROP_PROTO_V3=1` 收缩，
关窗口判据 `proto_v3_received`（:146-149）；§2.1 动态白名单（QR1124 已同步 :75-83）；§3.1.1 v4 可选字段。

**两处文档漂移（设计取准按代码，实施时顺手修文档）**：
1. messages.md:296-301 称「读帧方不预检尺寸」——**已过时**：服务端预检 1MB（mqtt_bridge.py:64,250-254）、Agent 预检 cmd 64KB（transport.py:55,417-426）。
2. messages.md:201 「rc=-3…服务端置 failed」——**实现是 done+truncated=1**（_store_commands.py:234-236），commands 无 failed 写入点。

### 2.4 部署依赖足迹

compose prod/offline（mosquitto + kk-server，1883，`KK_MQTT_URL=mqtt://mosquitto:1883`）；broker Dockerfile（ubuntu+apt mosquitto）；
离线 manifest kk-broker core tar；`scripts/build.sh`（agent 叠加 vscode-server，:33 编译单文件→:60-61 COPY→叠加 Dockerfile）；
非容器主机 `scripts/install.sh` + `deploy/systemd/kk-agent.service`（Restart=always，刻意不做沙箱 :15-31）；
web 展示 broker 状态：stats.py:27-32 `connected=bridge.connected.is_set()`、welcome/index.vue:91-95、system/index.vue:115-222 `COUNTER_META`。

### 2.5 Broker 职责 vs 服务端已有兜底（redesign 的可行性基础）

| 能力 | 当前主承担者 | 已有兜底（file:line） | 去 Broker 后补偿难度 |
|---|---|---|---|
| 离线判定（异常掉线） | Broker LWT（transport.py:189-191） | sweep mark_stale_offline max(3*hb,180)（_store_hosts.py:468-479）+ 优雅 stop 主动 offline（transport.py:250-272）+ updating 120s 宽限 | **低**：SSE 断连回调秒级置 offline + sweep 双保险 |
| 上线真相 | Broker retained status | set_online 落库 + **心跳回置 online**（_store_hosts.py:301-307）+ online_set 同口径宽限 | **低**：DB 已是真相源，retained 只是回放缓存 |
| **命令离线排队 + 补投** | **Broker 持久会话 + QoS1**（clean_session=False） | commands **无 queued/无补投**；但 **updates 域已有完整先例**（queued + flush_queued_upgrades + 7d TTL） | **中**：泛化 updates 模板到 commands（§5.2），是唯一需要新建的补偿 |
| 重连退避 | 双端 paho 1→60s 倍增（无抖动） | 应用层 hb ±10% / 更新 ±20% 抖动 | **低**：Agent 自写退避 + Full 抖动（业界标准） |
| 至少一次幂等 | Broker QoS1 重投 | Agent LRU DEDUP_MAX=512 + 服务端 last_seq 水位 + per-cid 锁 + 更新终态幂等 | **低**：补偿层已建成，传输换了仍复用 |
| 半开连接 | MQTT keepalive Broker 强断 | record_hb 回置 online + mark_stale_offline 兜底 | **低**：SSE ping 15s + 读超时纪律 |
| Agent 离线缓存（上行） | paho out-queue 128 帧≈8MB | QoS0 hb 断线即丢 + 溢出 rc=-3 终态 + 服务端超时清扫 | **低**：有界缓冲沿用，truncated 终态明示缺口 |

**结论**：可靠性补偿本仓 DB 层已建成约 70%，Broker 唯一不可替代职责是「命令离线队列 + 补投」，
而 updates 域的 `flush_queued_upgrades` 正是现成模板——泛化即可，不是从零自研。

---

## 3. 业界开源调研（2024-2026 活跃，查询日 2026-11-22）

### 3.1 项目速览表

| 项目 | 架构/传输 | 独立中间件 | Agent 语言/体积 | 下行命令 | 离线友好 | 许可 | star/活跃/版本 | 来源 |
|---|---|---|---|---|---|---|---|---|
| **Beszel** | hub(PocketBase)+agent，**agent 主动出站 WebSocket**（CBOR/gws），SSH fallback | **不需**，仅 hub | Go 静态二进制 scratch，tar.gz **4.52MB** | **无**（安全文档明示 SSH 模式不可执行命令） | 高 | MIT | **27,537★**，pushed 2026-11-20，v0.21.2 | [github](https://github.com/henrygd/beszel)；agent/client.go、internal/hub/agent_connect.go；[docs](https://github.com/henrygd/beszel-docs) security.md/reverse-proxy.md |
| **Netdata** | child→parent **HTTP 流式** + replication 回填 | 不需 | C，child 约 30MiB RAM | 无 | 高（体积大） | GPL-3.0 | **81,575★**，v2.13.0 | [github](https://github.com/netdata/netdata)；src/streaming/stream.conf；[learn](https://learn.netdata.cloud/docs/streaming) |
| **Glances** | REST/XML-RPC client-server，**被监控机开入站端口** | 不需 | **Python**（同栈），wheel 957KB | 无 | 中（开端口被拉，不符安全模型） | LGPL-3.0 | **34,001★**，v4.5.9 | [github](https://github.com/nicolargo/glances)；[pypi](https://pypi.org/project/glances/) |
| **Uptime Kuma** | 无 agent，拉取 + **Push 心跳** `/api/push/:token` 超间隔判 DOWN | 不需（自身即服务） | 无 agent（Node） | 无 | 高 | MIT | **94,744★**，2.5.12 | [github](https://github.com/louislam/uptime-kuma)；server/routers/api-router.js |
| **Checkmk** | 默认拉取 TCP 6556；**Push 仅 Ultimate 版** | 不需 | shell+二进制 | 有（本地检查） | 中（Raw 只拉取） | GPL-2.0 | 2,654★（镜像仓），v2.5.0p20 | [github](https://github.com/Checkmk/checkmk)；[docs](https://docs.checkmk.com/latest/en/agent_linux.html) |
| **Zabbix agent2** | **主动检查出站** 10051 拉 item 列表周期上报；被动 10050 | 不需 broker（server 重） | **Go** | **有** system.run[] **默认拒**需 AllowKey | 中（server 栈重） | AGPL-3.0 | **6,589★**，8.0.0 | [github](https://github.com/zabbix/zabbix)；[agent2 concepts](https://www.zabbix.com/documentation/current/en/manual/concepts/agent2) |
| **node_exporter** | Prometheus 拉取，开入站 9100 | 需 Prometheus | Go | 无 | 中（开端口被拉） | Apache-2.0 | **13,952★**，v1.13.1 | [github](https://github.com/prometheus/node_exporter) |
| **Pushgateway** | Prometheus 推送**中转缓存** | **它本身即中间件** | Go | 无 | 低（官方反对常驻主机） | Apache-2.0 | 3,375★，v1.11.4 | [github](https://github.com/prometheus/pushgateway)；[pushing practices](https://prometheus.io/docs/practices/pushing/) |
| **Grafana Alloy** | OTel Collector 发行版 | 不需 broker（需后端栈） | Go | 无（Fleet 是配置管理） | 中 | Apache-2.0 | 3,702★，v1.22.0 | [github](https://github.com/grafana/alloy) |
| **Vector** | source→transform→sink，**http sink 直发 POST** at-least-once + Fibonacci 退避 + Full 抖动 | **不需** | Rust（偏大） | 无 | 高 | MPL-2.0 | **22,931★**，v0.62.0 | [github](https://github.com/vectordotdev/vector)；[http sink](https://vector.dev/docs/reference/configuration/sinks/http/) |
| **Fluent Bit** | **http output** POST JSON/msgpack + 重试 DLQ | **不需** | C 极轻 | 无 | 高 | Apache-2.0 | 8,204★，v5.2.0 | [github](https://github.com/fluent/fluent-bit)；[http output](https://docs.fluentbit.io/manual/pipeline/outputs/http) |
| **Telegraf** | **outputs.http** POST 批量指标 | **不需** | Go | 无 | 高 | MIT | **17,917★**，v1.40.3 | [github](https://github.com/influxdata/telegraf)；plugins/outputs/http |
| **amqtt**（对照） | **纯 Python asyncio broker+client** 可嵌入 | 嵌入即无独立服务 | Python wheel **132.9KB** | pub/sub 可承载 | 高（无新镜像） | MIT | **222★**，v0.12.2，47 open issues | [github](https://github.com/Yakifo/amqtt)；[pypi](https://pypi.org/project/amqtt/) |
| **Mosquitto**（对照） | 独立 C broker | **是**（要去除的对象） | C | topic 下发 | 镜像清单无→不合规 | EPL-2.0/BSD | **11,481★**，v2.1.4 | [github](https://github.com/eclipse-mosquitto/mosquitto) |
| **NATS**（对照） | 独立 Go messaging server | **是**（清单无 nats 镜像） | Go server / nats-py 95.5KB | pub/sub | **低（不减依赖）** | Apache-2.0 | server 21,100★ v2.15.3 | [github](https://github.com/nats-io/nats-server) |

**Beszel 源码级要点（对本仓最有参考价值）**：连接方向 agent 主动出站 WS（security.md 原文 "The WebSocket connection is
initiated by the agent"）；**在线判定=连接态**（internal/hub/systems/system.go `DownChan` 触发即 setDown，up/down 写 hub DB）；
保活 `wsDeadline=120s` 读超时容忍慢采集 + `wsTickerInterval=10s` 固定重连节拍 + isConnecting 防并发重连；
nginx 反代 `proxy_http_version 1.1 + Upgrade 头 + proxy_read_timeout 360s`。**它证明「agent 出站长连接 + 连接态判在线 + DB 存状态」
的轻量直连架构两年做到 27.5k★**——是本仓方向的最强背书。

### 3.2 传输协议对比表

| 协议 | 方向 | FastAPI 生态 | Agent 成本 | nginx 穿透 | 断线重连 | 离线队列 | 多实例 | 离线合规 | 结论 |
|---|---|---|---|---|---|---|---|---|---|
| **HTTP push** | Agent→Server 出站 | 原生路由最成熟 | 极低 httpx/urllib | 普通反代零配置 | 每请求独立天然重试 | Agent 有界缓冲 + DB | **最好：上行无状态** | 完全合规 | **上行首选** |
| **SSE** | Server→Agent，**Agent 主动出站 GET 长连接** | sse-starlette 3.7.0（860★ BSD-3，18KB，ping 默认 15s） | 低 httpx stream 逐行解析；**Last-Event-ID 续传需应用层实现**（库不自动回放） | `proxy_buffering off` + read_timeout>ping | 自写重连循环带 Last-Event-ID | DB 队列连接建立 flush | 好：命令存 DB 重连任意实例补投 | 完全合规 | **下行首选** |
| **WebSocket** | 双向 Agent 出站 | FastAPI 原生，**官方警告连接管理器单进程，多 worker 需 Redis/broadcaster** | 中 websockets + 心跳重连状态机 | Upgrade 头 + http 1.1 | 自写 ping/pong | DB 队列 | 中：连接绑进程跨实例需 pub/sub | 合规 | 可行但比 SSE 多一套双向状态机 |
| **long-polling** | Agent 出站 GET 挂起 | 原生但占 worker 协程 | 低 | 调大 read timeout | 每轮即重连 | DB 队列 | 好 | 合规 | 兜底，效率低于 SSE |
| **gRPC 双向流** | 双向 | 需 grpcio + 自写 servicer | **高：grpcio cp312 linux wheel 7,188KB≈7.4MB C++ C 扩展** + protobuf 编译链 | grpc_pass HTTP/2 复杂 | 内建 keepalive | 自写 | 中 | **不合规风险：冲击 25-35MB RSS 预算** | **排除** |
| **嵌入式 MQTT(amqtt)** | 双向 pub/sub | 同进程 asyncio 可嵌 | 低（paho 已在栈） | ws listener 需升级头 | MQTT keepalive | broker 内存会话 | 差（状态锁单进程） | 合规 | **仅应急过渡** |
| **NATS** | 双向 pub/sub | nats-py 2.17.0 | 低 | TCP 4222/WS | 内建重连 | JetStream | 好 | **不合规：清单无 nats-server，不减依赖** | **排除** |
| **OTLP** | Agent→Server 单向 Export | 需自写 protobuf receiver | 高 OTel proto 编译链 | 普通 POST | 规范重试退避 | SDK 批量 | 好 | 合规但负收益 | **排除：无下行命令机制** |

SSE 规范依据：[WHATWG HTML §9.2](https://html.spec.whatwg.org/multipage/server-sent-events.html)（重连设 Last-Event-ID 头、retry 字段、注释行 keepalive）；
[MDN](https://developer.mozilla.org/en-US/docs/Web/API/Server-sent_events/Using_server-sent_events)。
OTLP：[spec v1.11.1](https://opentelemetry.io/docs/specs/otlp/)（仅 Export，无下行）。
FastAPI WS：[advanced/websockets](https://fastapi.tiangolo.com/advanced/websockets/)（单进程警告）。

### 3.3 去中间件可靠性补偿——业界做法 → 本仓映射

| 补偿点 | 业界谁这么做（参数） | 来源 | 本仓映射（HTTP push + SSE） |
|---|---|---|---|
| 离线判定：连接态 | Beszel DownChan→setDown，120s 读 deadline | beszel system.go/client.go | SSE 断连回调→立即 offline（秒级） |
| 离线判定：数据超时 | Netdata Live/Stale/Offline + timeout 5m；Zabbix nodata()；Uptime Kuma push 超间隔判 DOWN | netdata stream.conf；zabbix triggers；uptime-kuma | 双保险：连接态为主 + last_seen 超时（3×push 间隔）兜底「连接活但采集卡死」 |
| 上线真相源 DB | Beszel up/down 是 hub DB 字段；MQTT retain 无标准查询接口、命令不应 retain | beszel system_manager.go；[hivemq retain](https://www.hivemq.com/blog/mqtt-essentials-part-8-retained-messages/) | 在线态=PG 行（status+last_seen），SSE 事件只写 DB，前端查 DB |
| 命令离线排队+补投 | Zabbix 主动检查重连重拉（RefreshActiveChecks=120s）；Netdata 重连 flush 缓冲+replication | zabbix agent2 config；netdata stream.conf | 命令存 DB queued；SSE 连接建立按 host flush→下发（泛化 flush_queued_upgrades） |
| 重连退避+抖动 | Vector retry_initial 1s Fibonacci max 30s **Full 抖动**；OTLP Retry-After；Beszel 10s ticker+互斥 | vector http sink；otlp；beszel | Agent 指数退避 1→30s + Full 抖动；服务端 429+Retry-After |
| 至少一次+幂等 | Vector http at-least-once+ack；SSE id+Last-Event-ID 天然 seq 水位 | vector；whatwg | 指标 (host,seq) 去重；命令 cmd_id + ACK 销账；SSE 事件 id=seq 重连续传 |
| 半开连接检测 | Beszel wsDeadline 120s；sse-starlette ping 15s + send_timeout + is_disconnected | beszel；[sse-starlette](https://pypi.org/project/sse-starlette/) | SSE 15s ping 注释帧；nginx read_timeout>ping；Agent 45s 无帧即重连 |
| Agent 离线缓存 | Netdata buffer 10MiB 有界「flush on reconnects, will not prevent gaps」明示缺口；Zabbix agent2 BufferSize=1000 + SQLite 持久缓冲 PersistentBufferPeriod=1h | netdata stream.conf；zabbix agent2 | 有界环形缓冲（沿用 128 帧/8MB）；超限丢最旧 + truncated=true 终态（学 Netdata 明示缺口） |

---

## 4. 方案对比与推荐

| 维度 | **A：HTTP push + SSE** | B：amqtt 嵌入 | C：保留 Mosquitto |
|---|---|---|---|
| 依赖服务数 | **减 1**（去 Broker，收敛 nginx+FastAPI+DB） | 减 1（broker 进程内嵌） | 不变 |
| 改动量 | 大（2-3 人周，传输层 + 补偿层 + 灰度） | 中（嵌 broker + 配置） | 0 |
| 离线合规 | 完全合规（无新镜像） | 合规（纯 Python） | 需 ubuntu 自建 broker |
| 可靠性 | DB 补偿（updates 先例泛化），自管重连/半开 | 仍 MQTT 语义，但 222★ 库成熟度低 | 成熟 C broker 全包 |
| 复用现有栈 | **最高**（FastAPI 原生 + psutil 保留 + urllib/httpx） | 中（paho 保留 + amqtt） | 最高 |
| 多实例 | 上行无状态零改动；下行 DB 队列 + sticky/轮询兜底 | 差（broker 状态锁单进程） | 好（共享订阅） |
| 风险 | 自管重连/半开/nginx SSE 缓冲/多实例下行路由 | **方向错误**：222★ Python 库换 11.5k★ C broker | 无（但依赖不减） |

**推荐 A**，逐条理由：
1. **真正减依赖**：唯一去掉独立中间件的方案；B 只是把 broker 搬进进程（语义负担仍在），C 不减。
2. **FastAPI 原生零新中间件**：上行普通 POST；下行 sse-starlette 仅 18KB wheel、BSD-3、860★、2026-11 仍发版，
   ping/断连/超时内建。nginx 只需 `proxy_buffering off`（比 WS 的 Upgrade 头 + 多 worker pub/sub 更简）。
3. **安全模型不变**：SSE 是 Agent 主动出站 GET 长连接，主机不开入站端口（与 Beszel 同构）；HTTP 真实源 IP 白名单比自报 ip 更安全。
4. **纯 Python 小包**：httpx 71.8KB + SSE 解析几十行，psutil 保留、paho 移除，RSS 预算几乎零增量（对比 grpcio 单 wheel 7.4MB C 扩展）。
5. **上行无状态利多实例**：POST 落任意 worker；下行命令存 DB，重连任意实例按 seq flush，无需 sticky 也无需 Redis pub/sub（实时跨实例推可后补）。
6. **业界同构验证**：Uptime Kuma push（94.7k★）证明出站 HTTP + 超时判离线大规模可行；Vector/Fluent Bit/Telegraf http 输出证明无中间件直推是采集器主流；Beszel（27.5k★ MIT）证明轻量直连架构成立。

**B 仅应急过渡**：amqtt 222★、47 open issues、0.12.x 快速变动的纯 Python 库，替换 11,481★ v2.1.4 成熟 C broker，
**用成熟度低两个数量级的组件承担 broker 职责，风险方向错误**；它只解决「镜像清单无 mosquitto」的合规问题，不解决架构问题。
若 A 改造受阻可作短期应急桥，但标为过渡态而非目标态。

**排除 gRPC/OTLP/NATS**：gRPC wheel 7.4MB C++ 扩展冲击 RSS 预算 + protobuf 编译链 + nginx HTTP/2 复杂度；
OTLP v1.11.1 只有 Export **无下行命令通道**，仍需另建命令通道，引入整套 OTel 语义是过度设计；
NATS nats-server 不在镜像清单，「去 Mosquitto 换 NATS」依赖不减反增运维面。
**Pushgateway 反面教材**（[官方四条反对](https://prometheus.io/docs/practices/pushing/)：单点瓶颈/丢 up 指标/陈旧残留/生命周期脱钩）
恰好论证：推送必须直达自有服务端并自管状态（DB），而非推给无状态语义的中转缓存。

---

## 5. 推荐方案详细设计（A）

### 5.1 上行：HTTP push 指标

Agent 用 httpx（或 urllib 兜底）POST，**帧格式 v4 不变**（仅传输绑定变）：

```
POST /api/v1/ingest/status   body=status 帧（online/agent_ver/ip/env/group/labels/caps/proto_ver...）
POST /api/v1/ingest/hb       body=hb 帧（interval/metrics/cpu/mem_mb...）
POST /api/v1/ingest/result   body=result 帧（id/seq/total/out_b64/done/rc...）
```

- **鉴权**：复用 `deps.agent_ip_auth`（deps.py:23-34，**真实 TCP 源 IP** `request.client.host` 按 `app.state.agent_ips` 白名单校验，
   403 拒绝）。比 MQTT 自报 ip 更安全（不可伪造）；反代场景需内网直连或可信代理头（沿用 agent_update 先例口径）。
- **帧归属**：host 取自帧体（与 MQTT 一致），校验 reported==host（沿用 mqtt_bridge.py:279 语义）。
- **处理复用**：抽 transport-agnostic **FrameProcessor**（§5.3），HTTP 路由与 MqttBridge 共用 `on_status/on_hb/on_result`，
   写同一 store。result 分块 + last_seq 水位幂等 + per-cid 锁保序全部复用（_store_commands.py:227-232、mqtt_bridge.py:462-468）。
- **响应**：200 落库成功；403 白名单外；413 帧超 1MB（沿用预检）；429 + Retry-After 节流（可选，防误配高频）。
- **Agent 侧**：有界离线队列沿用 128 帧/8MB（transport.py:38-43 语义），断网缓存、重连 flush；超限丢最旧 + truncated 终态；
   重推指数退避 1→30s + Full 抖动（学 Vector，**修正 paho 无抖动的现状**）。

### 5.2 下行：SSE 命令通道

```
GET /api/v1/commands/stream?host=<host>      Accept: text/event-stream
```

- **鉴权**：同 `agent_ip_auth`（真实源 IP）。Agent 主动出站 GET 长连接，**主机不开入站端口**。
- **库**：sse-starlette `EventSourceResponse`（async generator），`ping=15s` 注释帧保活，`send_timeout`/`request.is_disconnected()` 断连检测。
   **Last-Event-ID 续传是应用层职责**（库不自动回放）：读请求头 `Last-Event-ID`，从 DB 命令水位补发。
- **连接表**：单进程内存 `dict[host] -> connection`（asyncio.Queue 或 send channel）。多实例：sticky（nginx ip_hash/cookie）为优化，
   **DB 队列 + 轮询兜底保正确性**（agent 重连任意实例都能 flush 自己的 queued 命令）。
- **命令下发状态机**（commands 表**新增 queued 态**，泛化 updates 模板）：
  ```
  建行 create_commands_batch → pending
    ├─ 主机在线（SSE 连接在）→ 直推事件（id=seq）→ mark_sent → sent
    └─ 主机离线（无连接）→ queued（DB 排队，等重连）
  SSE 连接建立 / status online → flush_queued_commands(host)：
    取 queued 行 → mark_sent（刷计时）→ 推事件 → sent
  sent → running（append_result）→ done（rc=-3 也 done+truncated）
  sweep：pending/sent/running 超时 → timeout；queued 超 TTL → timeout(queue_expired)；cleanup 超 1h sent/running → lost
  ```
- **flush_queued_commands**（新建，参照 flush_queued_upgrades mqtt_bridge.py:655-680）：
  ```python
  async def flush_queued_commands(self, host):
      rows = await self.store.queued_commands(host)        # 新增：WHERE pod=? AND status='queued'
      for row in rows:
          await self.store.mark_command_sent(row["id"])    # 先转 sent 刷计时再推（防重启重投窗口）
          await self.downlink.push_command(host, row)      # SSE 事件 id=seq；连接不在则回 queued
      self._bump("cmd_flushed")
  ```
  触发点：SSE 连接建立 + `_on_status` online 分支（与 flush_queued_upgrades 并列）。
- **断连置 offline**：SSE 连接断开回调 → `store.set_online(host, False)`（秒级）+ sweep mark_stale_offline 180s 双保险
   （覆盖「连接活但采集卡死」与「服务端没收到断连事件」）。
- **命令 TTL**：新增 `KK_CMD_QUEUE_TTL`（queued 保留时长，建议默认 24h，下限 3600，沿用 KK_UPDATE_QUEUE_TTL 口径 config.py:165-166）；
   sweep_command_timeouts 增 queued 档。**部分索引 idx_cmd_inflight 谓词同步**（当前 pending/sent/running，test_dialects.py:251-253 锁）——
   queued 是否纳入在途索引需裁决（建议 queued 单独 idx_cmd_queued 或扩谓词，二者都要同步改锁）。
- **ACK**：沿用 proto §6 否决应用层 ACK 的裁决——命令收敛靠 sweep_command_timeouts + result 终态，不新增 ACK 帧。
   SSE 事件 id=seq 提供续传水位，Agent 执行去重靠 seen_cids LRU（main.py:31）。

### 5.3 FrameProcessor 抽取（Phase 0 核心）

当前三处理器深度耦合 MqttBridge 实例状态，抽成 transport-agnostic 需注入以下依赖（耦合清单 mqtt_bridge.py）：

| 依赖 | 耦合点 | 抽取方式 |
|---|---|---|
| store.* | :395,411,417,423,469,485,661,674 | 注入 store 接口 |
| stats/_bump | :203-206,379,407,424,486 | 注入 stats sink（计数器按传输打 tag） |
| accept_proto_vers/proto_ver | :78-81,378 | 注入配置 |
| _audit_throttled/_audit_ts/_reject_ts | :311-326,347-360 | 随处理器迁移（节流状态共享） |
| _result_locks/_result_lock_ts | :96,462-467,489-491 | 注入（与事件循环绑定，per-cid 保序） |
| settings（interval_min/update_mode/public_url） | :433,581,599 | 注入 |
| **downlink publish**（cli.publish） | :563-564,633-634 | **抽象 Downlink 接口**：MqttDownlink（paho）/ SseDownlink（连接表/DB queued） |
| 升级三连/_maybe_push_upgrade/dispatch_upgrade | :413-414,585-680 | 业务策略，决定归属（建议留 FrameProcessor，downlink 抽象后传输无关） |
| paho 线程→loop 胶水/_on_message/_dispatch | :222-231,288-309 | **纯 transport 胶水，留 bridge** |
| connected（threading.Event） | :84,212 | 留 bridge（REST stats 读） |

**两个最大纠缠点**：`_on_status` 尾部升级三连（finish_updates_reaching/flush/auto-push）、`_on_result` 的 up- 分流 + per-cid 锁。
抽取后 FrameProcessor 持 store/stats/audit/locks/settings/downlink，MqttBridge 与 HTTP ingest 各自做传输胶水 + 闸门
（IP 白名单/归属/尺寸）后调同一 FrameProcessor。**proto 闸门留 on_status 内**（与现状一致）。

### 5.4 Agent 传输层

- 新增 `KK_TRANSPORT`（默认 `mqtt` 灰度，可选 `httpsse`）；config.py 增键，main.py 按值装配 transport。
- httpsse 传输模块复用 collector/executor/updater/分块/去重（main.py 逻辑不变，只换 transport 绑定）。
- 上行 httpx POST + 有界离线队列（128 帧/8MB + truncated 终态）；下行 httpx stream 读 SSE + 自写重连循环（带 Last-Event-ID）+ 指数退避 Full 抖动。
- `announce_update` 改 HTTP POST status（reason=updating）**等 200**（替代等 PUBACK，transport.py:316-337 语义不变）。
- 自更新 updater.py 已是纯标准库 HTTP（:29-30），**零改动**（本就独立于 MQTT）。

---

## 6. 分阶段迁移路径（双通道并存、可灰度可回滚）

### Phase 0 · 服务端双通道（MqttBridge 原样保留）
- 抽 FrameProcessor（§5.3）+ Downlink 接口；MqttBridge 改调 FrameProcessor（行为不变，回归锁全绿）。
- 新增 ingest 路由（/api/v1/ingest/{status,hb,result}，agent_ip_auth）+ SSE endpoint（/api/v1/commands/stream，sse-starlette）。
- commands 表增 queued 态 + queued_commands/mark_command_sent/flush_queued_commands + sweep queued 档 + KK_CMD_QUEUE_TTL；
   部分索引谓词裁决 + test_dialects 锁同步；_ADD_COLUMNS 无需（status 是既有列，只是新取值）。
- SSE 断连置 offline + 连接表 + Last-Event-ID 续传。
- 两通道写同一 store 混跑；新增按传输维度计数器（**修正：当前无 transport_mqtt_received，需新增 transport_tagged 计数**）。
- 顺手修 proto/messages.md 两处漂移（§7 尺寸预检、§3.3 rc=-3 落 done+truncated）。

### Phase 1 · Agent 可选传输
- KK_TRANSPORT=httpsse（默认 mqtt）；httpsse 传输模块复用 collector/executor/updater/分块/去重。
- 按 group/labels 灰度（先小批主机切 httpsse，观察）。

### Phase 2 · 验证收敛
- 按传输计数器治理（沿用 proto_v3_received 手法，mqtt 计数归零才拆）；
- E2E 五场景：断网/重连/服务端重启/大输出分块/自更新（改造 mqtt_e2e.py 或新增 httpsse_e2e.py，FakePublish 复用 conftest.py:26-38）；
- 核对 skipped 口径（**修正：4 条 broker-gated test_integration + 1 条 POSIX-only test_executor.py:209 = 5**；
   AGENTS.md「5 条 Broker 级」措辞不精确，quality_gate.sh:27-29 实为「至多 5」`sk>5`；httpsse 集成用例落单元层不新增 broker-gated）。

### Phase 3 · 拆除
- 删 MqttBridge/mosquitto 部署（compose prod/offline 的 mosquitto 服务/1883/KK_MQTT_URL）/ubuntu broker 镜像/离线 kk-broker tar/Agent paho 依赖。
- nginx 收敛 HTTP 反代 + SSE location `proxy_buffering off` + `proxy_read_timeout`>ping。
- proto/messages.md 传输绑定章节改写（主题布局→HTTP/SSE 端点）；design.md §2 更新。

### 回滚
- Phase 0-2 任意主机 `KK_TRANSPORT=mqtt` 一键回退（双通道并存）；Phase 3 前留版本窗口（mqtt 计数未归零不拆）。

---

## 7. 影响面清单

- **测试**：test_integration.py 4 条 broker-gated（:171,307,334,367，require_broker :41-57）需 httpsse 等价覆盖；
   mqtt_e2e.py 10 项 check（LWT/离线队列 :211-237）改造或新增 httpsse_e2e；FakePublish（conftest.py:26-38）复用 + 新增 FakeDownlink；
   test_dialects 部分索引谓词锁同步（:251-253）；FrameProcessor 抽取后 test_bridge 既有锁须全绿（行为不变）。
- **前端**：`broker.connected`（stats.py:27-32）→「采集通道活性」语义重映射（welcome/index.vue:91-95、system/index.vue:59-69）；
   COUNTER_META（system/index.vue:115-222）~20 计数器按传输维度重映射 + 新增 cmd_flushed/transport_tagged；cmd_failed hint 调整。
- **部署**：compose prod/offline 去 mosquitto（Phase 3）；离线 manifest 去 kk-broker core tar；1883 端口收敛；
   nginx SSE location（proxy_buffering off）；build.sh/install.sh 的 KK_SERVER 语义（mqtt://→https://）+ KK_TRANSPORT。
- **多实例**：上行无状态零改动；下行 sticky 优化 + DB 队列轮询兜底保正确性（跨实例实时推可后补 Redis pub/sub，非必需）。
- **协议文档**：proto/messages.md 传输绑定改写 + 两处漂移修正；design.md §2「为什么是 MQTT」更新为「为什么 HTTP+SSE + DB 补偿」。

---

## 8. 风险与缓解

| 风险 | 缓解 |
|---|---|
| **自管重连/半开连接**（失去 Broker keepalive 强断） | SSE ping 15s + Agent 读超时 45s 无帧即重连 + 指数退避 Full 抖动；record_hb 回置 online + mark_stale_offline 180s 兜底（已有） |
| **nginx SSE 缓冲**（默认 proxy_buffering 会攒帧） | SSE location `proxy_buffering off` + `proxy_read_timeout`>ping 间隔（sse-starlette README 原文指导）；E2E 穿 nginx 验证 |
| **多实例下行路由**（连接绑进程） | DB 队列 + 重连 flush 保正确性（无需 sticky）；sticky 为延迟优化；跨实例实时推后补 pub/sub |
| **历史包袱：v1 自研 WS pending_for 丢命令**（design.md:55） | **不重蹈覆辙的关键**：v1 缺陷是「sent 状态丢命令 + 内存连接表不可 HA」；本方案命令排队落 **DB queued 态**（持久、可 HA、重启不丢），
   flush_queued_commands 泛化已验证的 flush_queued_upgrades（先 mark 刷计时再推，防重启重投窗口），sweep + lost 兜底真丢的终态——
   补偿层是 DB 持久队列而非内存补发，与 v1 有本质区别 |
| **amqtt 过渡风险** | 仅应急桥，标过渡态；222★ 库不承担目标态 broker 职责 |
| **commands queued 态引入的索引/清扫口径变化** | 部分索引谓词与 sweep 逐字同步（test_dialects 锁）；queued TTL 双档（pending 30min/queued KK_CMD_QUEUE_TTL）沿用 updates 先例 |
| **灰度期双通道一致性** | 两通道写同一 store + 同一 FrameProcessor；按传输计数器治理，mqtt 归零才拆；任意主机一键回退 |

---

## 9. 来源引用

**业界项目**（star/版本/活跃查询日 2026-11-22）：
- Beszel（27,537★ MIT v0.21.2）：https://github.com/henrygd/beszel · https://github.com/henrygd/beszel-docs（security.md/reverse-proxy.md/heartbeat.md）
- Netdata（81,575★ GPL-3.0 v2.13.0）：https://github.com/netdata/netdata · https://learn.netdata.cloud/docs/streaming
- Glances（34,001★ LGPL-3.0 v4.5.9）：https://github.com/nicolargo/glances · https://pypi.org/project/glances/
- Uptime Kuma（94,744★ MIT 2.5.12）：https://github.com/louislam/uptime-kuma
- Checkmk（GPL-2.0 v2.5.0p20）：https://github.com/Checkmk/checkmk · https://docs.checkmk.com/latest/en/agent_linux.html
- Zabbix agent2（6,589★ AGPL-3.0 8.0.0）：https://github.com/zabbix/zabbix · https://www.zabbix.com/documentation/current/en/manual/concepts/agent2
- node_exporter（13,952★ Apache-2.0 v1.13.1）：https://github.com/prometheus/node_exporter
- Pushgateway（3,375★ v1.11.4）：https://github.com/prometheus/pushgateway · https://prometheus.io/docs/practices/pushing/
- Grafana Alloy（3,702★ v1.22.0）：https://github.com/grafana/alloy
- Vector（22,931★ MPL-2.0 v0.62.0）：https://github.com/vectordotdev/vector · https://vector.dev/docs/reference/configuration/sinks/http/
- Fluent Bit（8,204★ v5.2.0）：https://github.com/fluent/fluent-bit · https://docs.fluentbit.io/manual/pipeline/outputs/http
- Telegraf（17,917★ MIT v1.40.3）：https://github.com/influxdata/telegraf
- amqtt（222★ MIT v0.12.2）：https://github.com/Yakifo/amqtt · https://pypi.org/project/amqtt/
- Mosquitto（11,481★ EPL-2.0/BSD v2.1.4）：https://github.com/eclipse-mosquitto/mosquitto
- NATS（server 21,100★ v2.15.3）：https://github.com/nats-io/nats-server · https://github.com/nats-io/nats.py

**库/规范**：
- sse-starlette（3.7.0，860★ BSD-3，18KB，ping 默认 15s，Python≥3.11，**不做 Last-Event-ID 回放**）：https://pypi.org/project/sse-starlette/ · https://github.com/sysid/sse-starlette
- httpx（0.28.1，15,831★ BSD-3，71.8KB）：https://pypi.org/project/httpx/ · https://github.com/encode/httpx
- grpcio（1.85.3，cp312 manylinux wheel **7,188.2KB≈7.4MB C++ 扩展**）：https://pypi.org/project/grpcio/#files · https://github.com/grpc/grpc
- FastAPI WebSocket（单进程警告）：https://fastapi.tiangolo.com/advanced/websockets/
- SSE 规范：https://html.spec.whatwg.org/multipage/server-sent-events.html · https://developer.mozilla.org/en-US/docs/Web/API/Server-sent_events/Using_server-sent_events
- OTLP（v1.11.1，仅 Export 无下行）：https://opentelemetry.io/docs/specs/otlp/
- MQTT retain 语义：https://www.hivemq.com/blog/mqtt-essentials-part-8-retained-messages/

**遗留不确定项（诚实标注）**：Beszel 官方未公布 agent RAM 精确值（仅 lightweight 定性 + 4.52MB 压缩包/scratch 镜像佐证）；
Zabbix agent2「长连接」仅核实到设计目标 "Reduce the number of TCP connections"，未找到「单条持久连接承载全部主动检查」明文，
引用以「主动出站 + 连接复用倾向 + SQLite 缓冲」表述；Checkmk GitHub 为镜像仓（2.6k★），社区规模不能以该 star 衡量。

---

## 后续（本轮不做，待评审）

Phase 0 服务端双通道实施 → Phase 1 Agent 传输 → Phase 2 验证 → Phase 3 拆除 Broker。
每阶段独立可灰度可回滚。Phase 0 计划（§5.3 FrameProcessor 抽取点 + §5.1/5.2 ingest/SSE 端点签名 +
§5.2 flush_queued_commands 状态机 + commands queued 态补充）可直接作为下一轮实施依据。