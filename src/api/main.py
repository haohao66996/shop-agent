# -*- coding: utf-8 -*-
"""FastAPI：上传(校验+预览+质量报告) / 对话任务 / SSE 事件流 / 图表文件
v2.2: 登录认证(Cookie+X-Token 双通道) / 经营概况 / 会话列表 / 历史检索 / 记忆检索 / 数据集管理"""
import hashlib
import io
import json
import secrets
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote

from openpyxl import Workbook

import pandas as pd
from fastapi import Body, Depends, FastAPI, File, Form, Header, HTTPException, Request, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from src import db, queue
from src.config import abs_path, settings
from src.sandbox_runner import product_trend

app = FastAPI(title="shop-agent", version="0.2.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:18080", "http://127.0.0.1:18080",
                   "http://localhost:8100", "http://127.0.0.1:8100"],
    allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

# 新版 Web 前端（方案B 静态页）——由本服务直接托管: /app/
_WEB_DIR = Path(__file__).resolve().parents[2] / "frontend" / "web"
if _WEB_DIR.exists():
    app.mount("/app", StaticFiles(directory=str(_WEB_DIR), html=True), name="web")

# Cookie 免头通道覆盖 SSE(EventSource) 与图表(<img>)——它们无法携带自定义头
_READY_EXEMPT = {"/api/password", "/api/me", "/api/logout"}


@app.on_event("startup")
def _startup() -> None:
    db.init_db()
    db.memory_backfill()
    _ensure_admin()
    _ensure_invite()


# ---------------- 认证（v2.2，评审 P0-1/P1-5 采纳） ----------------

def _hash_pw(pw: str, salt_hex: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt_hex), 200_000).hex()


def _ensure_admin() -> None:
    """首次启动自动创建 admin/admin@123（首登强制改密）"""
    if db.one("SELECT id FROM users WHERE username=?", ("admin",)):
        return
    salt = secrets.token_hex(16)
    db.execute("INSERT INTO users(username, pw_salt, pw_hash, role,"
               " must_change_password, merchant_id, created_at) VALUES(?,?,?,?,?,?,?)",
               ("admin", salt, _hash_pw("admin@123", salt), "admin", 1,
                "demo-merchant", db.now()))


def _ensure_invite() -> None:
    """注册邀请码：管理员可查看/轮换（登录页注册需凭码，防任意注册）"""
    if not db.one("SELECT value FROM app_meta WHERE key='invite_code'"):
        db.execute("INSERT INTO app_meta VALUES('invite_code', ?)",
                   (secrets.token_hex(4).upper(),))


_DEMO_USER = {"id": 0, "username": "demo", "merchant_id": "demo-merchant",
              "must_change_password": 0, "hidden_file_ids": "[]", "role": "admin"}


def _user_by_token(tok: str) -> dict | None:
    if not tok:
        return None
    row = db.one("SELECT t.expires_at, u.* FROM tokens t JOIN users u ON u.id=t.user_id"
                 " WHERE t.token=?", (tok,))
    if not row:
        return None
    if row["expires_at"] < db.now():
        db.execute("DELETE FROM tokens WHERE token=?", (tok,))
        return None
    return dict(row)


def _auth(token: str = "", request: Request | None = None,
          ready: bool = True) -> dict:
    """三通道鉴权：allow_no_auth / Cookie(sid) / api_token；返回用户 dict。
    ready=True 时未完成首登改密的账号返回 403。"""
    u: dict | None = None
    if settings.allow_no_auth:
        u = dict(_DEMO_USER)
    if u is None and request is not None:
        u = _user_by_token(request.cookies.get("sid", ""))
    if u is None and token and token == settings.api_token:
        u = dict(_DEMO_USER)
    if u is None:
        raise HTTPException(401, "未登录或会话已过期")
    if ready and u.get("must_change_password"):
        raise HTTPException(403, "PASSWORD_CHANGE_REQUIRED")
    return u


def _require_role(u: dict, roles: set[str]) -> dict:
    if u.get("role") not in roles:
        raise HTTPException(403, "无权限执行该操作")
    return u


def _task_or_404(task_id: str, merchant_id: str) -> dict:
    t = db.one("SELECT id, status, stage, error FROM tasks"
               " WHERE id=? AND merchant_id=?", (task_id, merchant_id))
    if not t:
        raise HTTPException(404, "任务不存在")
    return t


@app.middleware("http")
async def origin_guard(request: Request, call_next):
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        origin = request.headers.get("origin")
        if origin and origin not in {
            "http://localhost:18080", "http://127.0.0.1:18080",
            "http://localhost:8100", "http://127.0.0.1:8100",
        }:
            return Response("非法来源", status_code=403, media_type="text/plain")
    return await call_next(request)


def _require_file(f: Path) -> Path:
    if not f.exists():
        raise HTTPException(404, "文件不存在")
    return f


