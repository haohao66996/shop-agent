# -*- coding: utf-8 -*-
"""趋势意图识别单测（趋势方案 §11.1）。运行: $PY tests/test_trend_intent.py"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.agents.pipeline import _is_trend_step, _trend_code_issues, is_trend_question


def test_trend_question_match():
    assert is_trend_question("有没有卖得越来越差、该少进货的商品？")
    assert is_trend_question("哪些商品在下滑？")
    assert is_trend_question("有没有商品销售趋势变差？")


def test_non_trend_question_not_match():
    assert not is_trend_question("这个月卖得怎么样？")
    assert not is_trend_question("卖得最好的是什么？")
    # 方案 §2 设计要点: 价格维度不触发商品趋势（其正文正则含"变化"会导致本用例失败，实施时已移除）
    assert not is_trend_question("价格变化对销售的影响是什么？")


def test_is_trend_step_reuses_trend_words():
    assert _is_trend_step({"description": "找出销量下滑的商品"})
    assert _is_trend_step({"description": "分析各商品销售趋势"})
    assert not _is_trend_step({"description": "统计8月整体销售额与销冠"})


def test_trend_code_issues():
    tc = {"latest_full_month": "2026-08"}
    ok = "t = product_trend(df, freq='W')\nresult = t\nsave_chart('trend_by_product.png')"
    assert _trend_code_issues(ok, tc) == []
    assert any("product_trend" in i for i in _trend_code_issues("df = load_data()\nresult = 1", tc))
    assert any("单月" in i for i in
               _trend_code_issues("df = load_data()\ndf = df[df['日期'] >= '2026-08-01']", tc))
    assert any("单月" in i for i in
               _trend_code_issues("df = load_data()\ndf = df[df['日期'].dt.month == 8]", tc))


if __name__ == "__main__":
    test_trend_question_match()
    test_non_trend_question_not_match()
    test_is_trend_step_reuses_trend_words()
    test_trend_code_issues()
    print("TREND_INTENT_TESTS_OK")
