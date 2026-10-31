"""SQLAlchemy 表定义：SQLite / PostgreSQL / MySQL 三库通用的 Core schema。

从 store.py 拆出——纯声明性，不含任何查询逻辑。表名统一加 kk_ 前缀避免与同库其他应用撞名。
"""
from sqlalchemy import (BigInteger, Column, Float, Index, Integer, MetaData,
                        String, Table, Text)
from sqlalchemy.dialects import mysql

MD = MetaData()

# MySQL 的 TEXT 只有 64KB，而命令输出 base64 后最大约 5.6MB（KK_MAX_OUT_MB=4），
# 不升级成 LONGTEXT 会被静默截断。其余两家原生就是无限长。
_LONGTEXT = Text().with_variant(mysql.LONGTEXT(), "mysql")


def _long_text():
    return _LONGTEXT


# pod 列即主机标识（历史命名，产品术语已统一为「主机 / host」；改名的迁移成本
# 换不到任何功能收益，故表名与列名保持原样）。
#
# cpu / mem_mb / disk_pct 是最近一帧心跳的摘要冗余：500 台列表若逐行 json.loads
# 完整 last_metrics（每帧 2~4KB）就是 1~2MB 的 CPU 开销，摘要列让列表接口只读列。
# 新增列靠 store.setup() 的 _ensure_schema 补，既有库无需手工迁移。
containers = Table(
    "kk_containers", MD,
    Column("pod", String(120), primary_key=True),
    Column("image", String(200), nullable=False, server_default=""),
    Column("agent_ver", String(40), nullable=False, server_default=""),
    Column("hb_interval", Integer, nullable=False, server_default="60"),
    Column("first_seen", BigInteger, nullable=False),
    Column("last_seen", BigInteger, nullable=False),
    Column("last_metrics", _long_text(), nullable=False),
    Column("online", Integer, nullable=False, server_default="0"),
    Column("status_ts", BigInteger, nullable=False, server_default="0"),
    # 最近一帧 status 的可读原因（online / offline / updating / LWT 触发为空）。
    # B6.1：自更新会先自报 reason=updating，让运维能把「正在升级」与「容器停了」分开。
    Column("status_reason", String(20), nullable=False, server_default=""),
    Column("cpu", Float),
    Column("mem_mb", Float),
    Column("disk_pct", Float),
    # ---- v4：任意 Linux 主机（不再只是 K8S 容器 IDE）----
    # host_type：container / vm / metal。存量行由 _ADD_COLUMNS 回落 container，
    #            既保持旧语义又无需数据迁移。
    Column("host_type", String(16), nullable=False, server_default="container"),
    Column("os_name", String(80), nullable=False, server_default=""),
    Column("kernel", String(80), nullable=False, server_default=""),
    Column("arch", String(20), nullable=False, server_default=""),
    # ip：最近一帧自报出口 IP（v3 白名单校验用），总览页副行展示
    Column("ip", String(64), nullable=False, server_default=""),
    # group_name / labels：机队筛选的两个维度（来自 Agent KK_GROUP / KK_LABELS）
    Column("group_name", String(64), nullable=False, server_default=""),
    # labels / caps 不带 server_default：MySQL 拒绝「BLOB/TEXT 带字面量 DEFAULT」
    # （1101），建表和 ALTER 两条路都炸（QR-S30）。空值由写入侧给 ""（见
    # Store.upsert_container / _jtext），升级库里补列前的旧行是 NULL，两者同义为
    # 「未上报」
    Column("labels", _long_text(), nullable=False),
    # caps：能力声明 JSON（{"shell":true,"docker":true,...}）。此阶段**只落库供展示与
    #       排障**，服务端按 caps 拦命令的门禁还没写（现在拦 shell 的是 Agent 侧
    #       allow_shell），别把它当成已经在守的防线
    Column("caps", _long_text(), nullable=False),
    # mem_limit / disk_limit：单机告警上限（百分比 1–100），NULL = 未自定义——
    # 磁盘回落全局 DISK_ALERT_PCT（85），内存没有全局口径就不画线。
    # 只服务趋势页的告警线与状态读数，不改变总览页 disk_alert 的全局口径
    Column("mem_limit", Integer),
    Column("disk_limit", Integer),
    # docker_*：Docker 摘要冗余列（v4 后的容器阶段写入，此阶段恒 0）
    Column("docker_total", Integer, nullable=False, server_default="0"),
    Column("docker_running", Integer, nullable=False, server_default="0"),
    Column("docker_unhealthy", Integer, nullable=False, server_default="0"),
    # 该主机上报的协议版本：兼容窗口期据此把「待升级」标出来
    Column("proto_ver", Integer, nullable=False, server_default="3"),
)