# ---------------- 上传 ----------------

def _parse_preview(df: pd.DataFrame) -> tuple[dict, dict]:
    cols = list(df.columns)
    date_col = next((c for c in cols if "日期" in str(c) or "date" in str(c).lower()
                     or "时间" in str(c)), None)
    date_range = "未识别"
    if date_col is not None:
        s = pd.to_datetime(df[date_col], errors="coerce")
        if s.notna().sum() > len(df) * 0.5:
            date_range = f"{s.min():%Y-%m-%d} ~ {s.max():%Y-%m-%d}"
    preview = {"columns": cols,
               "dtypes": {str(c): str(t) for c, t in df.dtypes.items()},
               "rows": int(len(df)), "date_range": date_range,
               "head": df.head(5).fillna("").to_dict("records")}
    quality = {
        "null_cells": int(df.isna().sum().sum()),
        "duplicate_rows": int(df.duplicated().sum()),
        "negative_values": int((df.select_dtypes("number") < 0).sum().sum()),
        "unparsed_dates": int(pd.to_datetime(df[date_col], errors="coerce").isna().sum())
        if date_col else 0,
    }
    return preview, quality


@app.post("/api/upload")
async def upload(request: Request, file: UploadFile = File(...),
                 merchant_id: str = Form("demo-merchant"), token: str = Form("")):
    u = _require_role(_auth(token, request), {"admin", "staff"})
    merchant_id = u["merchant_id"]
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in {".csv", ".xlsx"}:
        raise HTTPException(400, "仅支持 CSV / Excel")
    raw = await file.read()
    if len(raw) > settings.max_upload_mb * 1024 * 1024:
        raise HTTPException(400, f"文件超过 {settings.max_upload_mb}MB 限制")
    try:
        df = pd.read_csv(io.BytesIO(raw)) if suffix == ".csv" else pd.read_excel(io.BytesIO(raw))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"解析失败: {e}") from e
    if df.empty:
        raise HTTPException(400, "文件没有数据行")

    file_id = uuid.uuid4().hex[:12]
    dst = abs_path(settings.upload_dir) / merchant_id / f"{file_id}{suffix}"
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(raw)

    preview, quality = _parse_preview(df)
    db.execute("INSERT INTO files VALUES(?,?,?,?,?,?)",
               (file_id, merchant_id, str(dst), json.dumps(preview, ensure_ascii=False),
                json.dumps(quality), db.now()))
    return {"file_id": file_id, "preview": preview, "quality": quality}


# ---------------- 对话任务 ----------------

@app.post("/api/chat")
def chat(request: Request, question: str = Form(""),
         session_id: str = Form(""), file_id: str = Form(""),
         merchant_id: str = Form("demo-merchant"), token: str = Form(""),
         idempotency_key: str = Form("")):
    u = _require_role(_auth(token, request), {"admin", "staff"})
    merchant_id = u["merchant_id"]
    # P1-3: 缺参/空参统一显式 400（原来 Form(...) 缺失走 FastAPI 默认 422）
    if not question.strip():
        raise HTTPException(400, "问题不能为空")
    if not file_id:
        raise HTTPException(400, "file_id 不能为空")
    if not db.one("SELECT id FROM files WHERE id=? AND merchant_id=?",
                  (file_id, merchant_id)):
        raise HTTPException(404, "file_id 不存在，请先上传数据")
    # P2-1 幂等: 显式 Idempotency-Key(请求头优先/表单) > 自动指纹(商户+文件+问题)
    idem = request.headers.get("Idempotency-Key") or idempotency_key
    key = idem or hashlib.sha1(
        f"{merchant_id}|{file_id}|{question.strip()}".encode()).hexdigest()[:16]
    prev = db.one("SELECT id, session_id, status FROM tasks WHERE idempotency_key=?"
                  " ORDER BY created_at DESC LIMIT 1", (key,))
    if prev:
        if prev["status"] in ("queued", "running"):      # 未完成 → 直接复用原任务
            return {"task_id": prev["id"], "session_id": prev["session_id"],
                    "deduplicated": True}
        if idem:                                          # 显式 key 且已完成 → 复用报告
            return {"task_id": prev["id"], "session_id": prev["session_id"],
                    "deduplicated": True}
    if not session_id:
        session_id = uuid.uuid4().hex[:12]
        db.execute("INSERT INTO chat_sessions(id, merchant_id, user_id, title, created_at)"
                   " VALUES(?,?,?,?,?)",
                   (session_id, merchant_id, u["id"], question[:30], db.now()))
    task_id = uuid.uuid4().hex[:12]
    db.execute("INSERT INTO tasks(id, merchant_id, session_id, file_id, question,"
               " created_at, idempotency_key) VALUES(?,?,?,?,?,?,?)",
               (task_id, merchant_id, session_id, file_id, question, db.now(), key))
    db.execute("INSERT INTO messages(session_id, role, content, task_id, created_at)"
               " VALUES(?,?,?,?,?)", (session_id, "user", question, task_id, db.now()))
    queue.submit(task_id)
    return {"task_id": task_id, "session_id": session_id}


