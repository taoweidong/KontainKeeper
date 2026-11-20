# 全量审视优化账本（2026-11-20/21，QR1120 系）

多 agent 并行勘察 + 实现的一轮全量优化。编号系 `QR1120-{S|A|W}x`（S=server，A=agent，W=web），
与 QR-（09-29/10-06）、QR29-（10-29）是不同账本。

## 方向约束（用户裁决）

- Agent 接入管控**只靠服务端 `KK_AGENT_IPS` 白名单**（上行帧自报 ip 校验），不加任何新权限/鉴权层。
- Agent 保持「单二进制 + 一条启动命令」极简形态：`./kk-agent mqtt://broker:1883` 是唯一门槛
  （唯一必填 Broker 地址，其余全有默认值）——本轮核实无需改动，未新增任何必填配置。
- 升级接口鉴权现状已是 `AdminUser`（QR-S42 既成事实，`test_privileged_write_endpoints_require_admin`
  锁死 operator 403）。按「不加新权限层」约束**未动**——不收紧也不放松。

## 基线与验证

- 开工基线：后端 540 条口径（0 失败 / 恰好 5 条 Broker 集成 skipped），前端 vitest 177 条。
- 收口门禁 `bash scripts/quality_gate.sh`：**后端 675 tests / 0 failures / 0 errors / skipped=5；
  前端 typecheck 0 错 + vitest 27 文件 254 条全过**。
- 本机无 Docker，Broker 级集成用例按门禁口径 skip（恰好 5）；有 Broker 的 CI 节点须 0 skipped 复验。
- 新增回归：后端 6 个 `test_opt1120_*.py`（bridge 24 / store 18 / runtime 13 / upgrade 44 /
  update 10 / security 22），前端 9 个 `opt1120-*.spec.ts(x)` + kk.spec 扩充。

## 已修复（server，QR1120-S1..S18）

| 编号 | 严重度 | 问题 | 修法 | 位置 |
|---|---|---|---|---|
| S1 | 高 | 审计节流 key 含自报 ip/任选主题 host，轮换即绕过灌库 | 8 处 key 收口为按 action 全局；stats 仍逐帧计数 | services/mqtt_bridge.py |
| S2 | 中 | 入站帧无尺寸上限 | json.loads 前 1MB 预检，超限丢弃+计数+节流日志 | mqtt_bridge.py |
| S3 | 中 | topic host 超长 → MySQL 1406 整帧炸 | 入口拒收 len>120（对齐 pod 列宽）+计数 | mqtt_bridge.py |
| S4 | 低 | `_parse_url` 坏端口裸 ValueError | 包可读错误（含 KK_MQTT_URL 字样、1-65535 范围） | mqtt_bridge.py |
| S5 | 中 | 良性不更新回执被记 failed（与 A7 契约） | rc=0 落 done 且 reason 进台账；不写 failed 审计 | mqtt_bridge.py |
| S6 | 高 | 迟到 LWT 把已重连主机永久打回离线 | record_hb 回置 online=1 并清 status_reason（心跳即在线证据） | models/_store_hosts.py |
| S7 | 中 | LWT ts 冻结在 Agent 启动时刻，污染宽限判定 | 离线分支 status_ts 恒用服务器时间；在线分支钳制不变 | _store_hosts.py |
| S8 | 中 | append_result 水位 CASE 绑原始 seq（字符串 seq 在 PG 类型报错） | 三处 CASE 一律绑规整 seq_n | models/_store_commands.py |
| S9 | 中 | batch_summary 全表 GROUP BY 无时间窗 | 子查询先挑最近 N 批次再聚合（查询级，不动索引/schema） | _store_commands.py |
| S10 | 中 | hb 帧 raw 入库无尺寸收敛（插件可撑爆 LONGTEXT） | 256KB 封顶三级 sanitise，每级产物合法 JSON、标准标量保住 | _store_hosts.py |
| S11 | 低 | upsert_container image/agent_ver 未 clip | 补 _clip(200)/_clip(40) 对齐 set_online | _store_hosts.py |
| S12 | 低 | finish_updates_reaching 逐条 UPDATE；批量升级缺批量建台账 | 单语句 IN（分片/空短路）；新增 create_updates_batch | _store_commands.py |
| S13 | 中 | 上传整包双份驻留内存（峰值 ~128MB） | 流式落盘 + 增量 sha256/HMAC，64MB 流式中强制，os.replace 原子落盘 | controllers/agent_update.py |
| S14 | 中 | swap 与下载竞态（Windows 共享冲突） | rename/open PermissionError 短退避重试（3×0.2s）；下载不加锁 | agent_update.py |
| S15 | 中 | .prev 保留失败静默 → 回滚能力悄悄消失 | log.warning + 审计 agent_prev_lost（KV fail-closed 口径不变） | agent_update.py |
| S16 | 中 | 批量升级逐台串行建台账（500 台=500 次 DB 往返） | 按版本×在线态分组 create_updates_batch，publish 逐台语义不变 | agent_update.py |
| S17 | 低 | 版本号校验过弱（"1abc" 可入库） | `^\d+(\.\d+)*$` fullmatch | agent_update.py |
| S18 | — | 黑名单 wrapper 绕过（env/sudo/timeout） | **核实已被 QR-S41 修复**，本轮只补回归锁（含 API 边界审计+零帧+不误杀） | services/security.py（未改） |

