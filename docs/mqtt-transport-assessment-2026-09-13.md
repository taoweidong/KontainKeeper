# MQTT 传输通讯现状审视与重构方案大纲（2026-09-13）

> 背景：项目进入「重构阶段」，方向曾定为「采用 MQTT 传输协议」。本审视核对当前通讯链路，
> 并据「方案不要过于复杂，支持快速稳定传输即可；若当前已满足要求，不要求强制重构」三原则给出结论。

## 0. 结论（先说重点）

**当前通讯链路已是端到端 MQTT，且已具备「快速 + 稳定」所需的全部可靠性机制。
按「不要求强制重构」原则，建议：不重构，维持现状（MQTT 直连）。**

仅存在 1 个运维侧风险（Broker 单点）与 3 项可选的小加固，均非强制、不改架构。

## 1. 现状：当前就是 MQTT 架构

- 链路：`agent/src/kk_agent/transport.py`（Agent 端 MQTT 客户端）→ `eclipse-mosquitto:2` →
  `server/src/kk_server/services/mqtt_bridge.py`（服务端无状态桥）。
- 部署：`docker-compose*.yml` 中 `KK_MQTT_URL: mqtt://mosquitto:1883`；Agent 经 Broker 中转，
  服务端不持有连接表，扩容只改订阅前缀。

## 2. 「快速稳定传输」需求 ↔ 现状覆盖对照

| 需求 | 现状 | 依据 |
|---|---|---|
| 快速（低延迟、轻量） | ✅ 已满足 | 心跳 QoS0（不阻塞）、命令/结果 QoS1；JSON 小帧；**8–12MB 二进制走 HTTP 下载端点**，不占 MQTT |
| 稳定（命令不丢） | ✅ 已满足 | `clean_session=False` 持久会话 + QoS1，Broker 离线排队，重连自动补投 |
| 断线可感知 | ✅ 已满足 | LWT 遗嘱 + retained status，异常掉线由 Broker 代发 `offline` |
| 重连自愈 | ✅ 已满足 | paho 指数退避重连 `reconnect_delay_set(1, 60)` |
| 安全接入 | ✅ 已满足 | `mqtts://` TLS；上行帧自报 IP 经 `KK_AGENT_IPS` 白名单校验 |
| 离线命令不丢 | ✅ 已满足 | 离线主机台账标 `queued`，上线 `flush_queued_upgrades` 补投 |
| 水平扩容 | ⚠️ 已留口 | Server 多实例只需 `_sub_topics` 加 `$share/{group}/` 前缀 + 各实例唯一 `client_id`（代码注释已说明） |

## 3. 唯一实质风险：Broker 单点（SPOF）

- 当前为单个 Mosquitto 容器。Broker 宕机 = 控制面全断。
- 但设计对**瞬时抖动**已 resilient：Broker 恢复后，Agent 重连 + 持久会话会把离线期命令自动补投，不丢数据。
- **处置（运维侧，非代码重构）**：生产环境用 2 副本 + keepalived/负载，或托管 MQTT。
  单 Mosquitto 性能可轻松扛 500 台（万级连接无压力），瓶颈在 SPOF 而非吞吐。

## 4. 可选加固清单（均为小改动，非强制，认可后才做）

1. **（可选·极小）重连抖动**：`transport.py` 的 `reconnect_delay_set(1, 60)` 加随机 jitter，
   避免 500 台在 Broker 抖动后同一时刻重连形成惊群。约 3 行。
2. **（可选·运维）Broker HA**：如 §3，部署层解决，不改代码。
3. **（可选·仅扩容时）共享订阅**：Server 要水平扩容时，给 `_sub_topics` 加 `$share/{group}/` 前缀即可。

## 5. 不做什么（避免过度设计）

- ❌ 不重写为 WebSocket / gRPC / 自研长连接：MQTT 已接管连接可靠性，重写是倒退。
- ❌ 不引入 Broker 集群（EMQX 集群、Mosquitto bridge 网状）：单 Mosquitto + HA 副本足够 500 台，
  集群徒增运维复杂度，违背「不要过于复杂」。
- ❌ 不为「未来可能的」需求预留抽象层：当前直连已最简。

## 6. 建议

维持现状（MQTT 直连），精力放在**运维侧 Broker HA** 与**必要监控**（心跳计数、 `cmd_failed` 告警）上。
代码侧仅在上述「可选加固」被认可时做对应小改动；否则本次不排任何重构任务。
