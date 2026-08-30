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
  last_metrics TEXT NOT NULL DEFAULT ''
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
  elapsed_ms INTEGER,
  out TEXT NOT NULL DEFAULT ''
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

MAX_OUT_CHARS = 4 * 1024 * 1024
_CMD_COLS = "id,pod,kind,argv,timeout,status,created_by,created_at,finished_at,rc,timed_out,elapsed_ms"


def _pwdf(salt, password):
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 120_000).hex()


class Store:
    def __init__(self, path="kk-server.db"):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        self._outbuf = {}  # cid -> 累积输出（避免逐帧重读整段 out 造成 O(n^2)）
        with self.lock:
            self.db.executescript(SCHEMA)
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.commit()

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

    def record_hb(self, pod, msg):
        now = int(time.time())
        m = msg.get("metrics") or {}
        raw = json.dumps(msg, ensure_ascii=False)
        with self.lock:  # 明细落库与容器状态更新同事务，避免半写
            self.db.execute("INSERT INTO heartbeats(pod,ts,cpu,mem_mb,metrics) VALUES(?,?,?,?,?)",
                            (pod, now, m.get("cpu"), m.get("mem_mb"), raw))
            self.db.execute(
                "UPDATE containers SET last_seen=?, hb_interval=?, last_metrics=? WHERE pod=?",
                (now, int(msg.get("interval") or 60), raw, pod))
            self.db.commit()

    def list_containers(self):
        return self._query("SELECT * FROM containers ORDER BY last_seen DESC")

    def get_container(self, pod):
        return self._query_one("SELECT * FROM containers WHERE pod=?", (pod,))

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
        cid = "c-" + secrets.token_hex(6)
        self._exec(
            "INSERT INTO commands(id,pod,kind,argv,timeout,status,created_by,created_at)"
            " VALUES(?,?,?,?,?,'pending',?,?)",
            (cid, pod, kind, json.dumps(argv), timeout, created_by, int(time.time())))
        return cid

    def get_command(self, cid):
        return self._query_one("SELECT * FROM commands WHERE id=?", (cid,))

    def list_commands(self, pod=None, limit=100):
        if pod:
            return self._query(
                "SELECT %s FROM commands WHERE pod=? ORDER BY created_at DESC LIMIT ?" % _CMD_COLS,
                (pod, limit))
        return self._query(
            "SELECT %s FROM commands ORDER BY created_at DESC LIMIT ?" % _CMD_COLS, (limit,))

    def pending_for(self, pod):
        return self._query(
            "SELECT * FROM commands WHERE pod=? AND status='pending' ORDER BY created_at", (pod,))

    def mark_sent(self, cid):
        self._exec("UPDATE commands SET status='sent', sent_at=? WHERE id=? AND status='pending'",
                   (int(time.time()), cid))

    def append_result(self, msg):
        cid = msg.get("id")
        if cid is None:
            return None
        try:
            data = base64.b64decode(msg.get("data_b64") or "", validate=False)
        except Exception:
            data = b""
        text = data.decode("utf-8", "replace")
        # 内存累积输出，避免逐帧重读整段 out 造成 O(n^2)
        buf = self._outbuf.get(cid)
        if buf is None:
            row = self.get_command(cid)
            buf = row["out"] if row else ""
        buf = (buf + text)[:MAX_OUT_CHARS]
        # 防御性：极端情况下丢弃最旧未完成任务的部分输出缓存，避免内存无限增长
        # （DB 仍保留已落库的部分输出，重新累积不会丢数据）
        if len(self._outbuf) > 1000:
            self._outbuf.pop(next(iter(self._outbuf)), None)
        self._outbuf[cid] = buf
        if msg.get("done"):
            self._outbuf.pop(cid, None)
            self._exec(
                "UPDATE commands SET status='done', out=?, rc=?, timed_out=?, elapsed_ms=?,"
                " finished_at=? WHERE id=?",
                (buf, msg.get("rc"), 1 if msg.get("timed_out") else 0,
                 msg.get("elapsed_ms"), int(time.time()), cid))
        else:
            self._exec("UPDATE commands SET status='running', out=? WHERE id=?", (buf, cid))
        return cid

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
            self.db.execute(
                "UPDATE commands SET status='lost' WHERE status IN ('sent','running')"
                " AND created_at < ?", (now - 3600,))
            self.db.commit()
        return {"hours_aggregated": hours}
