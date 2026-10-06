# 服务端（kk_server）深度分析与优化方案

> 分析范围：`server/src/kk_server/` 全量（models / services / controllers / main / config / logsetup），
> 以 services 层（`mqtt_bridge.py`）与存储层（`models/store.py`）为重点。
> 基线：`pytest server/tests` 170 passed / 4 skipped（无 Broker 时集成用例跳过，符合预期）。
> 分析日期：2026-09-29。

## 0. 总体评价（先说结论）

现有代码质量**显著高于一般业务项目**：N+1 已成批消除、三库方言差异收口在两处、
幂等水位 / 审计限速 / 批量分片等历史评审问题都有编号、有注释、有测试。因此本方案**刻意
排除「推倒重来」类建议**（上 ORM 全家桶、引 Redis、重建 WS 通道等），只列经过代码逐行
验证、有真实收益的优化点。

优化空间集中在四类：

1. **两个真实的性能热点**（聚合 N+1、CPU 重活占住事件循环）；
2. **一个跨数据库的健壮性缺口**（畸形帧数值字段在 PG/MySQL 上整帧失败）；
3. **三个防御纵深的小漏洞**（登录限流字典、result 锁表、摄入无背压）;
4. **一批工程卫生项**（索引、常量收敛、类型注解、lint 工具链）。

每项给出：位置 → 根因 → 建议改法 → 验收方式。分级沿用仓库惯例（P1 = 建议本轮就做，
P2 = 排期做，P3 = 顺手做/备选）。

---

## 1. P1：建议本轮就做

### S1 聚合任务的小时级 N+1（存储层最重路径）

- **位置**：`server/src/kk_server/models/store.py:776-816`（`_aggregate_hours`）
- **现象**：对每个待聚合小时，先一次 GROUP BY 取该小时有心跳的主机，然后**逐主机**
  再查一次「该小时最后一帧 metrics」（`store.py:802-806`）+ 逐行 `_upsert`（每行一个事务）。
- **量化**：500 台 × 24 小时窗口 = 24 次 GROUP BY + **24,000 次单查 + 24,000 次单行 upsert**，
  每次都是独立的连接 checkout / 事务。janitor 每 5 分钟跑一次 `cleanup`，服务端停机一天后
  首个聚合窗口就是上面这个最坏值——正好卡在「重启恢复」这个最不该卡的时机。
- **建议**：把「每主机最后一帧」改为**窗口函数一次取完**（三库均支持，MySQL 需 8.0+，
  与 `requires-python>=3.12` 的现代基线一致）：

  ```sql
  SELECT pod, ts, metrics FROM (
    SELECT pod, ts, metrics,
           ROW_NUMBER() OVER (PARTITION BY pod ORDER BY ts DESC) AS rn
    FROM kk_heartbeats WHERE ts >= :lo AND ts < :hi
  ) WHERE rn = 1
  ```

  聚合行本身按小时**批量 upsert**（SQLAlchemy 2.0 的 `insert().on_conflict_do_update`
  支持 executemany 形态；MySQL 走既有 `_upsert` 分支循环兜底即可）。语句数从 ~4.8 万
  降到 **每小时 3 条**。
- **验收**：`test_store.py` 既有聚合用例全绿 + 新增「500 主机 × 2 小时」规模用例，断言
  查询计数（可用 `engine` 事件统计 statement 数）下降一个数量级。

### S2 畸形帧的数值字段未规整：SQLite 侥幸通过、PG 必炸

- **位置**：`store.py:164`（`set_online` 的 `int(ts or time.time())`）、
  `store.py:212`（`record_hb` 的 `int(msg.get("interval") or 60)`）、
  `store.py:514-531`（`append_result` 的 `seq` / `rc` / `elapsed_ms` 直接入 SQL）。
- **根因**：`_num` / `_summary_of` / ts 守卫已经把 cpu/mem/ts 坏值兜住了，但 **interval、
  seq、rc、elapsed_ms、set_online 的 ts 这五处漏了**。Agent 发的帧是自家协议，但
  `interval="60s"` 这种字符串一旦出现：SQLite 上 int() 抛 ValueError → 整帧心跳事务回滚
  （数据空洞）；PG/asyncpg 上字符串进 Integer 列直接报类型错误。
