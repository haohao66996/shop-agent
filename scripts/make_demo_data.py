# -*- coding: utf-8 -*-
"""生成演示销售数据（注入已知规律，便于验证分析正确性）：
- 周末销量 +30%
- 冰品类 6-8 月销量上浮 50%
- '芋泥波波' 近 30 天持续衰减
"""
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

rng = np.random.default_rng(42)

PRODUCTS = [
    ("珍珠奶茶", "奶茶", 12, 55), ("椰果奶茶", "奶茶", 11, 40),
    ("芋泥波波", "奶茶", 15, 30), ("柠檬水", "果饮", 8, 45),
    ("满杯百香果", "果饮", 13, 35), ("杨枝甘露", "果饮", 16, 25),
    ("冰淇淋(抹茶)", "冰品", 9, 20), ("冰淇淋(芒果)", "冰品", 9, 20),
    ("手冲咖啡", "咖啡", 18, 15), ("拿铁", "咖啡", 20, 25),
    ("蛋挞", "烘焙", 6, 30), ("芝士蛋糕", "烘焙", 22, 10),
]

start, end = datetime(2026, 6, 1), datetime(2026, 8, 31)
rows = []
for d in range((end - start).days + 1):
    date = start + timedelta(days=d)
    dow = date.weekday()
    weekend = 1.3 if dow >= 5 else 1.0
    summer = 1.5 if date.month in (6, 7, 8) else 1.0
    decline = max(0.25, 1 - d / 92 * 1.1)  # 全期缓慢衰减趋势(仅对芋泥波波再加成)
    for name, cat, price, base in PRODUCTS:
        factor = weekend * (summer if cat == "冰品" else 1.0)
        if name == "芋泥波波":
            factor *= 1 - (d / 92) * 0.75   # 明显衰减: 期末只有期初 25%
        qty = max(0, int(rng.normal(base * factor * (0.85 if name == "芋泥波波" and False else 1), 3)))
        if name == "芋泥波波":
            qty = max(0, int(rng.normal(base * factor * (1 - d / 92 * 0.75), 2)))
        if qty == 0 and rng.random() < 0.9:
            continue
        rows.append({"日期": date.strftime("%Y-%m-%d"),
                     "商品名称": name, "分类": cat,
                     "时段": rng.choice(["上午", "下午", "晚上"], p=[0.3, 0.45, 0.25]),
                     "单价": price, "数量": qty, "金额": price * qty})

df = pd.DataFrame(rows)
out = Path(__file__).resolve().parent.parent / "data" / "uploads" / "demo-merchant"
out.mkdir(parents=True, exist_ok=True)
path = out / "demo_sales_2026Jun-Aug.csv"
df.to_csv(path, index=False, encoding="utf-8-sig")
print(f"生成 {len(df)} 行 → {path}")
