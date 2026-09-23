# -*- coding: utf-8 -*-
"""生成大批量演示销售数据（约 7000+ 行），注入已知规律供测试断言：
- 周末销量 +30%（全部商品）
- 冰品类 6-8 月销量上浮 50%
- '芋泥波波' 全期持续衰减（期末≈期初 25%）→ 趋势问题应识别为显著下降
- '芝士蛋糕' 全期持续上升（期末≈期初 140%）→ 上升趋势断言
- 其余商品为噪声级波动 → 不应进入显著清单
运行: $PY scripts/make_demo_data_large.py
"""
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

rng = np.random.default_rng(2026)

PRODUCTS = [
    ("珍珠奶茶", "奶茶", 12, 60), ("椰果奶茶", "奶茶", 11, 45),
    ("芋泥波波", "奶茶", 15, 32), ("黑糖鹿丸", "奶茶", 14, 28),
    ("柠檬水", "果饮", 8, 50), ("满杯百香果", "果饮", 13, 40),
    ("杨枝甘露", "果饮", 16, 30), ("多肉葡萄", "果饮", 15, 26),
    ("冰淇淋(抹茶)", "冰品", 9, 22), ("冰淇淋(芒果)", "冰品", 9, 22),
    ("冰镇酸梅汤", "冰品", 7, 24), ("手冲咖啡", "咖啡", 18, 18),
    ("拿铁", "咖啡", 20, 28), ("美式", "咖啡", 15, 20),
    ("蛋挞", "烘焙", 6, 35), ("芝士蛋糕", "烘焙", 22, 12),
    ("泡芙", "烘焙", 8, 18), ("鸡肉卷", "小食", 13, 16),
]

SLOTS = [("上午", 0.60, 0.32), ("下午", 0.85, 0.44), ("晚上", 0.70, 0.24)]  # (出现概率, 数量占比)

start, end = datetime(2026, 3, 1), datetime(2026, 8, 31)
DAYS = (end - start).days + 1            # 184 天
rows = []
for d in range(DAYS):
    date = start + timedelta(days=d)
    dow = date.weekday()
    weekend = 1.3 if dow >= 5 else 1.0
    summer = 1.5 if date.month in (6, 7, 8) else 1.0
    prog = d / (DAYS - 1)                # 0 → 1
    for name, cat, price, base in PRODUCTS:
        factor = weekend * (summer if cat == "冰品" else 1.0)
        if name == "芋泥波波":
            factor *= 1 - prog * 0.75    # 持续衰减: 期末 25%
        if name == "芝士蛋糕":
            factor *= 1 + prog * 0.40    # 持续上升: 期末 140%
        day_qty = max(0, int(rng.normal(base * factor, max(2.0, base * 0.10))))
        if day_qty <= 0:
            continue
        slots = [(s, share) for s, p, share in SLOTS if rng.random() < p]
        if not slots:
            slots = [("下午", 1.0)]
        total_share = sum(sh for _, sh in slots)
        for slot, share in slots:
            qty = max(1, int(round(day_qty * share / total_share
                                   + rng.normal(0, 1))))
            rows.append({"日期": date.strftime("%Y-%m-%d"),
                         "商品名称": name, "分类": cat, "时段": slot,
                         "单价": price, "数量": qty, "金额": price * qty})

df = pd.DataFrame(rows)
out = Path(__file__).resolve().parent.parent / "data" / "uploads" / "demo-merchant"
out.mkdir(parents=True, exist_ok=True)
path = out / "demo_sales_large_2026Mar-Aug.csv"
df.to_csv(path, index=False, encoding="utf-8-sig")
print(f"生成 {len(df)} 行（{df['日期'].nunique()} 天 × {df['商品名称'].nunique()} 商品）→ {path}")

# 打印测试断言用的关键事实
m8 = df[pd.to_datetime(df["日期"]).dt.month == 8]
print("2026-08 总销售额:", m8["金额"].sum())
print("2026-08 销冠:", m8.groupby("商品名称")["金额"].sum().idxmax())
