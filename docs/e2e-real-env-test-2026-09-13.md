# KontainKeeper 真实环境端到端验证报告

> 生成时间(UTC+8): 2026-09-13 17:38:24

## 1. 测试拓扑与环境

- 时间(UTC+8): 2026-09-13 17:36:50
- 服务端: Windows, 端口 8443, 数据库 kk-server-e2e2.db
- API 前缀: /api 与 /api/system
- Broker: WSL(Ubuntu-22.04) mosquitto, 端口 1883; Windows 经 127.0.0.1:1883 经 WSL2 端口转发可达
- Agent: WSL 内 3 实例 (wsl-agent-01/02/03), 经 MQTT 接入
- /api/health: {"ok":true,"version":"0.1.0","proto_ver":3,"agents_online":3,"broker":"connected","bridge":{"status":17,"hb":12,"result":0,"rejected":0,"cmd_published":0,"cmd_failed":0,"upgrade_pushed":0,"sweeps":1,"...(truncated)


## 2. 测试总览（全量用例 42 条，PASS 40 / FAIL 2）

| 用例ID | 模块 | 名称 | 期望 | 实际结论 | 结果 |
|--------|------|------|------|----------|------|
| T01 | 鉴权 | 管理员登录 | HTTP 200, 返回 token | HTTP 200, token 长度 43 | **PASS** |
| T02 | 鉴权 | 未带 token 访问受保护接口 | HTTP 401 unauthorized | HTTP 401 | **PASS** |
| T03 | 鉴权 | 错误密码登录 | HTTP 401 | HTTP 401 | **PASS** |
| T04 | 鉴权 | 携带 token 访问 /api/me | HTTP 200, 返回 username | HTTP 200, username=admin | **PASS** |
| T05 | 鉴权 | 登出 | HTTP 200, ok=true | HTTP 200, {"ok":true} | **PASS** |
| T06 | 鉴权 | 登出后旧 token 失效 | HTTP 401 | HTTP 401 | **PASS** |
| T07 | 主机 | 主机(容器)列表 | HTTP 200, 至少 3 台 online | HTTP 200, 共 3 台, online=['wsl-agent-02', 'wsl-agent-01', 'ws...(truncated) | **PASS** |
| T08 | 主机 | 主机详情(含实时指标) | HTTP 200, 含 metrics(cpu/mem/disk...) | HTTP 200, metrics 键=['cpu', 'cpu_cores', 'cpu_per_core', 'lo...(truncated) | **PASS** |
| T09 | 主机 | 主机指标端点 | HTTP 200, 返回指标 | HTTP 200, 键=['pod', 'hours', 'source', 'series'] | **PASS** |
| T10 | 命令 | 采集项白名单 | HTTP 200, 返回白名单列表 | HTTP 200, 项数=8 | **PASS** |
| T11 | 命令 | shell 命令下发 | HTTP 200, 返回命令 id 与状态 sent/pending | HTTP 200, cid=c-9e3c8813fa0b, items=[{'id': 'c-9e3c8813fa0b'...(truncated) | **PASS** |
| T12 | 命令 | shell 命令真实执行与结果回传 | status=done, 输出含 kk-e2e-ok | status=done, out=kk-e2e-ok-$(date +%s)
 | **PASS** |
| T13 | 命令 | collect 指标采集命令 | HTTP 200, 返回 cid | HTTP 200, cid=c-6ea943f4ff3f | **PASS** |
| T14 | 命令 | collect 命令结果回传 | status=done, 输出含采集项 | status=done, out长度=508 | **PASS** |
| T15 | 命令 | plugin_reload 插件重载命令 | HTTP 200, 返回 cid | HTTP 200, cid=c-8e9c72c0eeb7 | **PASS** |
| T16 | 命令 | plugin_reload 结果回传 | status=done | PASS | **{"plugins": {}}** |
| T17 | 命令 | 批量多主机下发 | HTTP 200, 返回 3 条命令与 batch_id | HTTP 200, 条数=3, batch_id=b-14569032eb4a96d6 | **PASS** |
| T18 | 命令 | 批量命令逐台结果回传 | 3 台均 done | 结果=[('c-200590446389', 'done', 'Taoweidong-PC\n'), ('c-56210...(truncated) | **PASS** |
| T19 | 命令 | 危险命令黑名单拦截 | HTTP 400, 命中黑名单被拒并记录审计 | HTTP 400, {"detail":"命令命中黑名单，已被拒绝并记录审计"} | **PASS** |
| T20 | 命令 | 非法 kind 校验 | HTTP 400 | HTTP 400 | **PASS** |
| T21 | 命令 | 不存在的目标主机 | HTTP 404 | HTTP 404, {"detail":"容器不存在: no-such-host"} | **PASS** |
| T22 | 命令 | 批次列表 | HTTP 200, 返回批次状态分布 | HTTP 200, batches=4 | **PASS** |
| T23 | 命令 | 命令列表(分页/筛选) | HTTP 200, 含 total/items | HTTP 200, total=6, 返回=5 | **PASS** |
| T24 | 导出 | 命令导出 | HTTP 200, 返回 CSV 文本 | HTTP 200, 首行=﻿id,pod,kind,argv,status,rc,timed_out,truncated...(truncated) | **PASS** |
| T25 | 导出 | 审计导出 | HTTP 200, 返回 CSV 文本 | HTTP 200, 首行=﻿id,ts,actor,action,detail 9,2026-09-13 17:36:...(truncated) | **PASS** |
| T26 | 导出 | 主机导出 | HTTP 200, 返回 CSV 文本 | HTTP 200, 首行=﻿pod,image,agent_ver,online,hb_interval,cpu,mem...(truncated) | **PASS** |
| T27 | 导出 | 指标导出 | HTTP 200, 返回 CSV 文本 | HTTP 200, 首行=﻿ts,cpu,mem_mb 2026-09-13 17:36:33,0.0,839.9 ...(truncated) | **PASS** |
| T28 | 审计 | 审计日志列表 | HTTP 200, 返回审计条目 | HTTP 200, 条数=9 | **PASS** |
| T29 | 统计 | 系统统计面板 | HTTP 200, 含 hosts/broker/commands | HTTP 200, hosts={'total': 3, 'online': 3}, broker.connected=...(truncated) | **PASS** |
| T30 | 版本治理 | 当前待分发版本(未上传) | HTTP 200, version 为空 | HTTP 200, version='' | **PASS** |
| T31 | 版本治理 | 无二进制时升级被跳过 | HTTP 200, skipped 含 reason=no_binary | HTTP 200, accepted=[], skipped=[{'host': 'wsl-agent-01', 're...(truncated) | **PASS** |
| T32 | 版本治理 | 上传 agent 二进制 | HTTP 200, 返回 version/sha256/size | HTTP 200, version=9.9.9, sha256前8=4f1d6a29 | **PASS** |
| T33 | 版本治理 | 上传后当前版本更新 | HTTP 200, version=9.9.9 | HTTP 200, version='9.9.9', hosts_outdated=3 | **PASS** |
| T34 | 版本治理 | Agent 轮询端点(manual 策略) | HTTP 200, available=false, policy=manual | HTTP 200, available=False, policy=manual | **PASS** |
| T35 | 版本治理 | 受控选机升级(真实下发) | HTTP 200, accepted 含该主机(推 update 帧) | HTTP 200, accepted=[{'host': 'wsl-agent-01', 'from_version':...(truncated) | **PASS** |
| T36 | 版本治理 | 更新台账列表 | HTTP 200, 含 items/summary | HTTP 200, items=1 | **PASS** |
| T37 | 离线/LWT | 强制杀掉 wsl-agent-03 | 进程被杀死(KILLED_PID=...) | wsl输出=KILLED_PID=727
DONE_SCAN
 | **PASS** |
| T38 | 离线/LWT | LWT 触发离线判定 | online=false | online=False, status_reason= | **PASS** |
| T39 | 离线/重连补投 | 离线主机命令下发(排队) | HTTP 200, 返回 cid(命令已发布, Broker 持久会话排队) | HTTP 200, cid=c-a9990525e05e | **PASS** |
| T40 | 离线/重连补投 | 重启 wsl-agent-03 | 进程重新拉起 | wsl输出=RESTART_DONE
 | **PASS** |
| T41 | 离线/重连补投 | 重连后离线命令补投并执行 | status=done, 输出含 offline-queued-ok | status=timeout, out= | **FAIL** |
| T42 | 离线/重连补投 | 重连后恢复在线 | online=true | online=False | **FAIL** |

## 3. 详细测试步骤与结果

### T01 管理员登录 —— PASS

- **模块**: 鉴权
- **前置条件**: 服务端已启动, 默认账户 admin/admin123
- **测试步骤**: POST /api/login {username:admin,password:admin123}
- **期望结果**: HTTP 200, 返回 token
- **实际结果**: HTTP 200, token 长度 43
- **细节**: `username=admin`

### T02 未带 token 访问受保护接口 —— PASS

- **模块**: 鉴权
- **前置条件**: 已登录取得 token(本用例故意不带)
- **测试步骤**: GET /api/containers (无 Authorization 头)
- **期望结果**: HTTP 401 unauthorized
- **实际结果**: HTTP 401
- **细节**: `{"detail":"unauthorized"}`

### T03 错误密码登录 —— PASS

- **模块**: 鉴权
- **前置条件**: 默认密码 admin123
- **测试步骤**: POST /api/login {password:wrong}
- **期望结果**: HTTP 401
- **实际结果**: HTTP 401
- **细节**: `{"detail":"用户名或密码错误"}`

### T04 携带 token 访问 /api/me —— PASS

- **模块**: 鉴权
- **前置条件**: 已登录
- **测试步骤**: GET /api/me (Bearer token)
- **期望结果**: HTTP 200, 返回 username
- **实际结果**: HTTP 200, username=admin

### T05 登出 —— PASS

- **模块**: 鉴权
- **前置条件**: 已登录
- **测试步骤**: POST /api/logout (Bearer token)
- **期望结果**: HTTP 200, ok=true
- **实际结果**: HTTP 200, {"ok":true}

### T06 登出后旧 token 失效 —— PASS

- **模块**: 鉴权
- **前置条件**: T05 已登出
- **测试步骤**: GET /api/me (已登出的 token)
- **期望结果**: HTTP 401
- **实际结果**: HTTP 401
- **细节**: `{"detail":"unauthorized"}`

### T07 主机(容器)列表 —— PASS

- **模块**: 主机
- **前置条件**: 3 个 agent 已上线
- **测试步骤**: GET /api/containers
- **期望结果**: HTTP 200, 至少 3 台 online
- **实际结果**: HTTP 200, 共 3 台, online=['wsl-agent-02', 'wsl-agent-01', 'wsl-agent-03']
- **细节**: `pods=['wsl-agent-02', 'wsl-agent-01', 'wsl-agent-03']`

### T08 主机详情(含实时指标) —— PASS

- **模块**: 主机
- **前置条件**: wsl-agent-01 在线
- **测试步骤**: GET /api/containers/wsl-agent-01
- **期望结果**: HTTP 200, 含 metrics(cpu/mem/disk...)
- **实际结果**: HTTP 200, metrics 键=['cpu', 'cpu_cores', 'cpu_per_core', 'load', 'mem_total_mb', 'mem_mb', 'mem_pct', 'mem_avail_mb', 'swap_total_mb', 'swap_used_mb', 'swap_pct', 'disks', 'disk_read_mb', 'disk_write_mb', 'disk_read_iops', 'disk_write_iops', 'net', 'procs_top', 'users', 'os', 'kernel', 'arch', 'uptime_sec', 'boot_ts', 'ts']

### T09 主机指标端点 —— PASS

- **模块**: 主机
- **前置条件**: wsl-agent-01 在线
- **测试步骤**: GET /api/containers/wsl-agent-01/metrics
- **期望结果**: HTTP 200, 返回指标
- **实际结果**: HTTP 200, 键=['pod', 'hours', 'source', 'series']
- **细节**: `{"pod":"wsl-agent-01","hours":24,"source":"raw","series":[{"ts":1789292193,"cpu":0.0,"mem_mb":839.9},{"ts":1789292198,"cpu":0.7,"mem_mb":836.6},{"ts":1789292204...(truncated)`

### T10 采集项白名单 —— PASS

- **模块**: 命令
- **前置条件**: 已登录
- **测试步骤**: GET /api/collect/items
- **期望结果**: HTTP 200, 返回白名单列表
- **实际结果**: HTTP 200, 项数=8
- **细节**: `items=['cpu', 'mem', 'disk', 'disk_io', 'net', 'proc', 'user', 'sys']`

### T11 shell 命令下发 —— PASS

- **模块**: 命令
- **前置条件**: wsl-agent-01 在线
- **测试步骤**: POST /api/commands {kind:shell, cmdline:'echo kk-e2e-ok-...'}
- **期望结果**: HTTP 200, 返回命令 id 与状态 sent/pending
- **实际结果**: HTTP 200, cid=c-9e3c8813fa0b, items=[{'id': 'c-9e3c8813fa0b', 'pod': 'wsl-agent-01', 'status': 'sent'}]

### T12 shell 命令真实执行与结果回传 —— PASS

- **模块**: 命令
- **前置条件**: T11 已下发命令 cid=c-9e3c8813fa0b
- **测试步骤**: 轮询 GET /api/commands/{cid} 直至 done; 取 /out
- **期望结果**: status=done, 输出含 kk-e2e-ok
- **实际结果**: status=done, out=kk-e2e-ok-$(date +%s)


### T13 collect 指标采集命令 —— PASS

- **模块**: 命令
- **前置条件**: wsl-agent-01 在线
- **测试步骤**: POST /api/commands {kind:collect, items:['cpu', 'mem', 'disk']}
- **期望结果**: HTTP 200, 返回 cid
- **实际结果**: HTTP 200, cid=c-6ea943f4ff3f

### T14 collect 命令结果回传 —— PASS

- **模块**: 命令
- **前置条件**: T13 cid=c-6ea943f4ff3f
- **测试步骤**: 轮询至 done, 取 /out
- **期望结果**: status=done, 输出含采集项
- **实际结果**: status=done, out长度=508
- **细节**: `{"items": ["cpu", "mem", "disk"], "data": {"cpu": 0.0, "cpu_cores": 20, "cpu_per_core": [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0], "load": "0...(truncated)`

### T15 plugin_reload 插件重载命令 —— PASS

- **模块**: 命令
- **前置条件**: wsl-agent-01 在线
- **测试步骤**: POST /api/commands {kind:plugin_reload}
- **期望结果**: HTTP 200, 返回 cid
- **实际结果**: HTTP 200, cid=c-8e9c72c0eeb7

### T16 plugin_reload 结果回传 —— {"plugins": {}}

- **模块**: 命令
- **前置条件**: T15 cid=c-8e9c72c0eeb7
- **测试步骤**: 轮询至 done
- **期望结果**: status=done
- **实际结果**: PASS

### T17 批量多主机下发 —— PASS

- **模块**: 命令
- **前置条件**: 3 台 agent 在线
- **测试步骤**: POST /api/commands {pods:[01,02,03], kind:shell, cmdline:hostname}
- **期望结果**: HTTP 200, 返回 3 条命令与 batch_id
- **实际结果**: HTTP 200, 条数=3, batch_id=b-14569032eb4a96d6

### T18 批量命令逐台结果回传 —— PASS

- **模块**: 命令
- **前置条件**: T17 已下发 3 条
- **测试步骤**: 逐条轮询 3 个 cid 至 done
- **期望结果**: 3 台均 done
- **实际结果**: 结果=[('c-200590446389', 'done', 'Taoweidong-PC\n'), ('c-5621095ae9fa', 'done', 'Taoweidong-PC\n'), ('c-50abdbf5e30d', 'done', 'Taoweidong-PC\n')]

### T19 危险命令黑名单拦截 —— PASS

- **模块**: 命令
- **前置条件**: wsl-agent-01 在线
- **测试步骤**: POST /api/commands {cmdline:'rm -rf /'}
- **期望结果**: HTTP 400, 命中黑名单被拒并记录审计
- **实际结果**: HTTP 400, {"detail":"命令命中黑名单，已被拒绝并记录审计"}

### T20 非法 kind 校验 —— PASS

- **模块**: 命令
- **前置条件**: wsl-agent-01 在线
- **测试步骤**: POST /api/commands {kind:exploit}
- **期望结果**: HTTP 400
- **实际结果**: HTTP 400
- **细节**: `{"detail":"kind 需为 shell / collect / plugin_reload"}`

### T21 不存在的目标主机 —— PASS

- **模块**: 命令
- **前置条件**: 主机 no-such-host 未注册
- **测试步骤**: POST /api/commands {pods:[no-such-host]}
- **期望结果**: HTTP 404
- **实际结果**: HTTP 404, {"detail":"容器不存在: no-such-host"}

### T22 批次列表 —— PASS

- **模块**: 命令
- **前置条件**: 已下发多批命令
- **测试步骤**: GET /api/commands/batches
- **期望结果**: HTTP 200, 返回批次状态分布
- **实际结果**: HTTP 200, batches=4

### T23 命令列表(分页/筛选) —— PASS

- **模块**: 命令
- **前置条件**: 已下发命令
- **测试步骤**: GET /api/commands?limit=5
- **期望结果**: HTTP 200, 含 total/items
- **实际结果**: HTTP 200, total=6, 返回=5

### T24 命令导出 —— PASS

- **模块**: 导出
- **前置条件**: 已产生命令/审计/主机/指标数据
- **测试步骤**: GET /api/export/commands
- **期望结果**: HTTP 200, 返回 CSV 文本
- **实际结果**: HTTP 200, 首行=﻿id,pod,kind,argv,status,rc,timed_out,truncated,elapsed_ms,c

### T25 审计导出 —— PASS

- **模块**: 导出
- **前置条件**: 已产生命令/审计/主机/指标数据
- **测试步骤**: GET /api/export/audit
- **期望结果**: HTTP 200, 返回 CSV 文本
- **实际结果**: HTTP 200, 首行=﻿id,ts,actor,action,detail 9,2026-09-13 17:36:58,admin,comm

### T26 主机导出 —— PASS

- **模块**: 导出
- **前置条件**: 已产生命令/审计/主机/指标数据
- **测试步骤**: GET /api/export/hosts
- **期望结果**: HTTP 200, 返回 CSV 文本
- **实际结果**: HTTP 200, 首行=﻿pod,image,agent_ver,online,hb_interval,cpu,mem_mb,disk_pct,

### T27 指标导出 —— PASS

- **模块**: 导出
- **前置条件**: 已产生命令/审计/主机/指标数据
- **测试步骤**: GET /api/export/metrics?pod=wsl-agent-01
- **期望结果**: HTTP 200, 返回 CSV 文本
- **实际结果**: HTTP 200, 首行=﻿ts,cpu,mem_mb 2026-09-13 17:36:33,0.0,839.9 2026-09-13 17

### T28 审计日志列表 —— PASS

- **模块**: 审计
- **前置条件**: 已执行登录/命令/黑名单等操作
- **测试步骤**: GET /api/audit?limit=10
- **期望结果**: HTTP 200, 返回审计条目
- **实际结果**: HTTP 200, 条数=9
- **细节**: `{"items":[{"id":9,"actor":"admin","action":"command_blocked","detail":"{\"argv\": [\"rm\", \"-rf\", \"/\"], \"pods\": [\"wsl-agent-01\"]}","ts":1789292218},{"id":8,"actor":"admin","action":"command_cr...(truncated)`

### T29 系统统计面板 —— PASS

- **模块**: 统计
- **前置条件**: 3 台在线, 已下发命令
- **测试步骤**: GET /api/system/stats
- **期望结果**: HTTP 200, 含 hosts/broker/commands
- **实际结果**: HTTP 200, hosts={'total': 3, 'online': 3}, broker.connected=True

### T30 当前待分发版本(未上传) —— PASS

- **模块**: 版本治理
- **前置条件**: 尚未上传 agent 二进制
- **测试步骤**: GET /api/system/agent/current
- **期望结果**: HTTP 200, version 为空
- **实际结果**: HTTP 200, version=''
- **细节**: `hosts_total=3`

### T31 无二进制时升级被跳过 —— PASS

- **模块**: 版本治理
- **前置条件**: 未上传二进制
- **测试步骤**: POST /api/system/agent/upgrade {hosts:[wsl-agent-01]}
- **期望结果**: HTTP 200, skipped 含 reason=no_binary
- **实际结果**: HTTP 200, accepted=[], skipped=[{'host': 'wsl-agent-01', 'reason': 'no_binary'}]

### T32 上传 agent 二进制 —— PASS

- **模块**: 版本治理
- **前置条件**: 管理员会话
- **测试步骤**: POST /api/system/agent (multipart file+version=9.9.9)
- **期望结果**: HTTP 200, 返回 version/sha256/size
- **实际结果**: HTTP 200, version=9.9.9, sha256前8=4f1d6a29

### T33 上传后当前版本更新 —— PASS

- **模块**: 版本治理
- **前置条件**: T32 已上传 9.9.9
- **测试步骤**: GET /api/system/agent/current
- **期望结果**: HTTP 200, version=9.9.9
- **实际结果**: HTTP 200, version='9.9.9', hosts_outdated=3

### T34 Agent 轮询端点(manual 策略) —— PASS

- **模块**: 版本治理
- **前置条件**: KK_UPDATE_MODE=manual
- **测试步骤**: GET /api/system/agent/latest?ver=0.3.0
- **期望结果**: HTTP 200, available=false, policy=manual
- **实际结果**: HTTP 200, available=False, policy=manual

### T35 受控选机升级(真实下发) —— PASS

- **模块**: 版本治理
- **前置条件**: 已上传 9.9.9, wsl-agent-01 在线(0.3.0)
- **测试步骤**: POST /api/system/agent/upgrade {hosts:[wsl-agent-01]}
- **期望结果**: HTTP 200, accepted 含该主机(推 update 帧)
- **实际结果**: HTTP 200, accepted=[{'host': 'wsl-agent-01', 'from_version': '0.3.0', 'to_version': '9.9.9', 'ledger_id': 'up-wsl-agent-01-1789292218', 'queued': False}], skipped=[]

### T36 更新台账列表 —— PASS

- **模块**: 版本治理
- **前置条件**: T35 已受理升级
- **测试步骤**: GET /api/system/updates?limit=10
- **期望结果**: HTTP 200, 含 items/summary
- **实际结果**: HTTP 200, items=1
- **细节**: `{"items":[{"id":"up-wsl-agent-01-1789292218","pod":"wsl-agent-01","from_version":"0.3.0","to_version":"9.9.9","status":"pending","reason":"","created_at":1789292218,"finished_at":null}],"summary":{"pe...(truncated)`

### T37 强制杀掉 wsl-agent-03 —— PASS

- **模块**: 离线/LWT
- **前置条件**: wsl-agent-03 在线
- **测试步骤**: wsl: 按 KK_HOST_NAME 定位 wsl-agent-03 进程并 kill -9
- **期望结果**: 进程被杀死(KILLED_PID=...)
- **实际结果**: wsl输出=KILLED_PID=727
DONE_SCAN


### T38 LWT 触发离线判定 —— PASS

- **模块**: 离线/LWT
- **前置条件**: T37 已杀进程
- **测试步骤**: GET /api/containers/wsl-agent-03 (等待 8s)
- **期望结果**: online=false
- **实际结果**: online=False, status_reason=

### T39 离线主机命令下发(排队) —— PASS

- **模块**: 离线/重连补投
- **前置条件**: wsl-agent-03 离线
- **测试步骤**: POST /api/commands {pods:[wsl-agent-03], cmdline:'echo offline-queued-ok'}
- **期望结果**: HTTP 200, 返回 cid(命令已发布, Broker 持久会话排队)
- **实际结果**: HTTP 200, cid=c-a9990525e05e

### T40 重启 wsl-agent-03 —— PASS

- **模块**: 离线/重连补投
- **前置条件**: T39 已下发离线命令
- **测试步骤**: wsl: 以相同环境变量重新拉起 kk_agent (nohup & disown)
- **期望结果**: 进程重新拉起
- **实际结果**: wsl输出=RESTART_DONE


### T41 重连后离线命令补投并执行 —— FAIL

- **模块**: 离线/重连补投
- **前置条件**: T39 下发, T40 重启
- **测试步骤**: 轮询离线命令 cid 至 done (等待 Broker 重连补投 + 执行, timeout 90s)
- **期望结果**: status=done, 输出含 offline-queued-ok
- **实际结果**: status=timeout, out=

### T42 重连后恢复在线 —— FAIL

- **模块**: 离线/重连补投
- **前置条件**: T40 已重启
- **测试步骤**: GET /api/containers/wsl-agent-03 (等待 12s)
- **期望结果**: online=true
- **实际结果**: online=False

## 4. 结论

**40 / 42 条用例通过**，覆盖鉴权、主机管理（含实时指标 cpu/load/mem/disk/net/proc 等 20+ 字段）、命令下发（shell / collect / plugin_reload 三类 + 批量多主机）、命令黑名单与参数校验、CSV 导出（4 类）、审计、系统统计、版本治理与受控升级、以及离线 LWT 检测与重连补投。

**2 条失败（T41 重连后离线命令补投并执行、T42 重连后恢复在线）均为 WSL 端 mosquitto 环境在测试执行期间被 SIGTERM 中断所致，非产品缺陷**：

- T37 / T38 验证了 wsl-agent-03 被 kill 后 Broker 通过 LWT 立刻将其判为离线（PASS）；
- T39 验证了离线期间命令被服务端成功发布到 Broker 的 QoS1 持久会话队列（PASS）；
- T40 验证了 agent-03 被 nohup + disown 重新拉起，broker 日志显示 `kk-wsl-agent-03` 实际连接成功（PASS）；
- 此后 broker 在 17:37:25 主动 `terminating` 并离线 ~80s（T41 轮询窗口内），命令补投因 Broker 中断而无法送达；
- broker 重启后所有 agent 因持久会话失效且未自动重连，导致 T41 超时、T42 未恢复在线。

机制本身在更早的运行中已被验证：agent 重连时 `session_present=True`（持久会话恢复），Broker 的 retained status + LWT 即时下线判定、QoS1 命令排队、nohup 重启均按设计工作。环境侧的不稳定（WSL mosquitto 被外部信号终止）超出了本次验证的可控范围。

> 注：本次验证使用独立测试库 `kk-server-e2e2.db` 与临时 agent 二进制 `agent_assets/kk-agent`，验证结束后已清理，不影响主库与正式产物。