@app.get("/api/tasks/{task_id}")
def task_status(request: Request, task_id: str, token: str = ""):
    u = _auth(token, request)
    return _task_or_404(task_id, u["merchant_id"])


@app.get("/api/tasks/{task_id}/report")
def task_report(request: Request, task_id: str, token: str = ""):
    u = _auth(token, request)
    _task_or_404(task_id, u["merchant_id"])
    r = db.one("SELECT report_json FROM reports r"
               " JOIN tasks t ON t.id=r.task_id"
               " WHERE r.task_id=? AND t.merchant_id=?", (task_id, u["merchant_id"]))
    if not r:
        raise HTTPException(404, "报告未生成")
    return json.loads(r["report_json"])


@app.get("/api/tasks/{task_id}/steps")
def task_steps(request: Request, task_id: str, token: str = ""):
    u = _auth(token, request)
    _task_or_404(task_id, u["merchant_id"])
    return db.query("SELECT s.step_id, s.status, s.attempts, s.code,"
                    " length(s.result_json) AS result_len"
                    " FROM task_steps s JOIN tasks t ON t.id=s.task_id"
                    " WHERE s.task_id=? AND t.merchant_id=?"
                    " ORDER BY s.step_id", (task_id, u["merchant_id"]))


@app.get("/api/tasks/{task_id}/events")
def task_events(request: Request, task_id: str, token: str = ""):
    """SSE 事件流：阶段变更 / 代码执行状态 / 图表 / 完成"""
    u = _auth(token, request)
    _task_or_404(task_id, u["merchant_id"])
    def gen():
        deadline = time.time() + 900  # 最长 15 分钟
        while time.time() < deadline:
            for ev in queue.drain(task_id):
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
                if ev.get("type") in {"final", "error"}:
                    return
            t = db.one("SELECT status,error FROM tasks"
                       " WHERE id=? AND merchant_id=?", (task_id, u["merchant_id"]))
            if t and t["status"] == "failed":
                msg = json.dumps({"type": "error",
                                  "message": (t["error"] or "")[:300]}, ensure_ascii=False)
                yield f"data: {msg}\n\n"
                return
            yield ": keep-alive\n\n"
            time.sleep(1)
    return StreamingResponse(gen(), media_type="text/event-stream")


@app.get("/api/tasks/{task_id}/charts/{name}")
def chart(request: Request, task_id: str, name: str, token: str = ""):
    u = _auth(token, request)
    _task_or_404(task_id, u["merchant_id"])
    if "/" in name or "\\" in name or not name.endswith(".png"):
        raise HTTPException(400, "非法文件名")
    p = abs_path(settings.task_dir) / task_id / "output" / "charts" / name
    return FileResponse(_require_file(p), media_type="image/png")


@app.get("/api/sessions/{session_id}/history")
def history(request: Request, session_id: str, token: str = ""):
    u = _auth(token, request)
    if not db.one("SELECT id FROM chat_sessions WHERE id=? AND merchant_id=?",
                  (session_id, u["merchant_id"])):
        raise HTTPException(404, "会话不存在")
    msgs = db.query("SELECT m.role, m.content, m.task_id, m.created_at"
                    " FROM messages m JOIN chat_sessions cs ON cs.id=m.session_id"
                    " WHERE m.session_id=? AND cs.merchant_id=?"
                    " ORDER BY m.id", (session_id, u["merchant_id"]))
    for m in msgs:  # 附上该任务的报告摘要
        if m["task_id"]:
            r = db.one("SELECT report_json FROM reports WHERE task_id=?", (m["task_id"],))
            m["report"] = json.loads(r["report_json"]) if r else None
    return {"session_id": session_id, "messages": msgs}


@app.get("/api/health")
def health():
    return {"status": "ok"}




# ================= v2.2 新增：认证接口 =================

@app.post("/api/login")
def login(response: Response, payload: dict = Body(...)):
    username = str(payload.get("username", "")).strip()
    password = str(payload.get("password", ""))
    u = db.one("SELECT * FROM users WHERE username=?", (username,))
    if u and u.get("disabled"):
        raise HTTPException(403, "账号已禁用")
    if not u or not secrets.compare_digest(_hash_pw(password, u["pw_salt"]), u["pw_hash"]):
        raise HTTPException(401, "用户名或密码错误")
    tok = secrets.token_hex(32)
    exp = (datetime.now() + timedelta(days=7)).isoformat(timespec="seconds")
    db.execute("INSERT INTO tokens(token, user_id, expires_at) VALUES(?,?,?)",
               (tok, u["id"], exp))
    response.set_cookie("sid", tok, max_age=7 * 86400, httponly=True,
                        samesite="lax", path="/")
    return {"username": u["username"], "merchant_id": u["merchant_id"],
            "must_change_password": bool(u["must_change_password"])}