## 已修复（agent，QR1120-A1..A12）

| 编号 | 严重度 | 问题 | 修法 | 位置 |
|---|---|---|---|---|
| A1 | 高 | 插件隔离检查与发布口径不一致：孤立代理项/RecursionError 每拍丢整帧心跳 | 检查改 `json.dumps(ensure_ascii=False).encode("utf-8")` + catch Exception | plugin_loader.py |
| A2 | 中 | 序列化检查在超时保护外，可任意耗时卡 busy | 挪进 _collect_with_timeout worker（三类语义区分保持：collect 异常不隔离/卡死隔离/坏输出隔离） | plugin_loader.py |
| A3 | 中 | 单插件输出无体积上限 | 64KB 封顶（恰好放行），超限按坏输出隔离 | plugin_loader.py |
| A4 | 中 | timeout=Infinity → OverflowError 穿透无回执 | except 补 OverflowError 回落 30s | executor.py |
| A5 | 低 | 采集项异常静默 continue 无痕迹 | 首败 warning、续败 debug、成功清零（带锁，collect 命令池并发安全） | collector.py |
| A6 | 中 | 首拍更新检查无抖动（批量部署惊群） | next_update = now + uniform(0, interval) | main.py |
| A7 | 中 | not_newer/backoff 回 rc=1 → 台账假 failed（QoS1 重投） | BENIGN_SKIP_REASONS={not_newer,backoff,update_busy} 映射 rc=0，reason 保留 | main.py / updater.py |
| A8 | 低 | 下载无总时限 + 锁等待无超时 → 线程堆积 | 300s 单调钟总时限；_update_lock.acquire(timeout=10) 拿不到回 update_busy | updater.py |
| A9 | 低 | 下载半成品 tmp 进程死亡后永不清理 | 启动清扫同目录 .kk-agent.download.*（mtime>1h，源码形态空操作） | updater.py / main.py |
| A10 | 低 | 轮询 fetch_latest 失败只留 debug 无痕 | 首败 warning、每 10 次 warning、成功清零 | updater.py |
| A11 | 低 | announce_update 失败静默（B6.1 宽限高延迟链路失效） | 三种失败各留 warning，不阻断 execv | transport.py |
| A12 | 中 | 入站 cmd 帧无尺寸上限；克隆机互踢会话无提示 | cmd 64KB 预检（节流 warning）；MQTT5 142 定向提示 / v311 通用 KK_HOST_NAME 提示 | transport.py |

## 已修复（web，QR1120-W1..W33）

交互流畅度：
- W1 update 页勾选按 pod 集合 diff 才回填（废每 10s clearSelection+逐台 toggle）；升级成功改 clearSelection 清模型+表格
- W2 monitor/update 轮询 keyed merge（mergeHostRows，age_sec 豁免比较就地补值，未变行保引用）
- W3 trend setOption 改 merge；markLine 恒在、null 时 data 空（清除上限语义保持，既有锁裁决）
- W4 trend 换主机清场+加载态；空态 loading/failed/empty 三态
- W5 HostPicker draft 转 Set + 前端分页（50/100/200，row-key 跨页保勾）
- W6 tsText 模块级缓存 zh-CN Intl formatter
- W7 monitor 行点击检测文本选区，复制 IP/主机名不再误触跳转

统计口径：
- W8 welcome cmdStats 补 queued（与 system 页口径对齐）
- W9 detail 运行时长 ageText→durText
- W10 detail/HostPicker 在线标签复用 hostState* 三态（恢复 updating 语义）
- W11 detail pollFailed 覆盖 getHost 失败
- W12 trend 切时间窗不回填未保存上限（load 加 syncInputs 参数；保存后同步语义保持）
- W13 trend 页头读数 pctText + 该点时刻 tooltip
- W14 monitor 计量条统一 %（pctText）
- W15 fillPct 抽到 utils/kk.ts，monitor/detail 共用（下钳/NaN 防护）
- W16 零散口径：detail load `?? "-"`；system 缺键显「—」；welcome offline 夹 0；system 增量 span 走 durText；trend/detail tooltip 按轴配单位；system 停刷时最近一帧显绝对时刻
- W17 detail 图表空数据占位（含「没读到」分支）

