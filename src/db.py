# -*- coding: utf-8 -*-
"""SQLite 存储层（v1 部署版；阶段一切 PostgreSQL 时仅改本模块）+ 租户记忆"""
import json
import sqlite3
import threading
from datetime import datetime

try:
    import sqlite_vec
except Exception:
    sqlite_vec = None

from src.config import abs_path, settings

_lock = threading.Lock()
_DB = abs_path(settings.database_path)

SCHEMA = """
CREATE TABLE IF NOT EXISTS chat_sessions(
  id TEXT PRIMARY KEY, merchant_id TEXT NOT NULL, title TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS files(
  id TEXT PRIMARY KEY, merchant_id TEXT NOT NULL, path TEXT NOT NULL,
  preview TEXT, quality TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS tasks(
  id TEXT PRIMARY KEY, merchant_id TEXT NOT NULL, session_id TEXT NOT NULL,
  file_id TEXT NOT NULL, question TEXT NOT NULL,
  status TEXT DEFAULT 'queued', stage TEXT, error TEXT, plan_json TEXT,
  created_at TEXT, finished_at TEXT);
CREATE TABLE IF NOT EXISTS task_steps(
  task_id TEXT NOT NULL, step_id INTEGER NOT NULL,
  status TEXT, code TEXT, result_json TEXT, charts TEXT, attempts INTEGER DEFAULT 0,
  PRIMARY KEY(task_id, step_id));
CREATE TABLE IF NOT EXISTS reports(
  task_id TEXT PRIMARY KEY, report_json TEXT NOT NULL, created_at TEXT);
CREATE TABLE IF NOT EXISTS messages(
  id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, role TEXT NOT NULL,
  content TEXT NOT NULL, task_id TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS memory(
  id INTEGER PRIMARY KEY AUTOINCREMENT, merchant_id TEXT NOT NULL, kind TEXT NOT NULL,
  content TEXT NOT NULL, created_at TEXT);
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE NOT NULL,
  pw_salt TEXT NOT NULL, pw_hash TEXT NOT NULL, role TEXT DEFAULT 'admin',
  must_change_password INTEGER DEFAULT 1, disabled INTEGER DEFAULT 0,
  merchant_id TEXT DEFAULT 'demo-merchant',
  current_file_id TEXT, hidden_file_ids TEXT DEFAULT '[]', created_at TEXT);
CREATE TABLE IF NOT EXISTS tokens(
  token TEXT PRIMARY KEY, user_id INTEGER NOT NULL, expires_at TEXT NOT NULL,
  created_at TEXT DEFAULT (datetime('now','localtime')));
CREATE TABLE IF NOT EXISTS overview_cache(
  file_id TEXT PRIMARY KEY, json TEXT NOT NULL, computed_at TEXT);
CREATE TABLE IF NOT EXISTS app_meta(
  key TEXT PRIMARY KEY, value TEXT);
"""

_VEC_OK = sqlite_vec is not None


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(_DB, timeout=30)
    conn.row_factory = sqlite3.Row
    global _VEC_OK
    if _VEC_OK:
        try:
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            conn.enable_load_extension(False)
        except Exception:
            _VEC_OK = False
            conn.enable_load_extension(False)
    return conn


def init_db() -> None:
    with _lock, _conn() as c:
        c.executescript(SCHEMA)
        # 轻量迁移: 老库补列（P2-1 幂等键）
        cols = [r[1] for r in c.execute("PRAGMA table_info(tasks)")]
        if "idempotency_key" not in cols:
            c.execute("ALTER TABLE tasks ADD COLUMN idempotency_key TEXT")
            c.execute("CREATE INDEX IF NOT EXISTS idx_tasks_idem ON tasks(idempotency_key)")
        # v2.2: 归属统一（评审 P1-4）+ 事实卡置顶 + 常用索引
        cols = [r[1] for r in c.execute("PRAGMA table_info(chat_sessions)")]
        if "user_id" not in cols:
            c.execute("ALTER TABLE chat_sessions ADD COLUMN user_id INTEGER DEFAULT 0")
        cols = [r[1] for r in c.execute("PRAGMA table_info(memory)")]
        if "pinned" not in cols:
            c.execute("ALTER TABLE memory ADD COLUMN pinned INTEGER DEFAULT 0")
        cols = [r[1] for r in c.execute("PRAGMA table_info(users)")]
        if "disabled" not in cols:
            c.execute("ALTER TABLE users ADD COLUMN disabled INTEGER DEFAULT 0")
        c.execute("CREATE INDEX IF NOT EXISTS idx_messages_sid ON messages(session_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_tokens_exp ON tokens(expires_at)")
        if _VEC_OK:
            c.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS memory_vec"
                " USING vec0(rowid INTEGER PRIMARY KEY, embedding FLOAT[512])"
            )