@app.post("/api/logout")
def logout(request: Request, response: Response):
    tok = request.cookies.get("sid", "")
    if tok:
        db.execute("DELETE FROM tokens WHERE token=?", (tok,))
    response.delete_cookie("sid", path="/")
    return {"ok": True}


@app.get("/api/me")
def me(request: Request):
    return _auth(request=request, ready=False)


@app.post("/api/password")
def change_password(request: Request, payload: dict = Body(...)):
    u = _auth(request=request, ready=False)
    if u["id"] == 0:
        raise HTTPException(400, "demo 模式无账号体系，无法改密")
    row = db.one("SELECT * FROM users WHERE id=?", (u["id"],))
    if not secrets.compare_digest(_hash_pw(str(payload.get("old", "")), row["pw_salt"]),
                                  row["pw_hash"]):
        raise HTTPException(400, "旧密码错误")
    new = str(payload.get("new", ""))
    if len(new) < 8:
        raise HTTPException(400, "新密码至少 8 位")
    salt = secrets.token_hex(16)
    db.execute("UPDATE users SET pw_salt=?, pw_hash=?, must_change_password=0 WHERE id=?",
               (salt, _hash_pw(new, salt), u["id"]))
    # 改密后其它会话全部失效（保留当前会话）
    keep = request.cookies.get("sid", "")
    db.execute("DELETE FROM tokens WHERE user_id=? AND token != ?", (u["id"], keep))
    return {"ok": True}


@app.post("/api/register")
def register(response: Response, payload: dict = Body(...)):
    username = str(payload.get("username", "")).strip()
    password = str(payload.get("password", ""))
    invite = str(payload.get("invite", "")).strip().upper()
    if not (3 <= len(username) <= 20):
        raise HTTPException(400, "用户名需 3-20 个字符")
    if len(password) < 8:
        raise HTTPException(400, "密码至少 8 位")
    row = db.one("SELECT value FROM app_meta WHERE key='invite_code'")
    if not row or invite != row["value"].upper():
        raise HTTPException(403, "邀请码无效，请向管理员获取")
    if db.one("SELECT id FROM users WHERE username=?", (username,)):
        raise HTTPException(400, "用户名已存在")
    salt = secrets.token_hex(16)
    try:
        db.execute("INSERT INTO users(username, pw_salt, pw_hash, role,"
                   " must_change_password, merchant_id, created_at) VALUES(?,?,?,?,0,?,?)",
                   (username, salt, _hash_pw(password, salt), "staff",
                    "demo-merchant", db.now()))
    except Exception:  # noqa: BLE001
        raise HTTPException(400, "用户名已存在") from None
    tok = secrets.token_hex(32)
    exp = (datetime.now() + timedelta(days=7)).isoformat(timespec="seconds")
    db.execute("INSERT INTO tokens(token, user_id, expires_at) VALUES(?,?,?)",
               (tok, db.one("SELECT id FROM users WHERE username=?", (username,))["id"], exp))
    response.set_cookie("sid", tok, max_age=7 * 86400, httponly=True,
                        samesite="lax", path="/")
    return {"username": username, "merchant_id": "demo-merchant",
            "must_change_password": False, "role": "staff"}


@app.get("/api/invite")
def invite_get(request: Request):
    if _auth(request=request).get("role") != "admin":
        raise HTTPException(403, "仅管理员可查看邀请码")
    row = db.one("SELECT value FROM app_meta WHERE key='invite_code'")
    return {"invite": row["value"] if row else ""}


@app.post("/api/invite")
def invite_rotate(request: Request):
    if _auth(request=request).get("role") != "admin":
        raise HTTPException(403, "仅管理员可轮换邀请码")
    code = secrets.token_hex(4).upper()
    db.execute("INSERT OR REPLACE INTO app_meta VALUES('invite_code', ?)", (code,))
    return {"invite": code}


@app.get("/api/accounts")
def accounts_list(request: Request):
    if _auth(request=request).get("role") != "admin":
        raise HTTPException(403, "仅管理员可查看账号")
    return {"accounts": db.query(
        "SELECT id, username, role, disabled, created_at FROM users ORDER BY id")}