heartbeats = Table(
    "kk_heartbeats", MD,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("pod", String(120), nullable=False),
    Column("ts", BigInteger, nullable=False),
    Column("cpu", Float),
    Column("mem_mb", Float),
    # mem_pct / disk_pct：单机趋势页的两个序列列（cpu/mem_mb 同款写侧摘列）。
    # disk_pct = 多挂载点最大使用率；NULL = 该帧没带这类数据（老 Agent / 采集被关），
    # 趋势图上「没读到」与「用到 0%」必须可分（QR-W16 同一口径），所以不给默认值
    Column("mem_pct", Float),
    Column("disk_pct", Float),
    Column("metrics", _long_text(), nullable=False),
    Index("idx_hb_pod_ts", "pod", "ts"),
    # 小时聚合（_aggregate_hours）只按 ts 范围过滤：前导列是 pod 的唯一索引帮不上，
    # 没有这条索引每个小时桶都是全表扫描（QR-S4）
    Index("idx_hb_ts", "ts"),
    # PG 专属：ts 与写入物理相关，时间范围扫描（cleanup 挑批 / 聚合窗口）用
    # KB 级 BRIN 替代整棵 btree；其他方言忽略该 kwargs（标准行为），
    # 存量 PG 库由 _pg_tune 的 CREATE INDEX IF NOT EXISTS 补齐
    Index("idx_hb_ts_brin", "ts", postgresql_using="brin"),
)

hourly = Table(
    "kk_hourly", MD,
    Column("pod", String(120), primary_key=True),
    Column("hour", BigInteger, primary_key=True),
    Column("samples", Integer, nullable=False, server_default="0"),
    Column("cpu_avg", Float), Column("cpu_max", Float),
    Column("mem_avg", Float), Column("mem_max", Float),
    # mem_pct_avg / disk_avg：单机趋势页的小时聚合（disk = 多挂载点最大值的均值）。
    # 可空：聚合对 NULL 跳过（SQL AVG/MAX 语义），补列前的旧桶自然是 NULL
    Column("mem_pct_avg", Float),
    Column("disk_avg", Float),
    Column("last_metrics", _long_text(), nullable=False),
)

commands = Table(
    "kk_commands", MD,
    Column("id", String(32), primary_key=True),
    Column("pod", String(120), nullable=False),
    Column("kind", String(20), nullable=False, server_default="shell"),
    # LONGTEXT 而非 TEXT（QR29-S13）：argv 是整条用户输入（shell 模式存完整命令串），
    # MySQL TEXT 只有 64KB，超长在 INSERT 上炸 1406；新库直接建大列，老库 TEXT 对
    # 常规命令也够用（配合审计侧 add_audit 的入库截断双保险）
    Column("argv", _long_text(), nullable=False),
    Column("timeout", Integer, nullable=False, server_default="30"),
    Column("status", String(12), nullable=False, server_default="pending"),
    Column("created_by", String(64), nullable=False, server_default=""),
    Column("created_at", BigInteger, nullable=False),
    Column("sent_at", BigInteger),
    Column("finished_at", BigInteger),
    Column("rc", Integer),
    Column("timed_out", Integer, nullable=False, server_default="0"),
    Column("truncated", Integer, nullable=False, server_default="0"),
    Column("elapsed_ms", BigInteger),
    Column("out_b64", _long_text(), nullable=False),
    # out_b64 的尾段镜像（末 2732 个 base64 字符，写入时随分块维护）：
    # 列表/导出路径只读这列的小尾巴，不再整列拖 out_b64——单行输出最大 ~5.6MB，
    # 500 行列表最坏 GB 级 DB→应用传输（QR-S1）。2732 = ceil(2048/3)*4，
    # 右截到 4 的倍数仍是合法 base64（与 helpers._b64_tail 同一算术）。
    Column("out_tail", String(2732), nullable=False, server_default=""),
    Column("out_chunks", Integer, nullable=False, server_default="0"),
    # 结果帧幂等水位：已应用的最大 seq。默认 -1 使首块 seq=0 也能被应用
    # （P1-6：QoS1 重投同 seq 块会导致输出翻倍）。
    Column("last_seq", Integer, nullable=False, server_default="-1"),
    # 输出被保留策略清掉后置 1：状态行还在，但命令回显已不可追溯，
    # 前端据此提示「输出已清理」，而不是让用户对着空白以为命令没执行。
    Column("out_purged", Integer, nullable=False, server_default="0"),
    # 一次批量下发的批次号（b-<hex8>）：500 台一次点击后要能按「这一次操作」
    # 聚合核验与导出。索引暂不建——现有 idx_cmd_pod(pod, created_at) 在 500 台
    # 规模下够用；命令表上万级时再补（登记在二期备选）。
    Column("batch_id", String(32), nullable=False, server_default=""),
    Index("idx_cmd_pod", "pod", "created_at"),
)

