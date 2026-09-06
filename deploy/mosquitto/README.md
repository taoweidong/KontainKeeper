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

> **passwordfile 不入库**（已在 `.gitignore` 中，仓库公开，见代码审查 P0-2）。
> 首次使用请从示例复制或直接生成：
> `cp deploy/mosquitto/passwordfile.example deploy/mosquitto/passwordfile`

```bash
# 单台
bash deploy/mosquitto/gen-credentials.sh web-01 <token>
# 或直接在容器内（保证哈希算法与 Broker 一致）
docker compose run --rm mosquitto mosquitto_passwd -b deploy/mosquitto/passwordfile web-01 <token>
```

## Agent 接入形态（生产必读）

生产 Broker 关闭匿名且按用户名做 ACL，因此 Agent **必须携带凭据**，且用户名要等于主机名
（否则 `pattern readwrite kk/v1/%u/#` 展开后不是自己的子树）。两种等价写法：

```bash
# 写法 A：凭据写在 Broker 地址里（用户名=主机名，密码=该机 token）
KK_SERVER="mqtt://web-01:<token>@broker.ops:1883"

# 写法 B：地址不带凭据，显式指定（缺省时自动取 KK_HOST_NAME / KK_TOKEN）
KK_SERVER="mqtt://broker.ops:1883" KK_MQTT_USERNAME=web-01 KK_MQTT_PASSWORD=<token>
```

> 只写 `KK_SERVER=mqtt://broker:1883` + `KK_TOKEN=<token>` **不会被用于 MQTT 鉴权**，
> 在禁匿名 Broker 上会直接被拒连——这是最容易踩的坑（代码审查 P1-2）。

## 风险与未决项

- **`pattern %u` 的展开语义与优先级**需在真实 Mosquitto 2.x 实测确认（见方案 §13）。
  退路：用 `gen-credentials.sh` 脚本按主机清单生成显式 `user` 段。
- Windows 开发机无 docker，无法本地验证 ACL；CI 用 `eclipse-mosquitto:2` service 容器保证。
- `aclfile` 里的主题前缀目前**写死 `kk/v1`**：若通过 `KK_TOPIC_PREFIX` 改了前缀，
  必须同步改 `aclfile`，否则 ACL 全部失配（代码审查 P2）。