@app.post("/api/accounts/{uid}/disable")
def account_disable(uid: int, request: Request):
    u = _require_role(_auth(request=request), {"admin"})
    if uid == u["id"]:
        raise HTTPException(400, "不能禁用自己")
    target = db.one("SELECT id, role FROM users WHERE id=?", (uid,))
    if not target:
        raise HTTPException(404, "账号不存在")
    if target["role"] == "admin":
        active_admins = db.one(
            "SELECT COUNT(*) AS n FROM users WHERE role='admin' AND disabled=0"
        )
        if active_admins["n"] <= 1:
            raise HTTPException(400, "至少保留一个可用管理员")
    db.execute("UPDATE users SET disabled=1 WHERE id=?", (uid,))
    db.execute("DELETE FROM tokens WHERE user_id=?", (uid,))
    return {"ok": True}


@app.post("/api/accounts/{uid}/enable")
def account_enable(uid: int, request: Request):
    _require_role(_auth(request=request), {"admin"})
    if not db.one("SELECT id FROM users WHERE id=?", (uid,)):
        raise HTTPException(404, "账号不存在")
    db.execute("UPDATE users SET disabled=0 WHERE id=?", (uid,))
    return {"ok": True}


@app.delete("/api/accounts/{uid}")
def account_delete(uid: int, request: Request):
    u = _require_role(_auth(request=request), {"admin"})
    if uid == u["id"]:
        raise HTTPException(400, "不能删除自己")
    target = db.one("SELECT id, role FROM users WHERE id=?", (uid,))
    if not target:
        raise HTTPException(404, "账号不存在")
    if target["role"] == "admin":
        active_admins = db.one(
            "SELECT COUNT(*) AS n FROM users WHERE role='admin' AND disabled=0"
        )
        if active_admins["n"] <= 1:
            raise HTTPException(400, "至少保留一个可用管理员")
    db.execute("DELETE FROM users WHERE id=?", (uid,))
    db.execute("DELETE FROM tokens WHERE user_id=?", (uid,))
    return {"ok": True}


@app.post("/api/accounts/{uid}/reset-password")
def account_reset_password(uid: int, request: Request, payload: dict = Body(...)):
    _require_role(_auth(request=request), {"admin"})
    target = db.one("SELECT id FROM users WHERE id=?", (uid,))
    if not target:
        raise HTTPException(404, "账号不存在")
    new_password = str(payload.get("password", ""))
    if len(new_password) < 8:
        raise HTTPException(400, "新密码至少 8 位")
    salt = secrets.token_hex(16)
    db.execute(
        "UPDATE users SET pw_salt=?, pw_hash=?, must_change_password=1 WHERE id=?",
        (salt, _hash_pw(new_password, salt), uid),
    )
    db.execute("DELETE FROM tokens WHERE user_id=?", (uid,))
    return {"ok": True}


@app.patch("/api/accounts/{uid}/role")
def account_role(uid: int, request: Request, payload: dict = Body(...)):
    _require_role(_auth(request=request), {"admin"})
    target = db.one("SELECT id, role FROM users WHERE id=?", (uid,))
    if not target:
        raise HTTPException(404, "账号不存在")
    role = str(payload.get("role", ""))
    if role not in {"admin", "staff", "viewer"}:
        raise HTTPException(400, "角色只能是 admin / staff / viewer")
    if target["role"] == "admin" and role != "admin":
        active_admins = db.one(
            "SELECT COUNT(*) AS n FROM users WHERE role='admin' AND disabled=0"
        )
        if active_admins["n"] <= 1:
            raise HTTPException(400, "至少保留一个可用管理员")
    db.execute("UPDATE users SET role=? WHERE id=?", (role, uid))
    return {"ok": True, "role": role}


# ================= v2.2 新增：经营概况（按 file_id 缓存） =================

def _read_sales(path: str) -> "pd.DataFrame":
    p = Path(path)
    return pd.read_excel(p) if p.suffix.lower() == ".xlsx" else pd.read_csv(p)


