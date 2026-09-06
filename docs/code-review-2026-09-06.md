# 全量代码审查报告（2026-09-06）

审查范围：`server/src/kk_server/`（服务端）、`agent/src/kk_agent/`（Agent）、`web/src/`（Vue3 前端）、
`docker-compose*.yml` / `server/Dockerfile` / `deploy/` / `scripts/` / `agent/build/`（部署与构建）、
`proto/messages.md` 与 `README.md` / `docs/` / `AGENTS.md`（协议与文档一致性），以及两侧测试套件的覆盖缺口。

方法：五个专项并行审查 + 主审对**全部 P0/P1 结论逐条回到代码复核**（含修正子代理的误判，见文末「复核说明」）。

## 总体评价

架构与工程质量**高于平均水平**：分层清晰（models→services→controllers）、异步 SQLAlchemy 用法正确、
三库方言有编译期兜底测试、审计覆盖关键动作、Agent 资源占用控制扎实（读取侧输出封顶、线程有界、
daemon 化、单调钟抖动、进程组回收、插件超时隔离）、协议双端契约一致（`PROTO_VER=2`、`kk/v1` 前缀、
8 项采集白名单、QoS/retain 语义、LWT）。

主要短板集中在三处：**安全兜底在 shell 形态下失效**、**凭据与传输的准备度不足以上线**、
**前端会话失效后无兜底**。以下按严重度列出。

---

## P0 — 必须修（安全红线 / 上线阻断）

### P0-1 命令黑名单在 shell 形态下结构性失效（安全红线）
`server/src/kk_server/services/security.py:17` ↔ `controllers/commands.py:79-81,105` ↔ `agent/.../executor.py:159-163`

shell 模式下 `argv[0]` 就是**整条命令串**（executor 亦如此约定：取 `argv[0]` 交给 shell）。
此时 `prog = os.path.basename(argv[0])` 拿到的不是程序名，导致
「程序名 + 高危参数组合」与「高危程序集合」两层校验**整体失效**，只剩子串匹配，且子串可被轻易规避：

| 提交形态 | 命令 | 结果 |
|---|---|---|
| argv 数组 | `["rm","-rf","home"]` | ✅ 拦截 |
| shell 形态 | `{"argv":["rm -rf home"],"use_shell":true}` | ❌ **放行**（相对路径，子串 `rm -rf /` 不匹配） |
| shell 形态 | `{"argv":["dd if=/dev/urandom of=/dev/sda"],"use_shell":true}` | ❌ **放行**（默认串只写了 `dd if=/dev/zero`） |
| shell 形态 | `{"argv":["chmod 777 -R /etc"],"use_shell":true}` | ❌ **放行**（参数换序即不匹配 `chmod -R 777 /`） |

AGENTS.md 明示「命令黑名单（`KK_CMD_BLACKLIST`）+ 审计不能绕过」，此项击穿该红线。
另经 `cmdline` 路径（`commands.py:72` 走 `shlex.split`）**不受影响**——仅 `argv`+`use_shell` 组合可绕过。

**建议**：`is_blacklisted` 增加「单串形态」分支——对 `use_shell` 的整串先按 `;|&`+空白切分为 token 后再走
`dangerous_combos`/高危程序集；同时把高危组合改为「归一化后按 token 集合匹配」而非有序子串。

### P0-2 公开仓库提交可用默认凭据，prod compose 明文写死
`docker-compose.prod.yml:32-33,38`（`KK_MQTT_PASSWORD: dev-token`、`KK_AGENT_TOKENS: dev-token`、
`KK_ADMIN_PASS: change-me-in-prod`）、`deploy/mosquitto/passwordfile`（已入库，预置 `kk-server` 账号，
口令=公开值 `dev-token`；PBKDF2-SHA512/310000 迭代强度本身合格）。

仓库为公开仓库，任何人按 prod compose 原样部署即得到**公开已知的 Broker 与管理员口令**。
（compose 头部注释已提示改走 `.env`/secret，属「已知弱默认 + 未强制」，但上线前必须处理。）

**建议**：`passwordfile` 移出版本库（改 `.gitignore` + 部署时由 `gen-credentials.sh` 生成）；
compose 全部凭据改 `${VAR}` 引用 `.env`；加启动自检——检测到默认口令仍在用即拒绝启动或强告警。

---

## P1 — 上线前必须处理 / 功能必现异常