# 自更新台账（A6.2）：升级是「一主机一次、长周期（下载 + 重启）」的操作，
# 生命周期与命令不同——塞进 kk_commands 会污染命令历史页（用户看到的「命令」里
# 混着升级记录），并让 sweep_command_timeouts 的超时语义混淆。
#
# status: pending（已下发、在途） / queued（下发时离线，Broker 排队中，D2.2） /
#         done / failed / timeout
# reason: 失败原因码，与 Agent updater 的原因码一一对应
updates = Table(
    "kk_updates", MD,
    Column("id", String(48), primary_key=True),          # up-<host>-<ts>
    Column("pod", String(120), nullable=False),
    Column("from_version", String(40), nullable=False, server_default=""),
    Column("to_version", String(40), nullable=False, server_default=""),
    Column("status", String(12), nullable=False, server_default="pending"),
    Column("reason", String(40), nullable=False, server_default=""),
    Column("created_at", BigInteger, nullable=False),
    Column("finished_at", BigInteger),
    Index("idx_upd_pod", "pod", "created_at"),
)

audit = Table(
    "kk_audit", MD,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("actor", String(64), nullable=False),
    Column("action", String(40), nullable=False),
    # LONGTEXT 而非 TEXT（QR29-S13）：detail 由 add_audit 入库前截到 8KB，
    # 这里跟着升级是让新库与截断上限之间不再有第二套列宽语义
    Column("detail", _long_text(), nullable=False),
    Column("ts", BigInteger, nullable=False),
)

admins = Table(
    "kk_admins", MD,
    Column("username", String(64), primary_key=True),
    Column("salt", String(64), nullable=False),
    Column("pw_hash", String(128), nullable=False),
    Column("created", BigInteger, nullable=False),
    # ---- RBAC（2026-10-31）：单角色口径（roles.name），权限只控菜单 ----
    # enabled：禁用后登录 403 且会话即时吊销；存量行由 _ensure_schema 回落 1
    Column("role", String(32), nullable=False, server_default="admin"),
    Column("enabled", Integer, nullable=False, server_default="1"),
)

# 角色：name 是前端与 role_menus 的关联键；built_in 角色不可删，admin 角色的
# 菜单集固定为全量（防止管理员把自己菜单改没后无从恢复——恢复手段只剩改库）。
roles = Table(
    "kk_roles", MD,
    Column("name", String(32), primary_key=True),
    Column("label", String(64), nullable=False),
    Column("remark", String(200), nullable=False, server_default=""),
    Column("built_in", Integer, nullable=False, server_default="0"),
    Column("created", BigInteger, nullable=False),
)

# 菜单：**菜单树由数据库下发**（2026-10-31 RBAC 二阶段）——路由注册数据
# （path / component）也在库里，前端 addAsyncRoutes 按 component 字符串走
# glob 解析组件（分组固定 "Layout"）；name 是前后端关联主键。菜单管理仍只改
# 展示元数据与启停，path/component 只读——改它们等于改路由契约，必须与
# 前端 views 目录同步，不是运行期可编辑的东西（见 controllers/rbac.py）。
menus = Table(
    "kk_menus", MD,
    Column("name", String(64), primary_key=True),
    Column("parent", String(64), nullable=False, server_default=""),
    Column("title", String(64), nullable=False),
    Column("icon", String(64), nullable=False, server_default=""),
    Column("rank", Integer, nullable=False, server_default="99"),
    Column("path", String(120), nullable=False, server_default=""),
    # 组件视图路径（glob key，如 /src/views/host/monitor/index.vue）；分组为 Layout
    Column("component", String(120), nullable=False, server_default=""),
    Column("show_link", Integer, nullable=False, server_default="1"),
    Column("enabled", Integer, nullable=False, server_default="1"),
    Column("built_in", Integer, nullable=False, server_default="0"),
)

# 角色↔菜单多对多；admin 角色不落行（allowed_menus 对 admin 直通全量）。
role_menus = Table(
    "kk_role_menus", MD,
    Column("role", String(32), primary_key=True),
    Column("menu", String(64), primary_key=True),
)

sessions = Table(
    "kk_sessions", MD,
    Column("token", String(64), primary_key=True),
    Column("username", String(64), nullable=False),
    Column("created", BigInteger, nullable=False),
    Column("expires", BigInteger, nullable=False),
)

kv = Table(
    "kk_kv", MD,
    Column("k", String(64), primary_key=True),   # MySQL 不接受 TEXT 主键，必须定长
    Column("v", Text, nullable=False),
)

_CMD_COLS = [c.name for c in commands.columns if c.name != "out_b64"]
ONLINE_GRACE = 180

