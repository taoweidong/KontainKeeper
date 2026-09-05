# Mosquitto 部署说明

KontainKeeper 把「连接可靠性 / 离线排队 / 在线判定 / 鉴权」整体交给 Mosquitto 2.x。

## 两种配置

| 文件 | 用途 | 匿名 | ACL |
|---|---|---|---|
| `mosquitto.dev.conf` | 本地开发 / CI 冒烟 | 开 | 无 |
| `mosquitto.conf` | 生产 | 关 | `aclfile`（按主机名） |

`docker-compose.yml` 默认挂 `mosquitto.dev.conf`（开箱即起）；`docker-compose.prod.yml` 挂 `mosquitto.conf`。

## 安全模型

- **用户名 = 主机名**：服务端用固定账号 `kk-server`；每台 Agent 用各自的主机名作用户名、token 作密码。
- **ACL**：`aclfile` 里 `user kk-server` 拥有 `kk/v1/#` 全读写；`pattern readwrite kk/v1/%u/#` 让每台 Agent 只能读写自身主题子树（`%u` 由 Mosquitto 按认证用户名展开）。
- **status retain 持久化**：`persistence true` + `mosquitto-data` 卷，Broker 重启后在线视图无需等一轮心跳/LWT。

## 生成 Agent 账号

```bash
# 单台
bash deploy/mosquitto/gen-credentials.sh web-01 <token>
# 或直接在容器内（保证哈希算法与 Broker 一致）
docker compose run mosquitto mosquitto_passwd -b deploy/mosquitto/passwordfile web-01 <token>
```

## 风险与未决项

- **`pattern %u` 的展开语义与优先级**需在真实 Mosquitto 2.x 实测确认（见方案 §13）。
  退路：用 `gen-credentials.sh` 脚本按主机清单生成显式 `user` 段。
- Windows 开发机无 docker，无法本地验证 ACL；CI 用 `eclipse-mosquitto:2` service 容器保证。