| # | 位置 | 问题 | 影响 | 建议 |
|---|---|---|---|---|
| 1 | `web/src/utils/http/index.ts:80-97,132` ↔ 后端无 `/refresh-token`（仅 `/login` `/logout`） | token 过期后走 refresh 分支，但刷新接口不存在 → `retryOriginalRequest` 的回调永不触发，**Promise 永久 pending**；响应拦截器对 401/403 完全不处理 | 12h 会话过期后所有轮询挂起、界面卡死且不跳登录 | 响应拦截器统一拦截 401/403 → 清 token + 跳 `/login`；删除依赖不存在接口的 refresh 分支 |
| 2 | `agent/.../transport.py:108` ↔ `config.py` | Agent 仅当 `KK_SERVER` 形如 `mqtt://user:pass@host` 才调 `username_pw_set`，**`KK_TOKEN` 未接入 MQTT 鉴权** | README 的生产接入形态 `KK_SERVER=mqtt://broker:1883` + `KK_TOKEN=...` 在禁匿名 Broker 上**必被拒连**，且无法满足 ACL `pattern kk/v1/%u/#`（`%u` = 认证用户名 = 主机名） | 生产接入改为 `mqtt://<主机名>:<token>@<broker>:1883`（README/deploy 文档同步），或代码层用 `KK_TOKEN`+`KK_HOST_NAME` 兜底填 user/pass |
| 3 | `docker-compose.prod.yml:16`、`transport.py:112-113` | 1883 明文暴露且无 TLS；`tls_insecure_set(True)` 会连带把 `verify_mode` 置 `CERT_NONE`，使已配的 `KK_TLS_CA` 失效 | 命令/令牌/采集数据可被中间人劫持 | 启用 `mqtts://` + `KK_TLS_CA`；`tls_insecure` 只关 `check_hostname`、保留 CA 校验；或拒绝「同时设 CA 与 insecure」 |
| 4 | `server/.../models/store.py:166-190` | `record_hb` 只更新 `last_seen`，不刷新 `status_ts`；而在线判定看 `status_ts` | 长驻主机在数个周期后被**误判离线**，列表抖动 | `record_hb` 同步 bump `status_ts`，或判定改用 `max(status_ts, last_seen)` |
| 5 | `server/.../controllers/auth.py:15-23` | 登录失败无计数/限流/锁定 | 管理员口令可被在线爆破 | 加失败计数 + 临时锁定或速率限制 |
| 6 | `server/.../services/mqtt_bridge.py:202-216` | result 帧 QoS1「至少一次」，Broker 重投时同 `seq` 块被**重复拼接** | 命令输出被污染/翻倍 | 按 `(id, seq)` 幂等去重 |
| 7 | `agent/deploy/entrypoint-wrapper.sh:56` | `exec "$@"` 后无信号转发 | 容器停止时 Agent 收不到 SIGTERM → 被 SIGKILL，优雅下线帧丢失、`_MEI` 残留 | exec 前 `trap` 把 SIGTERM/SIGINT 转发给 Agent 子进程 |
| 8 | `proto/messages.md:93` ↔ `agent/.../collector.py:176-186` | 文档 `users` 示例为 `{name,uid,procs,vscode}`，实现返回 `{name,terminal,host,started}` | 按文档解析全部落空 | 修订文档示例（或补齐 uid/procs/vscode 实现） |
| 9 | `README.md:227` ↔ `agent/.../updater.py:75-81` | `KK_UPDATE_URL` 写「自动推导」，实际无推导、未配即跳过 | 用户以为不配也能自更新，实际永不生效 | 文档改为「必填，未配则跳过自更新」 |
| 10 | `AGENTS.md:47,51` | 仍描述「结果经 `queue.Queue` 回主线程」「采集经 `fs_root`（KK_FS_ROOT）注入伪造 /proc」「`resource` 模块 try/except」 | 与现状（psutil + 工作线程直接 publish）**相反**，且 AGENTS.md 每次会话注入，会主动误导后续开发 | 删除过期句，改为「paho publish 线程安全，工作线程直接发帧」「采集基于 psutil」 |

---

## P2 — 次要（建议排期修复）