def execute(sql: str, params: tuple = ()) -> int:
    with _lock, _conn() as c:
        return c.execute(sql, params).lastrowid


def query(sql: str, params: tuple = ()) -> list[dict]:
    with _lock, _conn() as c:
        rows = c.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def one(sql: str, params: tuple = ()) -> dict | None:
    rows = query(sql, params)
    return rows[0] if rows else None


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ---------- 记忆（v1: SQLite 关键词召回；阶段二换向量库，按商户隔离） ----------

def memory_add(merchant_id: str, kind: str, content: str) -> None:
    from src.embedding import embedding

    with _lock, _conn() as c:
        cur = c.execute(
            "INSERT INTO memory(merchant_id, kind, content, created_at) VALUES(?,?,?,?)",
            (merchant_id, kind, content[:2000], now()),
        )
        memory_id = cur.lastrowid
        vectors = embedding.embed([content[:2000]])
        if _VEC_OK and vectors:
            try:
                c.execute(
                    "INSERT INTO memory_vec(rowid, embedding) VALUES (?,?)",
                    (memory_id, sqlite_vec.serialize_float32(vectors[0])),
                )
            except Exception:
                pass


def memory_pin(memory_id: int, pinned: bool) -> None:
    execute("UPDATE memory SET pinned=? WHERE id=?", (1 if pinned else 0, memory_id))


def memory_delete(memory_id: int) -> None:
    with _lock, _conn() as c:
        if _VEC_OK:
            c.execute("DELETE FROM memory_vec WHERE rowid=?", (memory_id,))
        c.execute("DELETE FROM memory WHERE id=?", (memory_id,))


def memory_backfill(batch_size: int = 100) -> int:
    if not _VEC_OK:
        return 0
    from src.embedding import embedding

    total = 0
    while True:
        rows = query(
            "SELECT id, content FROM memory"
            " WHERE id NOT IN (SELECT rowid FROM memory_vec)"
            " ORDER BY id LIMIT ?",
            (batch_size,),
        )
        if not rows:
            break
        vectors = embedding.embed([r["content"] for r in rows])
        if not vectors:
            break
        with _lock, _conn() as c:
            for row, vector in zip(rows, vectors):
                try:
                    c.execute(
                        "INSERT INTO memory_vec(rowid, embedding) VALUES (?,?)",
                        (row["id"], sqlite_vec.serialize_float32(vector)),
                    )
                except Exception:
                    continue
        total += len(rows)
    return total


def memory_recall(merchant_id: str, question: str, k: int = 5) -> list[str]:
    from src.embedding import embedding

    rows = query(
        "SELECT id, content FROM memory WHERE merchant_id=?"
        " ORDER BY pinned DESC, id DESC LIMIT 30",
        (merchant_id,))
    like_hits = [
        r for r in rows
        if any(seg in r["content"] for seg in question.split() if len(seg) >= 2)
    ]

    if not _VEC_OK:
        return ([r["content"] for r in like_hits[:k]] or
                [r["content"] for r in rows[:k]])

    vectors = embedding.embed([question])
    if not vectors:
        return ([r["content"] for r in like_hits[:k]] or
                [r["content"] for r in rows[:k]])

    try:
        vec_rows = query(
            "SELECT m.id, m.content"
            " FROM memory_vec v JOIN memory m ON m.id=v.rowid"
            " WHERE m.merchant_id=? AND v.embedding MATCH ? AND k = ?",
            (merchant_id, sqlite_vec.serialize_float32(vectors[0]), 10),
        )
    except Exception:
        return ([r["content"] for r in like_hits[:k]] or
                [r["content"] for r in rows[:k]])

    scores: dict[int, float] = {}
    contents: dict[int, str] = {r["id"]: r["content"] for r in rows}
    for rank, row in enumerate(like_hits):
        scores[row["id"]] = scores.get(row["id"], 0.0) + 1 / (60 + rank)
    for rank, row in enumerate(vec_rows):
        scores[row["id"]] = scores.get(row["id"], 0.0) + 1 / (60 + rank)
        contents[row["id"]] = row["content"]

    ranked = sorted(scores, key=lambda i: scores[i], reverse=True)
    return [contents[i] for i in ranked[:k]]