def _overview_payload(path: str) -> dict:
    df = _read_sales(path)
    date_col = next((c for c in df.columns if "日期" in str(c)), None)
    amt_col = "金额" if "金额" in df.columns else df.select_dtypes("number").columns[0]
    df[date_col] = pd.to_datetime(df[date_col], errors="coerce")
    df = df.dropna(subset=[date_col])
    per = df[date_col].dt.to_period("M")
    last_p = df[date_col].max().to_period("M")
    if df[date_col].max() < last_p.end_time.normalize():
        last_p -= 1                      # 最新月不完整 → 回退
    cur, prev = df[per == last_p], df[per == (last_p - 1)]
    m_total, p_total = float(cur[amt_col].sum()), float(prev[amt_col].sum())
    byprod = cur.groupby("商品名称")[amt_col].sum().sort_values(ascending=False)
    cur_df = df[per == last_p]
    daily = cur_df.groupby(cur_df[date_col].dt.day)[amt_col].sum()
    cat = (cur.groupby("分类")[amt_col].sum().sort_values(ascending=False)
           if "分类" in df.columns else None)
    channel_col = next(
        (c for c in df.columns if "渠道" in str(c) or "channel" in str(c).lower()),
        None,
    )
    channel = (
        cur.groupby(channel_col)[amt_col].sum().sort_values(ascending=False)
        if channel_col else None
    )
    tr = product_trend(df, freq="W")
    sig = tr[tr["显著"]]
    days = [f"{last_p.month}/{int(d)}" for d in daily.index]
    return {
        "month": str(last_p), "total": round(m_total),
        "mom": (round((m_total - p_total) / p_total, 4) if p_total else None),
        "n_products": int(cur["商品名称"].nunique()),
        "top_name": str(byprod.index[0]), "top_value": round(float(byprod.iloc[0])),
        "top_share": round(float(byprod.iloc[0]) / m_total, 4) if m_total else 0,
        "risk": int((sig["趋势"] == "下降").sum()),
        "daily": {"days": days, "values": [round(v) for v in daily.values]},
        "category": ({"names": cat.index.tolist(), "values": [round(v) for v in cat.values]}
                     if cat is not None else None),
        "channel": ({"names": channel.index.tolist(),
                     "values": [round(v) for v in channel.values]}
                    if channel is not None else None),
        "top8": [[n, round(float(v))] for n, v in byprod.head(8).items()],
        "trend": sig.to_dict("records"),
    }


@app.get("/api/overview")
def overview(request: Request):
    u = _auth(request=request)
    fid = u.get("current_file_id") if u["id"] else None
    if not fid:
        row = db.one("SELECT id FROM files WHERE merchant_id=? ORDER BY created_at DESC",
                     (u["merchant_id"],))
        fid = row["id"] if row else None
    if not fid:
        raise HTTPException(404, "尚未上传数据")
    frow = db.one("SELECT * FROM files WHERE id=? AND merchant_id=?", (fid, u["merchant_id"]))
    if not frow:
        raise HTTPException(404, "数据集不存在")
    cached = db.one("SELECT json FROM overview_cache WHERE file_id=?", (fid,))
    if cached:
        return json.loads(cached["json"])
    payload = _overview_payload(frow["path"])
    payload["file_id"] = fid
    db.execute("INSERT OR REPLACE INTO overview_cache VALUES(?,?,?)",
               (fid, json.dumps(payload, ensure_ascii=False), db.now()))
    return payload


@app.get("/api/export/overview.xlsx")
def export_overview_xlsx(request: Request):
    u = _auth(request=request)
    fid = u.get("current_file_id")
    if not fid:
        row = db.one("SELECT id FROM files WHERE merchant_id=? ORDER BY created_at DESC",
                     (u["merchant_id"],))
        fid = row["id"] if row else None
    if not fid:
        raise HTTPException(404, "尚未上传数据")
    frow = db.one("SELECT * FROM files WHERE id=? AND merchant_id=?",
                  (fid, u["merchant_id"]))
    if not frow:
        raise HTTPException(404, "数据集不存在")
    payload = _overview_payload(frow["path"])

    wb = Workbook()
    ws = wb.active
    ws.title = "总览"
    ws.append(["指标", "数值"])
    ws.append(["月份", payload["month"]])
    ws.append(["总销售额", payload["total"]])
    ws.append(["环比", payload["mom"]])
    ws.append(["动销商品数", payload["n_products"]])
    ws.append(["销冠商品", payload["top_name"]])
    ws.append(["销冠销售额", payload["top_value"]])
    ws.append(["风险商品数", payload["risk"]])

    ws2 = wb.create_sheet("商品明细")
    ws2.append(["商品", "销售额"])
    for name, value in payload["top8"]:
        ws2.append([name, value])

    ws3 = wb.create_sheet("每日趋势")
    ws3.append(["日期", "销售额"])
    for day, value in zip(payload["daily"]["days"], payload["daily"]["values"]):
        ws3.append([day, value])

    if payload.get("channel"):
        ws4 = wb.create_sheet("渠道结构")
        ws4.append(["渠道", "销售额"])
        for name, value in zip(payload["channel"]["names"], payload["channel"]["values"]):
            ws4.append([name, value])

    output = io.BytesIO()
    wb.save(output)
    filename = quote(f"经营概览_{payload['month']}.xlsx")
    return Response(
        output.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"},
    )


# ================= v2.2 新增：数据集管理（上传中心） =================

def _file_rows(u: dict) -> list[dict]:
    hidden = json.loads(u.get("hidden_file_ids") or "[]")
    rows = db.query("SELECT * FROM files WHERE merchant_id=? ORDER BY created_at DESC",
                    (u["merchant_id"],))
    out = []
    for r in rows:
        if r["id"] in hidden:
            continue
        p = json.loads(r["preview"] or "{}")
        out.append({"file_id": r["id"], "name": Path(r["path"]).name,
                    "rows": p.get("rows"), "date_range": p.get("date_range"),
                    "quality": json.loads(r["quality"] or "{}"),
                    "is_current": (u.get("current_file_id") == r["id"]),
                    "created_at": r["created_at"]})
    return out