- `server/.../controllers/commands.py`：`sudo dd ...` / `env rm ...` 等包装命令逃过高危程序集（`prog` 只取首 token）；`verify_admin` 对不存在的用户直接返回（非恒定时间，可枚举用户名）。
- `server/.../models/store.py:203-214`：`list_containers` full 视图无分页上限。
- `server/.../controllers/agent_update.py:103-115`：download 生成器未用 `with` 包住文件句柄。
- `server/.../services/mqtt_bridge.py:161-163`：帧处理异常仅 log，静默丢帧且不计 `rejected`/审计；`:172-188` 未校验 `topic.host == body.host`（纵深缺失，全依赖 Broker ACL）。
- `server/.../controllers/auth.py:26-32`：logout 未写审计。
- `server/.../models/helpers.py:8`：pbkdf2 迭代 120k 偏低（建议 300k+）。
- `agent/.../main.py:119-121`：`kind=update` 推送更新无回执，失败时服务端命令永久停在 running。
- `agent/.../executor.py:197`：shell 模式丢弃 `argv[1:]`（应文档化或加校验）。
- `deploy/mosquitto/aclfile:15`：前缀死写 `kk/v1`，与可配置 `KK_TOPIC_PREFIX` 脱节（改前缀即隔离崩塌）。
- `web/src/api/user.ts:47-59`：后端 `/api/login` 只回 `{token,username}`，前端**自造** `expires=+12h`、`roles=["admin"]`、`permissions=["*:*:*"]` → RBAC 形同虚设。
- `web/src/store/modules/user.ts:80-88`：登出只清本地，未调 `/api/logout`，服务端令牌仍有效。
- 各业务页轮询无 inflight 守卫（后端慢于轮询间隔时请求堆叠）。
- `server/Dockerfile`：未 `USER`（root 运行）；`uv:latest` 未固定版本；compose 无 healthcheck。
- `README.md:220,222`：`KK_DISK_PATHS` 实际默认空（自动发现）、`KK_PLUGIN_DIR` 默认包内目录，文档默认值过时。
- `proto/messages.md §3.3`：rc 表缺 `-2`（未取到退出码）与 `125`（执行器异常）；`§4.1` collect 帧示例带 `argv/use_shell` 但实现不接受。
- `web/src/views/host/detail/index.vue:167-168`：读 `metrics.disk_read_mb/disk_write_mb/cpu_cores/kernel`，与心跳帧实际字段（`disk_io.*`/`load`/`sys`）疑似不符，可能长期显示 `-`。
- `web/src/views/login/index.vue:39-42`：登录表单硬编码 `admin/admin123` 初值。

## P3 — 建议

- `agent/.../plugin_loader.py:16`：`_loaded` 不淘汰已删除插件（规模极小，可定期清理）。
- `agent/.../collector.py:153`：`proc_metrics` 固定 `sleep(0.3)` 也作用于 `kind=collect` 命令路径（可传 `sample_sec=0`）。
- `agent/.../main.py`：`metrics["ts"]` 与帧顶层 `ts` 重复。
- `agent/.../transport.py`：`stop()` 先发 offline 再 `loop_stop`，存在未 flush 竞争（LWT 已兜底）。
- `web`：审计/命令表格全量渲染无分页或虚拟滚动；三处裸 `setInterval`（已正确清理，建议抽 `usePoll`）。
- `.dockerignore` 补 `*.pem *.key .env`。
- `docs/architecture-review.md:79,84`：WebSocket 选型与 `fs_root` 描述需标注为历史（现已被 MQTT / psutil 取代）。
- 文档中的测试数量（159 passed）与当前（agent 99 + server 66）需同步。

## 测试覆盖缺口

- **黑名单**：仅覆盖 argv/少量 cmdline，**缺 `use_shell + argv[0]` 整串绕过用例**（对应 P0-1）。
- **三库**：只有 `test_dialects.py` 编译期校验，**PG/MySQL 未连真库实跑**，`append_result` 拼接语义未验证。
- **MQTT**：无 result 帧重复投递去重测试；无 `topic.host != body.host` 越权测试。
- **Agent**：缺畸形端口 `ValueError`、TLS insecure 旁路、`_on_message` 坏 JSON/超大帧、`kind=update` 全链路回执、Broker 不可达下 `run()` 韧性、Windows `kill_tree` 路径。
- **服务端**：缺登录爆破、长驻主机误判离线（P1-4）、download 中断资源泄漏测试。
- **前端**：无过期/401 场景测试。

## 复核说明（主审对子代理结论的验证与修正）

- ✅ **P0-1 已复核**：确认 `commands.py:72` 的 `cmdline` 走 `shlex.split`（该路径**不受影响**）；
  绕过仅存在于 `argv`+`use_shell` 组合。子代理举例的 `rm -rf /home` 实际会被默认子串 `rm -rf /` 拦下——
  **真正的绕过是相对路径 / 参数换序 / `dd if=/dev/urandom`**，本表已修正。
- ✅ **P1-1 已复核**：grep 后端控制器确认**无 `/refresh-token` 端点**（仅 `/login`、`/logout`），
  前端 refresh 分支确为死路；响应拦截器确无 401 处理。
- ✅ **P0-2 已复核**：`git ls-files` 确认 `passwordfile` 已入库，prod compose 明文凭据属实。
- ⚠️ 子代理另报「Agent 未校验 topic-host == body.host」为 P2，同意——该防线当前完全依赖 Broker pattern ACL，
  ACL 误配即失去主机隔离，属纵深缺失。

## 建议修复顺序

1. **P0-1（黑名单 shell 形态）+ 补绕过测试** —— 安全红线，改动集中在 `is_blacklisted` 一个函数。
2. **P0-2（凭据移出仓库 / 改 .env 注入）** —— 上线阻断。
3. **P1-1（前端 401 兜底）** —— 12h 必现的功能阻断，改动集中在响应拦截器。
4. **P1-2 / P1-3（Agent 接入形态与 TLS）** —— 决定生产能否真跑通，需代码与部署文档同步。
5. 其余 P1 按序，P2 排期。
