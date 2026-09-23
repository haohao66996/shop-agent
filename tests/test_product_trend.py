# -*- coding: utf-8 -*-
"""product_trend 显著性过滤单测（趋势方案 §11.1）。运行: $PY tests/test_product_trend.py"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from src.agents.pipeline import _extract_trend_top
from src.sandbox_runner import product_trend

DEMO = Path(__file__).resolve().parent.parent / "data/uploads/demo-merchant/demo_sales_2026Jun-Aug.csv"


def _demo_df() -> "pd.DataFrame":
    df = pd.read_csv(DEMO)
    df["日期"] = pd.to_datetime(df["日期"])
    return df


def test_schema():
    out = product_trend(_demo_df(), freq="W")
    assert list(out.columns) == ["商品名称", "每期变化额", "相对变化", "趋势", "显著"]


def test_product_trend_filters_noise():
    out = product_trend(_demo_df(), freq="W")
    sig = out[(out["趋势"] == "下降") & (out["显著"] == True)]  # noqa: E712
    assert "芋泥波波" in sig["商品名称"].tolist(), f"芋泥波波应显著下降, 实际: {out.to_dict('records')}"
    assert "手冲咖啡" not in sig["商品名称"].tolist()
    assert "珍珠奶茶" not in sig["商品名称"].tolist()
    assert "拿铁" not in sig["商品名称"].tolist()


def test_sorted_significant_first():
    out = product_trend(_demo_df(), freq="W")
    assert out.iloc[0]["商品名称"] == "芋泥波波"
    assert bool(out.iloc[0]["显著"]) is True


def test_extract_trend_top():
    fake = {"type": "dataframe",
            "columns": ["商品名称", "每期变化额", "相对变化", "趋势", "显著"], "rows": 2,
            "head": [
                {"商品名称": "芋泥波波", "每期变化额": -248.49, "相对变化": -0.35,
                 "趋势": "下降", "显著": True},
                {"商品名称": "珍珠奶茶", "每期变化额": -11.87, "相对变化": -0.02,
                 "趋势": "平稳", "显著": False}]}
    top = _extract_trend_top({1: {"status": "success", "result": fake}})
    assert [t["商品名称"] for t in top] == ["芋泥波波"]
    assert top[0]["evidence_step_id"] == 1
    assert top[0]["每期变化额"] == -248.49


def test_extract_empty():
    assert _extract_trend_top({1: {"status": "success",
                                   "result": {"type": "dataframe", "head": []}}}) == []


def test_trend_heavy_dataset():
    """趋势品占比高(3/6)时旧 median 阈值会自抬门槛卡掉真趋势（P25 修复的回归用例）"""
    import numpy as np
    rng = np.random.default_rng(7)
    spec = {"品A": -0.6, "品B": +0.5, "品C": -0.45, "品D": 0.0, "品E": 0.0, "品F": 0.0}
    rows = []
    for name, drift in spec.items():
        for d in range(98):
            q = max(0, int(rng.normal(200 * (1 + drift * d / 97), 8)))
            if q == 0:
                continue
            rows.append({"日期": (pd.Timestamp("2026-06-01") + pd.Timedelta(days=d)).strftime("%Y-%m-%d"),
                         "商品名称": name, "金额": q * 10})
    out = product_trend(pd.DataFrame(rows), freq="W")
    sig_d = out[(out["趋势"] == "下降") & (out["显著"])]["商品名称"].tolist()
    sig_u = out[(out["趋势"] == "上升") & (out["显著"])]["商品名称"].tolist()
    assert set(sig_d) == {"品A", "品C"}, f"显著下降应为品A/品C: {sig_d}"
    assert sig_u == ["品B"], f"显著上升应为品B: {sig_u}"


if __name__ == "__main__":
    test_schema()
    test_product_trend_filters_noise()
    test_sorted_significant_first()
    test_extract_trend_top()
    test_extract_empty()
    print("PRODUCT_TREND_TESTS_OK")
