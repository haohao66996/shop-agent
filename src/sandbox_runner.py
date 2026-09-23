# -*- coding: utf-8 -*-
"""沙箱内执行器：Analyst 生成的代码在受限子进程中由此包装运行。
契约：cwd=任务工作目录；读 input/sales.csv；写 output/；结果赋给变量 result；
     图表用 save_chart('xx.png') 保存到 output/charts/。"""
import contextlib
import io
import json
import sys
import traceback
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np               # noqa: E402
import pandas as pd              # noqa: E402

plt.rcParams["font.sans-serif"] = ["WenQuanYi Zen Hei", "Noto Sans CJK SC", "DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False

IN, OUT = Path("input"), Path("output")
(OUT / "charts").mkdir(parents=True, exist_ok=True)


def save_chart(name: str, dpi: int = 130) -> str:
    if not name.endswith(".png"):
        name += ".png"
    p = OUT / "charts" / name
    plt.savefig(p, dpi=dpi, bbox_inches="tight")
    plt.close()
    return f"charts/{name}"


def load_data(path: str = "input/sales.csv") -> "pd.DataFrame":
    """加载销售数据，日期/时间列自动转 datetime（用户代码统一用本函数，避免 pandas 版本坑）"""
    df = pd.read_csv(path)
    for c in df.columns:
        cs = str(c).lower()
        if "日期" in str(c) or "时间" in str(c) or "date" in cs or "time" in cs:
            df[c] = pd.to_datetime(df[c], errors="coerce")
    return df


# ---- LLM 兼容 shim：恢复 pandas 2.0+ 移除的 DataFrame/Series.append ----
# LLM 生成的代码常用旧 API，缺这个 shim 会大量 AttributeError。
def _append_shim(self, other, ignore_index=False, *args, **kwargs):
    others = other if isinstance(other, (list, tuple)) else [other]
    frames = [self]
    for o in others:
        frames.append(o if isinstance(o, pd.DataFrame) else pd.DataFrame(o))
    return pd.concat(frames, ignore_index=ignore_index)


if not hasattr(pd.DataFrame, "append"):
    pd.DataFrame.append = _append_shim  # type: ignore[attr-defined]
if not hasattr(pd.Series, "append"):
    pd.Series.append = _append_shim     # type: ignore[attr-defined]


def _plain(v, depth: int = 0):
    if isinstance(v, np.generic):
        v = v.item()
    if isinstance(v, (int, float, str, bool)) or v is None:
        return v
    if isinstance(v, pd.DataFrame):
        head = v.head(30)
        return {"type": "dataframe", "columns": [str(c) for c in v.columns],
                "rows": int(len(v)), "head": head.to_dict("records")}
    if isinstance(v, pd.Series):
        return {"type": "list", "value": [_plain(x) for x in v.head(50).tolist()]}
    if isinstance(v, dict) and depth < 3:
        return {str(k): _plain(x, depth + 1) for k, x in v.items()}
    if isinstance(v, (list, tuple)) and depth < 3:
        return [_plain(x, depth + 1) for x in list(v)[:50]]
    return str(v)


# ---- 内置分析积木：让 LLM 组合现成函数，而不是手写复杂循环 ----
def product_trend(df=None, product_col: str = "商品名称",
                  amount_col: str = "金额", freq: str = "W") -> "pd.DataFrame":
    """各商品销售额趋势与显著性（趋势方案 §6：斜率+相对变化双阈值三分类）。
    返回 DataFrame: [product_col, 每期变化额, 相对变化, 趋势, 显著]
    排序: 显著在前，同组内按每期变化额升序（最下降排最前）。"""
    cols = [product_col, "每期变化额", "相对变化", "趋势", "显著"]
    if df is None:
        df = load_data()
    if "日期" not in df.columns:
        return pd.DataFrame(columns=cols)
    g = df.groupby([product_col, pd.Grouper(key="日期", freq=freq)])[amount_col].agg(["sum", "count"])
    rows = []
    for prod, ts in g.groupby(level=0):
        ts = ts.reset_index()
        med_days = float(ts["count"].median()) or 1.0
        ts = ts[ts["count"] >= max(1, 0.6 * med_days)]   # 剔除首尾不完整周期，防止假下降
        y = ts["sum"].astype(float).values
        if len(y) >= 2 and np.abs(y).sum() > 0:
            slope = float(np.polyfit(np.arange(len(y)), y, 1)[0])
            # 相对变化 = 后 1/3 期均值 vs 前 1/3 期均值——ramp 类信号的首尾比
            # 不随周期数变化（slope/mean 会因周期拉长而减半，26 周下芋泥波波仅 -0.048 漏检）
            k = max(1, len(y) // 3)
            head_m = float(np.mean(y[:k]))
            rel = (float(np.mean(y[-k:])) - head_m) / head_m if head_m else 0.0
            rows.append({product_col: prod, "每期变化额": round(slope, 2),
                         "相对变化": round(rel, 4)})
    out = pd.DataFrame(rows)
    if out.empty:
        return pd.DataFrame(columns=cols)
    # 阈值: 绝对=max(10, 2×|斜率|25分位)，相对=15%；两者同时满足才显著（方案 §6.2/§6.3）
    # 25 分位：趋势品占比高时 median 会被趋势品自身抬高而卡掉真趋势
    # （实测 3/6 品为趋势时 median×2=92.5 恰好滤掉第三个真趋势 -89.75）
    # rel=0.15：相对变化为首尾 1/3 均值比，其噪声底实测 ~0.10（98 天小数据），
    # 0.05 低于噪声底无区分力；真趋势实测最弱 0.26 → 取 0.15
    abs_thr = max(10.0, 2.0 * float(out["每期变化额"].abs().quantile(0.25)))
    rel_thr = 0.15

    def _cls(s: float, rel: float):
        if s < 0 and abs(s) >= abs_thr and abs(rel) >= rel_thr:
            return "下降", True
        if s > 0 and s >= abs_thr and rel >= rel_thr:
            return "上升", True
        return "平稳", False

    cls = [_cls(r["每期变化额"], r["相对变化"]) for _, r in out.iterrows()]
    out["趋势"] = [c[0] for c in cls]
    out["显著"] = [c[1] for c in cls]
    out = out.sort_values(["显著", "每期变化额"], ascending=[False, True]).reset_index(drop=True)
    return out


ns = {"pd": pd, "np": np, "plt": plt, "save_chart": save_chart,
      "load_data": load_data, "product_trend": product_trend}


def main() -> None:
    code = (IN / "code.py").read_text(encoding="utf-8")
    buf = io.StringIO()
    payload: dict
    try:
        with contextlib.redirect_stdout(buf):
            exec(compile(code, "user_code.py", "exec"), ns)  # noqa: S102
        payload = {"status": "success", "stdout": buf.getvalue()[-4000:],
                   "result": _plain(ns.get("result"))}
    except Exception:  # noqa: BLE001
        payload = {"status": "error", "stdout": buf.getvalue()[-4000:],
                   "error": traceback.format_exc()[-4000:]}
    (OUT / "result.json").write_text(
        json.dumps(payload, ensure_ascii=False, default=str), encoding="utf-8")
    sys.exit(0 if payload["status"] == "success" else 1)


if __name__ == "__main__":
    main()