@app.get("/api/files")
def files_list(request: Request):
    u = _auth(request=request)
    return {"files": _file_rows(u)}


@app.post("/api/files/{fid}/current")
def files_current(request: Request, fid: str):
    u = _require_role(_auth(request=request), {"admin", "staff"})
    if u["id"] == 0:
        return {"ok": True, "note": "demo 模式不持久化"}
    if not db.one("SELECT id FROM files WHERE id=? AND merchant_id=?",
                  (fid, u["merchant_id"])):
        raise HTTPException(404, "数据集不存在")
    db.execute("UPDATE users SET current_file_id=? WHERE id=?", (fid, u["id"]))
    return {"ok": True}


@app.delete("/api/files/{fid}")
def files_delete(request: Request, fid: str):
    u = _require_role(_auth(request=request), {"admin", "staff"})
    if u["id"] == 0:
        raise HTTPException(400, "demo 模式不支持")
    if not db.one("SELECT id FROM files WHERE id=? AND merchant_id=?",
                  (fid, u["merchant_id"])):
        raise HTTPException(404, "数据集不存在")
    hidden = json.loads(u.get("hidden_file_ids") or "[]")
    if fid not in hidden:
        hidden.append(fid)
    db.execute("UPDATE users SET hidden_file_ids=?,"
               " current_file_id=CASE WHEN current_file_id=? THEN NULL"
               " ELSE current_file_id END WHERE id=?",
               (json.dumps(hidden), fid, u["id"]))
    return {"ok": True}


# ================= v2.2 新增：会话管理 =================

@app.get("/api/sessions")
def sessions_list(request: Request):
    u = _auth(request=request)
    rows = db.query(
        "SELECT cs.id, cs.title, cs.created_at,"
        " (SELECT COUNT(*) FROM messages m WHERE m.session_id=cs.id) AS n_msgs,"
        " (SELECT MAX(created_at) FROM messages m WHERE m.session_id=cs.id) AS last_at"
        " FROM chat_sessions cs WHERE cs.merchant_id=?"
        " ORDER BY cs.created_at DESC LIMIT 100", (u["merchant_id"],))
    return {"sessions": rows}


@app.post("/api/sessions")
def sessions_create(request: Request, payload: dict = Body(default={})):
    u = _require_role(_auth(request=request), {"admin", "staff"})
    sid = uuid.uuid4().hex[:12]
    title = str((payload or {}).get("title") or "新会话")[:30]
    db.execute("INSERT INTO chat_sessions(id, merchant_id, user_id, title, created_at)"
               " VALUES(?,?,?,?,?)", (sid, u["merchant_id"], u["id"], title, db.now()))
    return {"session_id": sid, "title": title}


@app.patch("/api/sessions/{sid}")
def sessions_rename(request: Request, sid: str, payload: dict = Body(...)):
    u = _require_role(_auth(request=request), {"admin", "staff"})
    if not db.one("SELECT id FROM chat_sessions WHERE id=? AND merchant_id=?",
                  (sid, u["merchant_id"])):
        raise HTTPException(404, "会话不存在")
    db.execute("UPDATE chat_sessions SET title=? WHERE id=?",
               (str(payload.get("title", "会话"))[:30], sid))
    return {"ok": True}


@app.delete("/api/sessions/{sid}")
def sessions_delete(request: Request, sid: str):
    u = _require_role(_auth(request=request), {"admin", "staff"})
    if not db.one("SELECT id FROM chat_sessions WHERE id=? AND merchant_id=?",
                  (sid, u["merchant_id"])):
        raise HTTPException(404, "会话不存在")
    db.execute("DELETE FROM messages WHERE session_id=?", (sid,))
    db.execute("DELETE FROM chat_sessions WHERE id=?", (sid,))
    return {"ok": True}


@app.get("/api/sessions/{sid}/messages")
def session_messages(request: Request, sid: str):
    u = _auth(request=request)
    if not db.one("SELECT id FROM chat_sessions WHERE id=? AND merchant_id=?",
                  (sid, u["merchant_id"])):
        raise HTTPException(404, "会话不存在")
    msgs = db.query("SELECT role, content, task_id, created_at FROM messages"
                    " WHERE session_id=? ORDER BY id", (sid,))
    for m in msgs:
        if m["task_id"]:
            r = db.one("SELECT report_json FROM reports WHERE task_id=?", (m["task_id"],))
            m["report"] = json.loads(r["report_json"]) if r else None
    return {"session_id": sid, "messages": msgs}


