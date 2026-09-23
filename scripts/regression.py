# -*- coding: utf-8 -*-
"""黄金问题回归（P1-1: 量化任务成功率 + 趋势方案 §11.2 步骤级断言）。
用法: python scripts/regression.py [次数]   （默认 1 次）
每个用例: 上传演示数据 → 提问 → 轮询 → 校验 verified=true 且报告包含期望关键词；
趋势用例额外做步骤级断言（代码含 product_trend、无单月过滤）。
"""
import json
import re
import sys
import time
from pathlib import Path

import requests

BASE = "http://127.0.0.1:8100"
CSV = Path(__file__).resolve().parent.parent / "data/uploads/demo-merchant/demo_sales_2026Jun-Aug.csv"

# 步骤级断言的单月过滤特征（与 pipeline._trend_code_issues 口径一致）
SINGLE_MONTH_RE = re.compile(
    r"['\"]\d{4}-\d{2}['\"]|dt\.month\s*(==|>=|<=)|to_period\(\s*['\"]M['\"]\s*\)\s*(==|>=|<=)")

GOLDEN = [
    # (问题, 报告必须包含的关键词, 步骤级断言 dict 或 None)
    ("这个月卖得怎么样？卖得最好的是什么？",
     ["分析月份", "135015", "珍珠奶茶"], None),
    ("有没有卖得越来越差、该少进货的商品？",
     ["芋泥波波"], {"must_contain": ["product_trend"], "must_not_match": [SINGLE_MONTH_RE]}),
]


def _check_steps(tid: str, spec: dict) -> str | None:
    steps = requests.get(f"{BASE}/api/tasks/{tid}/steps", timeout=10).json()
    code = " ".join(s.get("code") or "" for s in steps)
    for kw in spec.get("must_contain", []):
        if kw not in code:
            return f"步骤代码缺少 {kw}"
    for pat in spec.get("must_not_match", []):
        if pat.search(code):
            return f"步骤代码含单月过滤特征: {pat.pattern}"
    return None


def run_once(question: str, expect: list[str], step_spec: dict | None) -> tuple[bool, str]:
    fid = requests.post(f"{BASE}/api/upload", files={"file": open(CSV, "rb")}).json()["file_id"]
    tid = requests.post(f"{BASE}/api/chat", data={"question": question, "file_id": fid}).json()["task_id"]
    print(f"  task={tid} ...", flush=True)
    t0 = time.time()
    while time.time() - t0 < 420:
        st = requests.get(f"{BASE}/api/tasks/{tid}").json()
        if st["status"] in ("done", "failed"):
            break
        time.sleep(10)
    if st["status"] != "done":
        return False, f"status={st['status']} err={st.get('error', '')[:120]}"
    rep = requests.get(f"{BASE}/api/tasks/{tid}/report").json()
    blob = json.dumps(rep, ensure_ascii=False).replace(",", "")
    missing = [e for e in expect if e not in blob]
    dur = round(time.time() - t0)
    if not rep.get("verified"):
        return False, f"verified=false ({dur}s)"
    if missing:
        return False, f"missing={missing} ({dur}s)"
    if step_spec and (msg := _check_steps(tid, step_spec)):
        return False, f"{msg} ({dur}s)"
    n = len(rep.get("findings", []))
    if step_spec and not (1 <= n <= 5):
        return False, f"findings={n} 超出 1~5 ({dur}s)"
    return True, f"OK ({dur}s, findings={n}, verified=true)"


def main() -> None:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    total = passed = 0
    for i in range(n):
        for q, expect, step_spec in GOLDEN:
            total += 1
            print(f"[{i + 1}/{n}] {q}")
            try:
                ok, msg = run_once(q, expect, step_spec)
            except Exception as e:  # noqa: BLE001
                ok, msg = False, f"exception: {e}"
            passed += ok
            print(f"    -> {'PASS' if ok else 'FAIL'}: {msg}", flush=True)
    print(f"\n成功率: {passed}/{total} = {passed / total:.0%}")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
