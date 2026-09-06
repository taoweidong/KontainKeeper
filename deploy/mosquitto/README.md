# Mosquitto 部署说明

KontainKeeper 把「连接可靠性 / 离线排队 / 在线判定」整体交给 Mosquitto 2.x。

## 单一配置

`mosquitto.conf` 同时用于开发与生产（`docker-compose.yml` / `docker-compose.prod.yml`
均挂载它）：**匿名开放、无 ACL**——Agent 零凭据接入，只需知道 Broker 地址。

## v3 安全模型（内网可信 + 服务端白名单）

- **Broker 匿名开放**：Agent 不再维护任何凭据（无 password_file / aclfile /
  逐主机账号），部署复杂度降到最低。
- **接入管控上收到 kk-server**：Agent 上行帧（status / hb / result）统一携带
  自报 `ip`，由服务端 `KK_AGENT_IPS` 白名单校验（IP / CIDR 混合列表），
  白名单外的上报全部拒绝并审计（`ip_rejected`）。
- **REST 侧自更新接口**（`/api/system/agent/latest|download`）同样按请求源 IP
  校验该白名单。
- **status retain 持久化**：`persistence true` + `mosquitto-data` 卷，Broker
  重启后在线视图无需等一轮心跳/LWT。

## 边界说明（必读）

- MQTT 经 Broker 中转拿不到发布者真实 TCP 源 IP，白名单基于 Agent 自报值
  （`KK_ADVERTISE_IP` 显式覆盖 > UDP connect 自动探测出口地址）——适合内网
  可信环境，不适合不可信网络。
- 匿名 Broker 下**网络层隔离是唯一不可伪造的边界**：请用防火墙 / 安全组
  限制 1883 端口的可达范围，只放行服务端与被管理主机所在的网段。
- 主题前缀默认 `kk/v1`；若改了 `KK_TOPIC_PREFIX`，只需双端一致（无 ACL 需要同步）。