- **建议**：加一个 `_int(value, default)` helper（与 `_num` 并排放在 `models/helpers.py`），
  五处统一替换。`seq` 额外要求 `None` 判断保留（幂等分支依赖它）。
- **验收**：新增用例：`interval` 传 `"abc"` / `ts` 传 `"yesterday"` / `seq` 传 `"3"`，
  断言帧仍落库且不抛异常（这正是 `_summary_of` 注释里承诺的行为：「坏数据返回 0 而不是
  让整帧失败」——把承诺补齐）。

### S3 PBKDF2 与 64MB sha256 同步执行在事件循环里

- **位置**：`store.py:714-721`（`verify_admin`，每次登录 2 次 PBKDF2@310k 轮迭代）、
  `store.py:694-708`（`ensure_admin`，lifespan 启动时）、
  `controllers/agent_update.py:94`（`hashlib.sha256(64MB)` 在 async 处理器内同步算）。
- **现象**：310k 轮 PBKDF2-SHA256 单次约 100–300ms。每次管理员登录，**整个事件循环
  （包括全部心跳落库、MQTT 帧处理）停摆 0.1–0.3s**；旧迭代数存量哈希的首次登录要算两次
  （`store.py:714` 与 `718`），停摆翻倍。上传 Agent 二进制时 64MB 哈希同样卡一圈。
- **建议**：`_pwdf` 的调用点包 `await asyncio.to_thread(...)`（Store 是协程，改动是
  局部的）；`upload_agent` 把 sha256 挪进已有的 `_write`（它已经 `to_thread`）或单独
  `to_thread`。`ensure_admin` 在 lifespan 里，启动期一次，可顺手同改。
- **验收**：登录接口压测（`httpx` 并发 10 × 心跳同时注入）对比改造前后心跳 P99 延迟；
  既有 auth 测试全绿。

---

## 2. P2：排期做

### S4 `kk_commands` 缺 `status` 索引，sweep 每 30s 全表扫

- **位置**：`models/tables.py:66-95`（仅有 `idx_cmd_pod(pod, created_at)`；
  `batch_id` 的注释里已自我预告「命令表上万级时再补」，见 `tables.py:90-93`）。
- **现象**：`sweep_command_timeouts`（`store.py:540`）与 `cleanup` 的 lost 收敛
  （`store.py:849`）每 30s / 5min 各扫一次全表，过滤条件是 `status IN (pending,sent,running)`
  ——而表里绝大多数行是终态。批量下发常态化后命令表进入「万级行 × 每分钟两次全扫」。
- **建议**：`commands` 表补 `Index("idx_cmd_status", "status")`（终态行占绝对多数，
  普通索引即可，不必整 partial index 的三库兼容麻烦）；`batch_id` 索引按原注释的触发条件
  一起登记。新表结构用 `create_all`；**存量库要走 `tables._ADD_COLUMNS` 同款机制补索引**
  （现在该机制只补列，需扩展支持 `CREATE INDEX IF NOT EXISTS`——SQLite/PG 支持，
  MySQL 用 information_schema 判存在性，收口在 `_ensure_schema` 一处，符合仓库约束）。
- **验收**：三库方言测试（`test_dialects.py`）覆盖补索引路径；`EXPLAIN` 抽查 sweep 语句走索引。

### S5 两个「无界字典」的防御纵深缺口

- **位置 A**：`controllers/auth.py:40-48` —— `_reap` 只清理**已锁定**的键；
  1–4 次失败未触发锁定的用户名 / IP 计数**永不清除**。分布式低速用户名枚举
  （每用户名试 2 次换下一个）可以让 `_LOGIN_FAILS` 无限增长。
- **位置 B**：`services/mqtt_bridge.py:299-304` —— `_on_result` 先建 `asyncio.Lock`
  再查命令存在性，**未知 cid 也进 `_result_locks` + `_result_lock_ts`**。有白名单内
  Agent 被攻破的场景下，随机 id 洪泛可在 600s 回收窗口内灌出百万级条目。