@app.get("/api/sessions/{sid}/export.md")
def export_session_markdown(request: Request, sid: str):
    u = _auth(request=request)
    sess = db.one("SELECT id, title, created_at FROM chat_sessions"
                  " WHERE id=? AND merchant_id=?", (sid, u["merchant_id"]))
    if not sess:
        raise HTTPException(404, "会话不存在")
    msgs = db.query("SELECT role, content, task_id, created_at FROM messages"
                    " WHERE session_id=? ORDER BY id", (sid,))
    lines = [f"# {sess['title'] or sess['id']}", "",
             f"- 会话 ID：`{sess['id']}`",
             f"- 创建时间：{sess['created_at']}", ""]
    for m in msgs:
        role = "用户" if m["role"] == "user" else "助手"
        lines.append(f"## {role} · {m['created_at']}")
        lines.append("")
        lines.append(m["content"])
        if m["task_id"]:
            lines.append("")
            lines.append(f"> 关联任务：`{m['task_id']}`")
        lines.append("")
    content = "\n".join(lines)
    filename = quote(f"{sess['title'] or sess['id']}.md")
    return Response(
        content,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"},
    )


# ================= v2.2 新增：历史报告检索 =================

@app.get("/api/history")
def history_search(request: Request, q: str = "", verified: str = "", page: int = 0):
    u = _auth(request=request)
    rows = db.query(
        "SELECT t.id, t.question, t.created_at, t.status, r.report_json"
        " FROM tasks t JOIN reports r ON r.task_id=t.id"
        " WHERE t.merchant_id=? AND t.status='done' ORDER BY t.created_at DESC LIMIT 500",
        (u["merchant_id"],))
    out = []
    for r in rows:
        rep = json.loads(r.pop("report_json"))
        item = {"task_id": r["id"], "question": r["question"], "created_at": r["created_at"],
                "verified": rep.get("verified", False),
                "findings_n": len(rep.get("findings", [])),
                "summary": rep.get("summary", "")}
        if q and q not in item["question"] and q not in item["summary"]:
            continue
        if verified == "ok" and not item["verified"]:
            continue
        if verified == "warn" and item["verified"]:
            continue
        out.append(item)
    total = len(out)
    return {"total": total, "page": page, "items": out[page * 20:(page + 1) * 20]}


# ================= v2.2 新增：记忆检索 =================

def _excerpt(text: str, q: str, width: int = 80) -> str:
    i = text.find(q)
    if i < 0:
        return text[:width] + ("…" if len(text) > width else "")
    s = max(0, i - width // 2)
    return ("…" if s else "") + text[s:i + width] + ("…" if i + width < len(text) else "")


@app.get("/api/memory/search")
def memory_search(request: Request, q: str = ""):
    u = _auth(request=request)
    all_facts = db.query("SELECT id, kind, content, pinned, created_at FROM memory"
                         " WHERE merchant_id=? ORDER BY pinned DESC, id DESC LIMIT 12",
                         (u["merchant_id"],))
    if not q.strip():
        return {"hits": [], "all_facts": all_facts}
    like = f"%{q}%"
    hits = db.query(
        "SELECT m.role, m.content, m.created_at, m.task_id, m.session_id, cs.title"
        " FROM messages m JOIN chat_sessions cs ON cs.id=m.session_id"
        " WHERE cs.merchant_id=? AND m.content LIKE ?"
        " ORDER BY m.id DESC LIMIT 20", (u["merchant_id"], like))
    # 结论记忆使用语义混合召回（Batch 3）
    for content in db.memory_recall(u["merchant_id"], q, k=10):
        hits.insert(0, {"role": "fact", "content": content,
                        "created_at": None, "task_id": None,
                        "session_id": None, "title": "店铺事实"})
    return {"hits": [{"role": h["role"], "excerpt": _excerpt(h["content"], q.strip()),
                      "session_id": h["session_id"], "title": h["title"],
                      "task_id": h["task_id"], "created_at": h["created_at"]} for h in hits],
            "all_facts": all_facts}


@app.patch("/api/memory/{fact_id}")
def memory_pin(fact_id: int, request: Request, payload: dict = Body(...)):
    u = _require_role(_auth(request=request), {"admin", "staff"})
    row = db.one("SELECT id FROM memory WHERE id=? AND merchant_id=?",
                 (fact_id, u["merchant_id"]))
    if not row:
        raise HTTPException(404, "事实卡不存在")
    pinned = 1 if payload.get("pinned") else 0
    db.memory_pin(fact_id, bool(pinned))
    return {"ok": True, "pinned": bool(pinned)}


@app.delete("/api/memory/{fact_id}")
def memory_delete(fact_id: int, request: Request):
    u = _require_role(_auth(request=request), {"admin", "staff"})
    row = db.one("SELECT id FROM memory WHERE id=? AND merchant_id=?",
                 (fact_id, u["merchant_id"]))
    if not row:
        raise HTTPException(404, "事实卡不存在")
    db.memory_delete(fact_id)
    return {"ok": True}


