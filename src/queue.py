# -*- coding: utf-8 -*-
"""进程内任务队列（v1 部署版）。
阶段一切换 Celery+Redis 时仅改 submit/_wrap，事件协议(Redis Streams)按实施方案升级。"""
import collections
import json
import logging
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor

import src.db as db
from src.agents.pipeline import run_pipeline

log = logging.getLogger("queue")
_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="analysis")
_lock = threading.Lock()
_events: dict[str, collections.deque] = {}


def emit(task_id: str, type_: str, **payload) -> None:
    with _lock:
        _events.setdefault(task_id, collections.deque(maxlen=1000)).append(
            {"type": type_, **payload})


def drain(task_id: str) -> list[dict]:
    with _lock:
        q = _events.get(task_id)
        if not q:
            return []
        out = list(q)
        q.clear()
        return out


def submit(task_id: str) -> None:
    db.execute("UPDATE tasks SET status='queued', stage='waiting' WHERE id=?", (task_id,))
    _pool.submit(_wrap, task_id)


def _wrap(task_id: str) -> None:
    try:
        db.execute("UPDATE tasks SET status='running', stage='planning' WHERE id=?", (task_id,))
        run_pipeline(task_id, emit)
        db.execute("UPDATE tasks SET status='done', stage='finished', finished_at=? WHERE id=?",
                   (db.now(), task_id))
        # v2.2: assistant 消息回填（会话回放/记忆检索需要）
        rep = db.one("SELECT report_json FROM reports WHERE task_id=?", (task_id,))
        trow = db.one("SELECT session_id FROM tasks WHERE id=?", (task_id,))
        if rep and trow:
            try:
                summary = json.loads(rep["report_json"]).get("summary", "")
                if summary:
                    db.execute("INSERT INTO messages(session_id, role, content, task_id,"
                               " created_at) VALUES(?,?,?,?,?)",
                               (trow["session_id"], "assistant", summary, task_id, db.now()))
            except Exception:  # noqa: BLE001 回填失败不影响任务结果
                log.exception("[%s] assistant 消息回填失败", task_id)
    except Exception as e:  # noqa: BLE001
        log.error("[%s] 任务失败:\n%s", task_id, traceback.format_exc())
        db.execute("UPDATE tasks SET status='failed', error=?, finished_at=? WHERE id=?",
                   (str(e)[:2000], db.now(), task_id))
        emit(task_id, "error", message=str(e)[:300])