- **建议**：
  - A：失败计数改存 `(count, last_ts)`，`_reap` 同时清「5 分钟内无新增失败」的键
    （临界区内多一次遍历，成本可忽略，与原注释的成本论证一致）。
  - B：`get_command` 前置一次无锁预查（未知 cid 直接审计 + return，不建锁），
    命中后再进锁做正式校验；同时给 `_result_locks` 设容量上限（如 10k，超限拒收并审计）。
- **验收**：A 用 1000 个不同用户名各失败 2 次，断言字典尺寸收敛；B 用未知 cid 洪泛
  断言锁表不增长、审计被既有 `_audit_throttled` 限速。

### S6 摄入路径无背压：重连风暴是真实的突发场景

- **位置**：`services/mqtt_bridge.py:187-204`（`_dispatch` → `call_soon_threadsafe`
  → 每帧一个 task，无并发上限）。
- **现象**：服务端重连后 Broker 会**重放全部 retained status**（QoS1 的 status 还会
  加上离线期积压）。500 台主机的重放瞬间产生 500 个并发 task，每个串 4–6 条 SQL
  （`set_online` + `finish_updates_reaching` + `flush_queued_upgrades` +
  `_maybe_push_upgrade`，其中 `get_agent_latest` 在 `mqtt_bridge.py:413` 与 `:467`
  被读两次）。库慢一点，task 队列和连接池一起堆积。
- **建议**（三步，成本递增）：
  1. `agent_latest` 在事件循环内加 10–30s 的 TTL 缓存（KV 表本就是低频写），status
     风暴期读一次库，同时消掉 S6 里顺带提到的双读；
  2. `_spawn` 前过一道 `asyncio.Semaphore(N)`（N 取 16–32），背压自然传导到
     paho 的 out-queue / socket 缓冲；
  3. （可选）`_on_connect` 里记录「重连时刻」，对风暴窗口内的 status 合并去重
     （同 host 30s 内只处理最后一帧）。
- **验收**：单测模拟 500 条 retained status 重放，断言 task 峰值并发 ≤ N、全部处理完成、
  总耗时可控；`test_bridge.py` 既有语义用例全绿。

### S7 SQLite PRAGMA 只作用在 setup 那一条连接

- **位置**：`store.py:52-55`。
- **根因**：`journal_mode=WAL` 是库级持久属性（✓ 全连接生效）；但 `synchronous=NORMAL`
  是**每连接**属性——连接池里后续新建的连接全部回落默认 `FULL`（WAL 下每次 commit 多一次
  fsync）。`busy_timeout=5000` 同样不继承，实际靠 Python sqlite3 驱动默认 `timeout=5.0`
  兜底，属于「碰巧没坏」。
- **建议**：把每连接 PRAGMA 挂到 engine 的 connect 事件上：

  ```python
  from sqlalchemy import event
  @event.listens_for(self.engine.sync_engine, "connect")
  def _set_pragma(dbapi_conn, _):
      cur = dbapi_conn.cursor()
      cur.execute("PRAGMA synchronous=NORMAL")
      cur.execute("PRAGMA busy_timeout=5000")
      cur.close()
  ```

  收口在 `Store.__init__` 的 sqlite 分支，不新增方言分叉点。
- **验收**：新开连接查询 `PRAGMA synchronous` 断言为 1(NORMAL)；写入吞吐对比（可选）。

---

## 3. P3：顺手做 / 备选

