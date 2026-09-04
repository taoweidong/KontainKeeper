"""SQLite 存储：容器、心跳、命令、审计、管理员会话。

单文件 WAL 模式 + 一把线程锁，单机管理规模（千级容器、秒级查询）足够；
所有写入都是毫秒级短事务，在 async 处理器中直接调用。
"""
import base64
import hashlib
import json
import secrets
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS containers(
  pod TEXT PRIMARY KEY,
  image TEXT NOT NULL DEFAULT '',
  agent_ver TEXT NOT NULL DEFAULT '',
  hb_interval INTEGER NOT NULL DEFAULT 60,
  first_seen INTEGER NOT NULL,
  last_seen INTEGER NOT NULL,
  last_metrics TEXT NOT NULL DEFAULT '',
  online INTEGER NOT NULL DEFAULT 0,
  status_ts INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS heartbeats(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  pod TEXT NOT NULL,
  ts INTEGER NOT NULL,
  cpu REAL,
  mem_mb REAL,
  metrics TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_hb_pod_ts ON heartbeats(pod, ts);
CREATE TABLE IF NOT EXISTS hourly(
  pod TEXT NOT NULL,
  hour INTEGER NOT NULL,
  samples INTEGER NOT NULL DEFAULT 0,
  cpu_avg REAL, cpu_max REAL, mem_avg REAL, mem_max REAL,
  last_metrics TEXT NOT NULL DEFAULT '',
  PRIMARY KEY(pod, hour)
);
CREATE TABLE IF NOT EXISTS commands(
  id TEXT PRIMARY KEY,
  pod TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'shell',
  argv TEXT NOT NULL DEFAULT '[]',
  timeout INTEGER NOT NULL DEFAULT 30,
  status TEXT NOT NULL DEFAULT 'pending',
  created_by TEXT NOT NULL DEFAULT '',
  created_at INTEGER NOT NULL,
  sent_at INTEGER,
  finished_at INTEGER,
  rc INTEGER,
  timed_out INTEGER NOT NULL DEFAULT 0,
  truncated INTEGER NOT NULL DEFAULT 0,
  elapsed_ms INTEGER,
  out_b64 TEXT NOT NULL DEFAULT '',
  out_chunks INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_cmd_pod ON commands(pod, created_at);
CREATE TABLE IF NOT EXISTS audit(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  actor TEXT NOT NULL,
  action TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT '',
  ts INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS admins(
  username TEXT PRIMARY KEY,
  salt TEXT NOT NULL,
  pw_hash TEXT NOT NULL,
  created INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions(
  token TEXT PRIMARY KEY,
  username TEXT NOT NULL,
  created INTEGER NOT NULL,
  expires INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS revoked_tokens(
  token TEXT PRIMARY KEY,
  ts INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT NOT NULL);
"""

_CMD_COLS = ("id,pod,kind,argv,timeout,status,created_by,created_at,sent_at,finished_at,"
             "rc,timed_out,truncated,elapsed_ms,out_chunks")
# 协议 v2 起命令输出以 base64 存 out_b64（二进制不再被 utf-8/replace 污染）。
# 旧库遗留的 out 列不再读取，只保留不破坏。
_NEW_COLS = {
    "containers": [("online", "INTEGER NOT NULL DEFAULT 0"),
                   ("status_ts", "INTEGER NOT NULL DEFAULT 0")],
    "commands": [("out_b64", "TEXT NOT NULL DEFAULT ''"),
                 ("out_chunks", "INTEGER NOT NULL DEFAULT 0"),
                 ("truncated", "INTEGER NOT NULL DEFAULT 0")],
}


def _pwdf(salt, password):
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 120_000).hex()


def _b64_tail(b64, nbytes=2048):
    """从拼接好的 base64 里截末段解码。

    base64 每 4 字符解 3 字节且组间互不依赖，所以按 4 的倍数从右侧截取仍是合法串。
    """
    if not b64:
        return ""
    chars = ((nbytes + 2) // 3) * 4
    tail = b64[-chars:] if len(b64) > chars else b64
    tail = tail[-((len(tail) // 4) * 4):] or tail
    try:
        return base64.b64decode(tail, validate=False).decode("utf-8", "replace")[-nbytes:]
    except Exception:
        return ""


class Store:
    def __init__(self, path="kk-server.db"):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock:
            self.db.executescript(SCHEMA)
            self.db.execute("PRAGMA journal_mode=WAL")
            self._migrate()
            self.db.commit()

    def _migrate(self):
        """SCHEMA 里的 CREATE TABLE IF NOT EXISTS 不会给已存在的表补列，需手工 ALTER。"""
        for table, cols in _NEW_COLS.items():
            have = {r["name"] for r in self.db.execute("PRAGMA table_info(%s)" % table)}
            for name, decl in cols:
                if name not in have:
                    self.db.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, name, decl))

    def _exec(self, sql, args=()):
        with self.lock:
            cur = self.db.execute(sql, args)
            self.db.commit()
            return cur

    def _query(self, sql, args=()):
        with self.lock:
            return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    def _query_one(self, sql, args=()):
        rows = self._query(sql, args)
        return rows[0] if rows else None

    # ---- 容器与心跳 ----
    def upsert_container(self, pod, image, agent_ver, interval):
        now = int(time.time())
        self._exec(
            "INSERT INTO containers(pod,image,agent_ver,hb_interval,first_seen,last_seen,last_metrics)"
            " VALUES(?,?,?,?,?,?, '')"
            " ON CONFLICT(pod) DO UPDATE SET image=excluded.image, agent_ver=excluded.agent_ver,"
            " hb_interval=excluded.hb_interval, last_seen=excluded.last_seen",
            (pod, image or "", agent_ver or "", int(interval or 60), now, now))

    def touch(self, pod):
        self._exec("UPDATE containers SET last_seen=? WHERE pod=?", (int(time.time()), pod))

    # ---- 在线状态（真相来自 Broker 的 retained status + LWT）----
    def set_online(self, pod, online, ts=None, image="", agent_ver=""):
        """上线来自 Agent 的 retained status，下线来自 LWT 或优雅 stop。

        hb_interval 不在这里写——record_hb 每帧都会带权威的 interval，避免两处互相覆盖。
        """
        now = int(ts or time.time())
        if not online:
            self._exec("UPDATE containers SET online=0, status_ts=? WHERE pod=?", (now, pod))
            return
        with self.lock:
            self.db.execute(
                "INSERT INTO containers(pod,image,agent_ver,hb_interval,first_seen,last_seen,"
                "last_metrics,online,status_ts) VALUES(?,?,?,?,?,?,?,1,?)"
                " ON CONFLICT(pod) DO UPDATE SET online=1, status_ts=excluded.status_ts,"
                " last_seen=excluded.last_seen,"
                " image=CASE WHEN excluded.image<>'' THEN excluded.image ELSE containers.image END,"
                " agent_ver=CASE WHEN excluded.agent_ver<>'' THEN excluded.agent_ver"
                " ELSE containers.agent_ver END",
                (pod, image or "", agent_ver or "", 60, now, now, "", now))
            self.db.commit()

    def mark_stale_offline(self, grace=180):
        """把「声称在线但久无 status 刷新」的主机判成离线。

        retained online 只有在 Broker 崩溃且来不及发 LWT 时才会失真，
        这条兜底由桥接的周期任务驱动，不再需要每连接一个超时定时器。
        """
        cur = self._exec(
            "UPDATE containers SET online=0"
            " WHERE online=1 AND ? - status_ts > MAX(3 * hb_interval, ?)",
            (int(time.time()), int(grace)))
        return max(0, cur.rowcount or 0)

    def online_count(self):
        return self._query_one("SELECT COUNT(*) AS n FROM containers WHERE online=1")["n"]

    def is_online(self, pod, grace=180):
        row = self._query_one("SELECT online,status_ts,hb_interval FROM containers WHERE pod=?",
                              (pod,))
        if not row or not row["online"]:
            return False
        # 宽限兜底：Broker 崩溃且没来得及发 LWT 时，retained 的 online 会一直挂着
        span = max(3 * (row["hb_interval"] or 60), grace)
        return int(time.time()) - (row["status_ts"] or 0) < span

    def record_hb(self, pod, msg):
        now = int(time.time())
        # 指标点用帧内 ts（缺失才回落服务器时间）：补发/重放时不全部挤到「刚刚」，
        # 曲线不会被自己制造的幽灵点污染。last_seen 仍用服务器时间——在线判定看的是
        # 「服务端何时收到」，与指标时刻是两回事。
        try:
            ts = int(msg.get("ts") or now)
        except (TypeError, ValueError):
            ts = now
        m = msg.get("metrics") or {}
        raw = json.dumps(msg, ensure_ascii=False)
        with self.lock:  # 明细落库与容器状态更新同事务，避免半写
            self.db.execute("INSERT INTO heartbeats(pod,ts,cpu,mem_mb,metrics) VALUES(?,?,?,?,?)",
                            (pod, ts, m.get("cpu"), m.get("mem_mb"), raw))
            self.db.execute(
                "UPDATE containers SET last_seen=?, hb_interval=?, last_metrics=? WHERE pod=?",
                (now, int(msg.get("interval") or 60), raw, pod))
            self.db.commit()

    def list_containers(self):
        return self._query("SELECT * FROM containers ORDER BY last_seen DESC")

    def get_container(self, pod):
        return self._query_one("SELECT * FROM containers WHERE pod=?", (pod,))

    def containers_exist(self, pods):
        """批量下发前的存在性校验：一次 IN 查询取代 N 次单查（500 台一次点击）。"""
        pods = list(pods or [])
        if not pods:
            return set()
        found = set()
        for i in range(0, len(pods), 400):     # 分片避开 SQLite 变量数上限
            shard = pods[i:i + 400]
            sql = "SELECT pod FROM containers WHERE pod IN (%s)" % ",".join("?" * len(shard))
            found |= {r["pod"] for r in self._query(sql, tuple(shard))}
        return found

    def metrics_series(self, pod, hours=24):
        since = int(time.time()) - hours * 3600
        if hours <= 24:
            rows = self._query(
                "SELECT ts, cpu, mem_mb FROM heartbeats WHERE pod=? AND ts>=? ORDER BY ts",
                (pod, since))
            return rows, "raw"
        since_h = since // 3600
        rows = self._query(
            "SELECT hour*3600 AS ts, cpu_avg AS cpu, mem_avg AS mem_mb FROM hourly"
            " WHERE pod=? AND hour>=? ORDER BY hour", (pod, since_h))
        return rows, "hourly"

    # ---- 命令 ----
    def create_command(self, pod, kind, argv, timeout, created_by):
        return self.create_commands_batch([pod], kind, argv, timeout, created_by)[0]

    def create_commands_batch(self, pods, kind, argv, timeout, created_by):
        """批量下发一次点击最多建 500 行：单事务 executemany，不再逐条 commit。"""
        now = int(time.time())
        ids = ["c-" + secrets.token_hex(6) for _ in pods]
        argv_json = json.dumps(argv, ensure_ascii=False)
        rows = [(cid, pod, kind, argv_json, timeout, created_by, now)
                for cid, pod in zip(ids, pods)]
        with self.lock:
            self.db.executemany(
                "INSERT INTO commands(id,pod,kind,argv,timeout,status,created_by,created_at)"
                " VALUES(?,?,?,?,?,'pending',?,?)", rows)
            self.db.commit()
        return ids

    def get_command(self, cid):
        return self._query_one("SELECT * FROM commands WHERE id=?", (cid,))

    def list_commands(self, pod=None, limit=100):
        sql = "SELECT %s, out_b64 FROM commands" % _CMD_COLS
        args = ()
        if pod:
            sql += " WHERE pod=?"
            args = (pod,)
        sql += " ORDER BY created_at DESC LIMIT ?"
        rows = self._query(sql, args + (limit,))
        for r in rows:
            r["out_tail"] = _b64_tail(r.pop("out_b64", ""))
        return rows

    def command_output(self, cid, as_text=True):
        """完整输出：整段解码只在单条查看时发生，不进列表。"""
        row = self._query_one("SELECT out_b64 FROM commands WHERE id=?", (cid,))
        if row is None:
            return None
        raw = base64.b64decode(row["out_b64"] or "", validate=False)
        if as_text:
            return raw.decode("utf-8", "replace")
        # 二进制输出原样再包一层 base64 回给调用方，不再被 utf-8/replace 污染
        return base64.b64encode(raw).decode("ascii")

    def pending_for(self, pod):
        """已废弃：离线命令排队由 Broker 的持久会话负责，服务端不再手工补发。"""
        return self._query(
            "SELECT * FROM commands WHERE pod=? AND status='pending' ORDER BY created_at", (pod,))

    def mark_sent(self, cid):
        """语义 = 已发布给 Broker（QoS1 已入会话队列），不代表 Agent 已收到。"""
        self._exec("UPDATE commands SET status='sent', sent_at=? WHERE id=? AND status='pending'",
                   (int(time.time()), cid))

    def append_result(self, msg, host=None):
        """协议 v2 结果帧：{id, seq, total, out_b64, done, rc, timed_out, elapsed_ms, truncated}。

        输出用 SQL 字符串拼接累加 base64：48KB 分块长度是 3 的整数倍，拼接结果仍是
        整段输出的合法 base64，省掉逐块解码重编码的 O(n²)，也不再有进程内存态缓存。
        """
        cid = msg.get("id")
        if cid is None:
            return None
        chunk = str(msg.get("out_b64") or "")
        now = int(time.time())
        with self.lock:
            if msg.get("done"):
                truncated = bool(msg.get("truncated")) or (
                    msg.get("rc") == -3)  # Agent 侧分块没能全部送达
                self.db.execute(
                    "UPDATE commands SET status='done', out_b64=out_b64||?,"
                    " out_chunks=out_chunks+1, rc=?, timed_out=?, elapsed_ms=?,"
                    " truncated=?, finished_at=? WHERE id=?",
                    (chunk, msg.get("rc"), 1 if msg.get("timed_out") else 0,
                     msg.get("elapsed_ms"), 1 if truncated else 0, now, cid))
            else:
                self.db.execute(
                    "UPDATE commands SET status='running', out_b64=out_b64||?,"
                    " out_chunks=out_chunks+1 WHERE id=?",
                    (chunk, cid))
            self.db.commit()
        return cid

    def sweep_command_timeouts(self, now=None, slack=30):
        """把迟迟没有终态的命令收敛掉，前端不再无限转圈。

        用 COALESCE(sent_at, created_at)：publish 失败停在 pending 的行 sent_at 为 NULL，
        只判 sent_at 会让它们永不被扫——正是评审 P1-6 里 lost 永不清理的同一个坑。
        """
        now = int(now or time.time())
        cur = self._exec(
            "UPDATE commands SET status='timeout', finished_at=?"
            " WHERE status IN ('pending','sent','running')"
            " AND COALESCE(sent_at, created_at) < ? - (timeout + ?)",
            (now, now, slack))
        return cur.rowcount if cur and cur.rowcount and cur.rowcount > 0 else 0

    # ---- 审计 ----
    def add_audit(self, actor, action, detail=None):
        self._exec("INSERT INTO audit(actor,action,detail,ts) VALUES(?,?,?,?)",
                   (actor, action, json.dumps(detail, ensure_ascii=False) if detail else "", int(time.time())))

    def list_audit(self, limit=200):
        return self._query("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,))

    # ---- 管理员与会话 ----
    def ensure_admin(self, username, password, force=False):
        """确保管理员账号存在，并返回是否发生了创建/更新。

        - 不存在 → 创建，返回 True
        - 已存在且 force → 强制更新口令，返回 True
        - 已存在且 env 口令与当前存储不一致 → 应用新口令（支持 KK_ADMIN_PASS 轮换），返回 True
        - 已存在且口令一致 → 不改动，返回 False
        """
        row = self._query_one("SELECT username, salt, pw_hash FROM admins WHERE username=?", (username,))
        if row is None:
            salt = secrets.token_hex(16)
            self._exec("INSERT INTO admins(username,salt,pw_hash,created) VALUES(?,?,?,?)",
                       (username, salt, _pwdf(salt, password), int(time.time())))
            return True
        if force or not secrets.compare_digest(row["pw_hash"], _pwdf(row["salt"], password)):
            salt = secrets.token_hex(16)
            self._exec("UPDATE admins SET salt=?, pw_hash=? WHERE username=?",
                       (salt, _pwdf(salt, password), username))
            return True
        return False

    def verify_admin(self, username, password):
        row = self._query_one("SELECT * FROM admins WHERE username=?", (username,))
        if not row:
            return False
        return secrets.compare_digest(row["pw_hash"], _pwdf(row["salt"], password))

    def create_session(self, username, ttl=12 * 3600):
        token = secrets.token_urlsafe(32)
        now = int(time.time())
        self._exec("INSERT INTO sessions(token,username,created,expires) VALUES(?,?,?,?)",
                   (token, username, now, now + ttl))
        return token

    def get_session(self, token):
        if not token:
            return None
        row = self._query_one("SELECT * FROM sessions WHERE token=?", (token,))
        if not row or row["expires"] < int(time.time()):
            return None
        return row["username"]

    def delete_session(self, token):
        self._exec("DELETE FROM sessions WHERE token=?", (token,))

    def default_admin_exists(self):
        return self._query_one("SELECT username FROM admins WHERE username='admin'") is not None

    # ---- Agent token 吊销 ----
    def revoke_token(self, token):
        self._exec("INSERT OR IGNORE INTO revoked_tokens(token,ts) VALUES(?,?)",
                   (token, int(time.time())))

    def restore_token(self, token):
        self._exec("DELETE FROM revoked_tokens WHERE token=?", (token,))

    def is_token_revoked(self, token):
        return self._query_one("SELECT token FROM revoked_tokens WHERE token=?",
                               (token,)) is not None

    def revoked_tokens(self):
        return [r["token"] for r in self._query("SELECT token FROM revoked_tokens ORDER BY ts")]

    # ---- 清理与聚合 ----
    def kv_get(self, k, default=None):
        row = self._query_one("SELECT v FROM kv WHERE k=?", (k,))
        return row["v"] if row else default

    def kv_set(self, k, v):
        self._exec("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))

    # ---- Agent 自更新：最新版本清单（含 sha256） ----
    def set_agent_latest(self, info):
        self.kv_set("agent_latest", json.dumps(info, ensure_ascii=False))

    def get_agent_latest(self):
        raw = self.kv_get("agent_latest")
        if not raw:
            return None
        try:
            return json.loads(raw)
        except ValueError:
            return None

    def _aggregate_hours(self, now):
        """把已结束的小时聚合进 hourly 表，推进水位线。"""
        last = int(self.kv_get("agg_hour", "0") or 0)
        now_hour = now // 3600
        first = self._query_one("SELECT MIN(ts) AS m FROM heartbeats")["m"]
        start = last
        if first is not None:
            start = max(last, int(first) // 3600 - 1)
        if start >= now_hour:
            return 0
        n = 0
        for h in range(start, now_hour):  # 不含当前未结束的小时
            rows = self._query(
                "SELECT pod, COUNT(*) AS samples, AVG(cpu) AS cpu_avg, MAX(cpu) AS cpu_max,"
                " AVG(mem_mb) AS mem_avg, MAX(mem_mb) AS mem_max FROM heartbeats"
                " WHERE ts>=? AND ts<? GROUP BY pod", (h * 3600, (h + 1) * 3600))
            for r in rows:
                lastm = self._query_one(
                    "SELECT metrics FROM heartbeats WHERE pod=? AND ts>=? AND ts<?"
                    " ORDER BY ts DESC LIMIT 1", (r["pod"], h * 3600, (h + 1) * 3600))
                self._exec(
                    "INSERT INTO hourly(pod,hour,samples,cpu_avg,cpu_max,mem_avg,mem_max,last_metrics)"
                    " VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(pod,hour) DO UPDATE SET"
                    " samples=excluded.samples, cpu_avg=excluded.cpu_avg, cpu_max=excluded.cpu_max,"
                    " mem_avg=excluded.mem_avg, mem_max=excluded.mem_max, last_metrics=excluded.last_metrics",
                    (r["pod"], h, r["samples"], r["cpu_avg"], r["cpu_max"], r["mem_avg"], r["mem_max"],
                     lastm["metrics"] if lastm else ""))
            n += 1
        self.kv_set("agg_hour", now_hour - 1)
        return n

    def cleanup(self, now=None, raw_days=2, cmd_days=30):
        now = int(now or time.time())
        hours = self._aggregate_hours(now)
        with self.lock:
            self.db.execute("DELETE FROM heartbeats WHERE ts < ?", (now - raw_days * 86400,))
            self.db.execute("DELETE FROM commands WHERE finished_at IS NOT NULL AND finished_at < ?",
                            (now - cmd_days * 86400,))
            self.db.execute("DELETE FROM sessions WHERE expires < ?", (now,))
            # 兜底：sweeper 漏掉的僵死命令盖成 lost，并补 finished_at——
            # 不补时间戳的 lost 行永远落在上面那条 DELETE 的窗口之外，正是 P1-6。
            self.db.execute(
                "UPDATE commands SET status='lost', finished_at=?"
                " WHERE status IN ('sent','running') AND created_at < ?",
                (now, now - 3600))
            self.db.commit()
        return {"hours_aggregated": hours}