布局/上传/其他：
- W18/W29 弹窗宽度 `min(92vw, Npx)`（host 侧与 RBAC 三页）
- W19 HostPicker 全选并集/反选对称差（筛选外已选不再静默清空）
- W20 uploadAgent timeout:0 + onUploadProgress 透传 + 上传对话框 el-progress（http 封装本就展开第 3 参，无需改）
- W21 system COUNTER_META 收录 oversized_dropped/host_too_long_dropped（正常恒 0，增长=入站被攻击），移除 test_proto_window 临时豁免
- W22 command pollFailed 失败恒 true（六页口径对齐，既有锁裁决）
- W23 用户/角色删除套 ElMessageBox.confirm
- W24 命令输出 >256KB 头尾切片渲染 + 省略引导 + 下载完整输出（纯前端，不改 API）
- W25 shell pods >20 台或 >1500 字符不落 URL（sessionStorage 兜底，?cmdline= 一次性抹除）
- W26 审计明细 load 后整批预解析（WeakMap 按行缓存）
- W27 RePureTableBar additive loading prop（refreshSpin 状态机抽出可单测），command/audit 接线；未接线页旧行为有锁
- W28 RBAC 三页 read 三态空态（QR-W16 红线对齐）
- W30 命令历史同步读数按 hasActive 显当前档位
- W31 logOut 接 clearPolls()；删 handRefreshToken/refreshTokenApi 死代码（锁裁决）
- W32 角色创建授权失败指引「编辑该角色补授权」+ 关框刷新
- W33 审计搜索按钮统一 reloadFirstPage

## 既有锁裁决清单（均 docstring 注明依据）

1. server/tests/test_api.py `_seed`：离线种子改在最后一帧心跳之后 set_online(False)（QR1120-S6，与 _seed_meta 同款时序）
2. server/tests/test_proto_window.py：proto_mismatch 审计计数 2→1（S1 全局节流）；新计数器 COUNTER_META 收录后移除临时豁免（S2/S3/W21）
3. web trend hook.spec「上限线」：markLine 恒在、null 时 data 空（W3 merge 后省略不会移除旧线）
4. web command hook.spec：手动失败 pollFailed 置位（W22 对齐 W5/QR-W17）
5. web user.spec / api/user.spec：删死代码锁、新增 clearPolls 真停表锁（W31）

## 核实已被前轮修复（11-03 报告过期项，本轮未重复改）

pollFailed 六页中五页、trend 静默不回填、DEFAULT_ITEMS 收敛、downloadOut 在途守卫、
Agent 坏端口 TransportError、非 str kind 恒回 127、record_hb interval 钳制、set_online clip、
pods 去重 strip、负 limit 钳制、uid 同秒唯一、插件心跳停摆主形态（QR-A11）、
黑名单 wrapper 解析（QR-S41）、升级接口 AdminUser（QR-S42）。

## 列账不做（需单独裁决）

- 架构级低项：adaptive offsetBottom 容器相对测量重构（L-6）、刷新间隔档位组件统一（L-7）、
  跨页消息出口统一（O-3）、kwTimer/clampPage/severity 映射收敛（O-8）、detail 描述列按容器宽（L-5）
- 历轮裁决边界：batch_summary 索引/schema 变更、MySQL 真库 CI 矩阵、mqtt_bridge 拆模块、
  collector 差分换单调钟、token Cookie 属性、菜单树三份收敛
- U9 auto 推送 in_flight 去重的部分唯一索引兜底（涉 schema，check-then-create 竞态窗口仍在，低危）
- QR29-S6 黑名单覆盖面（curl|sh/truncate/find -delete 等放行）——产品裁决项，现状有锁
- RePureTableBar loading prop 在 host 侧页接线（monitor/update 仍旧 500ms 行为，有锁保）

## 运维可见变化

- 同类安全事件审计全网 60s 只落一条（原按 host/ip 分 key）——防灌库收口，溯源靠首条 detail + stats 逐帧计数；审计页「事件变少」不是丢事件。
- 升级台账良性跳过（not_newer/backoff/update_busy）落 done 带 reason，不再污染失败率。
- 统计页新增「超大帧丢弃」「超长主机名丢弃」两计数器，正常恒 0。