| # | 位置 | 问题与建议 |
|---|---|---|
| S8 | `main.py:124` | 生产入口向 uvicorn 传了 `log_level`（会 `logging.getLogger("uvicorn.*").setLevel`），与 AGENTS.md A7「不传 log_level」约定相悖。当前靠 `setup_logging` 的 NOTSET 复位兜住，建议删掉该参数，与 A7 完全对齐 |
| S9 | `store.py:423` | `list_commands` 仅按 `created_at desc` 排序，同秒批量行翻页会漂移，补 `, commands.c.id.desc()` 次序键 |
| S10 | `tables.py:149` / `containers.py:13` / `mqtt_bridge.py:51` | `ONLINE_GRACE=180` 三处重复定义（桥接侧还叫 `OFFLINE_GRACE`），收敛到 `tables.py` 一处导出 |
| S11 | `store.py:148` | `upsert_container` 生产链路已无调用方（仅测试用），要么标注 `"""测试与夹具用：生产上线走 set_online"""`，要么迁到 tests 夹具里 |
| S12 | `controllers/deps.py` | 鉴权是处理器内手写 `await current_user(request)`，导致 **body 校验先于 401**（未带 token 也能触发 422）。改为 `Depends(current_user)` 与 FastAPI 官方 skill 推荐一致，鉴权时机也提前 |
| S13 | `controllers/health.py` | `/api/health` 未鉴权即暴露在线数 / bridge 计数 / 版本号。内网可接受，至少在 deployment.md 标注「不要把该端点暴露到公网」，或加可选开关 |
| S14 | `store.py:519-526` | `append_result` 在 SQL 内做 base64 字符串拼接，5.6MB 上限下写放大 ~330MB/命令，可接受；若未来放宽上限，备选方案是「分块行表 + 读取时拼装」。**本轮不动**，登记备选 |
| S15 | `pyproject.toml` | dev 组只有 pytest/httpx/pytest-asyncio。接入 `ruff`（lint+format，零配置起步）与 `mypy`（先 `--ignore-missing-imports` 宽松跑通 store/services），挂进 Jenkinsfile 测试阶段；`store.py`/`mqtt_bridge.py` 方法签名目前零类型注解，按 `.zcode/skills/python-code-quality` 的规范渐进补齐 |

### 明确不做（防止过度设计）

- 不引入 Redis/外置缓存：会话与限流规模都在内存可承受范围，多实例部署尚未启用；
- 不重写 append_result 为分块表（S14 触发条件未到）；
- 不给心跳表上分区/时序库：2 天保留 + 分批删已够用；
- 不改数据库表名/列名（仓库约定）。

---

## 4. 实施顺序与工作量估算

| 批次 | 内容 | 预估 | 理由 |
|---|---|---|---|
| ① 快赢 | S2（帧字段规整）+ S3（to_thread）+ S8/S9/S10 | ~0.5 天 | 全是局部小改，测试存量直接覆盖 |
| ② 性能 | S1（窗口函数聚合）+ S7（PRAGMA 事件） | ~1 天 | S1 要补三库兼容性测试 |
| ③ 韧性 | S5（字典治理）+ S6（背压 + latest 缓存） | ~1 天 | 涉及桥接行为，需 E2E（`scripts/mqtt_e2e.py`）回归 |
| ④ 卫生 | S4（索引）+ S12（Depends）+ S15（ruff/mypy） | ~1 天 | S4 要扩展 `_ensure_schema`，单独评审 |

每批次独立提交（`perf:` / `fix:` / `chore:` 前缀，标注 S 编号），全量测试 + `mqtt_e2e.py`
通过后再进下一批。

## 5. 与新装 skills 的映射

本轮分析执行期已把 7 个市场头部 Python 工程 skills 装入 `.zcode/skills/`（见该目录
`INDEX.md`）。后续实施时的对应关系：

- **S1/S4** 写 SQL 改造 → `fastapi-patterns`（DB 访问分层与生产风险清单）；
- **S2/S3** 异步正确性 → `async-python-patterns`（事件循环阻塞模式）+ `blocking-io-guard`
  （把「阻塞 IO 运行时锚点测试」方法论映射到 `server/tests/`，为 S3 类问题建立回归锚）；
- **S15/S12** → `python-code-quality`（类型注解规范）+ `fastapi` 官方 skill（`Annotated[..., Depends()]` 惯例）；
- **测试补充** → `python-testing`（fixture/参数化）+ `webapp-testing`（Playwright 对六个
  业务页做端到端冒烟，覆盖前端轮询与命令下发链路）。

## 6. 验收口径

1. `pytest agent/tests server/tests` 全 passed（Broker 可达时），测试计数不降；
2. `scripts/mqtt_e2e.py` 语义冒烟通过（LWT / 离线队列 / 幂等水位）；
3. S1/S3/S6 各有一条「改造前后」的可量化对比记录（查询计数 / P99 延迟 / 并发峰值）；
4. 新增行为均有负路径用例（坏帧、洪泛、并发上传）。