# 列表摘要视图只读这几列：完整 last_metrics（每帧 2~4KB JSON）不进列表响应。
# 500 台 × 4KB = 2MB 的 JSON 解析开销，占了列表接口耗时的绝大部分。
# v4 追加的仍是**标量或极小 JSON**（caps/labels 里 labels 可能被运维写大，故不入列）：
# 分组/类型/Docker 异常/协议版本都是总览页的筛选与列，第二次查询换它们就是 N+1。
_SUMMARY_COLS = ["pod", "image", "agent_ver", "hb_interval", "online",
                 "last_seen", "cpu", "mem_mb", "disk_pct", "status_reason",
                 "host_type", "os_name", "ip", "group_name", "caps",
                 "docker_total", "docker_running", "docker_unhealthy", "proto_ver"]

# 既有库补列清单：create_all 不会给已存在的表加列，升级后必须自己 ALTER。
# 类型写三库都认的写法（DOUBLE PRECISION / BIGINT），避免再分支。
_ADD_COLUMNS = {
    "kk_containers": [("cpu", "DOUBLE PRECISION"), ("mem_mb", "DOUBLE PRECISION"),
                      ("disk_pct", "DOUBLE PRECISION"),
                      ("status_reason", "VARCHAR(20) DEFAULT ''"),
                      # v4：host_type 缺省 container —— 旧库里的行本来就是容器，
                      # 回落值必须与旧语义一致，不能图省事落空串
                      ("host_type", "VARCHAR(16) DEFAULT 'container'"),
                      ("os_name", "VARCHAR(80) DEFAULT ''"),
                      ("kernel", "VARCHAR(80) DEFAULT ''"),
                      ("arch", "VARCHAR(20) DEFAULT ''"),
                      ("ip", "VARCHAR(64) DEFAULT ''"),
                      ("group_name", "VARCHAR(64) DEFAULT ''"),
                      # labels/caps：类型必须与建表模型同型，否则升级库和新库同一列
                      # 两型（QR-S33）；且三家都不带 DEFAULT —— MySQL 拒绝 TEXT 类
                      # 字面量默认值（QR-S30），而写入侧本来就给 ""
                      ("labels", {"mysql": "LONGTEXT", "*": "TEXT"}),
                      ("caps", {"mysql": "LONGTEXT", "*": "TEXT"}),
                      ("docker_total", "INTEGER DEFAULT 0"),
                      ("docker_running", "INTEGER DEFAULT 0"),
                      ("docker_unhealthy", "INTEGER DEFAULT 0"),
                      ("proto_ver", "INTEGER DEFAULT 3"),
                      # 单机告警上限：NULL = 未自定义（磁盘回落全局 85），可空整数
                      ("mem_limit", "INTEGER"),
                      ("disk_limit", "INTEGER")],
    "kk_commands": [("out_purged", "INTEGER DEFAULT 0"),
                    ("last_seq", "INTEGER DEFAULT -1"),
                    # 不带引号：MySQL 严格模式下 'VARCHAR' 被引号包住会解析失败
                    ("batch_id", "VARCHAR(32) DEFAULT ''"),
                    # QR-S1：历史行的 out_tail 为空（不回填——回填要逐行读大字段，
                    # 新写入的命令即时生效）
                    ("out_tail", "VARCHAR(2732) DEFAULT ''")],
    # 单机趋势页的两个序列列（写侧摘列，与 cpu/mem_mb 同族）；NULL = 该帧未上报，
    # 聚合与序列查询对 NULL 跳过（SQL AVG/MAX 语义），老行不需要回填
    "kk_heartbeats": [("mem_pct", "DOUBLE PRECISION"),
                      ("disk_pct", "DOUBLE PRECISION")],
    "kk_hourly": [("mem_pct_avg", "DOUBLE PRECISION"),
                  ("disk_avg", "DOUBLE PRECISION")],
    # RBAC：存量管理员回落 admin 角色且保持启用（升级前后行为不变）
    "kk_admins": [("role", "VARCHAR(32) DEFAULT 'admin'"),
                  ("enabled", "INTEGER DEFAULT 1")],
    # 菜单表二阶段补列（路由注册数据）；存量行的空值由 seed_rbac 按内置种子回填
    "kk_menus": [("path", "VARCHAR(120) DEFAULT ''"),
                 ("component", "VARCHAR(120) DEFAULT ''")],
}

# 既有库补索引清单：create_all 同样不给已存在的表建索引。
# 名字三库通用（MySQL 索引名表内唯一，PG/SQLite schema 内唯一，全库小写不冲突）。
_ADD_INDEXES = {
    "kk_heartbeats": [("idx_hb_ts", ("ts",))],
    # v4：分组与主机类型是总览页的常驻筛选项，500 台时靠表扫会拖慢列表接口
    "kk_containers": [("idx_ct_group", ("group_name",)),
                      ("idx_ct_host_type", ("host_type",))],
}
