# -*- coding: utf-8 -*-
"""grounding 数值归一化单测（P0-1 修复回归）。直接用项目解释器运行: python tests/test_grounding.py"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.agents.pipeline import AnalysisReport, Finding, _grounding, _norm_numbers


def test_norm() -> None:
    cases = {
        "135,015": [135015.0],
        "21,984元": [21984.0],
        "1,234,567.89": [1234567.89],
        "¥13,5015": [135015.0],
        "13.5万": [135000.0],
        "3.2亿": [320000000.0],
        "环比下降 23.5%": [23.5],
        "总额 2,048 与 1,000,000": [2048.0, 1000000.0],
    }
    for text, want in cases.items():
        got = _norm_numbers(text)
        assert got == want, f"norm({text!r}) = {got}, want {want}"


def test_grounding_comma() -> None:
    results = {1: {"status": "success", "result": {"总销售额": 135015, "销冠": "珍珠奶茶"}}}
    rep = AnalysisReport(
        summary="8月份总销售额为135,015元，珍珠奶茶是销售最好的商品。",
        findings=[Finding(title="8月份总销售额", metric="销售额", value="135,015",
                          evidence_step_id=1, interpretation="正常")],
        recommendations=[])
    assert _grounding(rep, results) == [], "千分位数值应通过校验"


def test_grounding_wan_unit() -> None:
    results = {1: {"status": "success", "result": {"总销售额": 135015}}}
    rep = AnalysisReport(
        summary="8月总销售额约13.5万元。",
        findings=[Finding(title="8月总销售额", metric="销售额", value="13.5万",
                          evidence_step_id=1, interpretation="正常")],
        recommendations=[])
    # 13.5万=135000 vs 135015，误差 0.011% < 1% → 应通过
    assert _grounding(rep, results) == []


def test_grounding_reject_fake() -> None:
    results = {1: {"status": "success", "result": {"总销售额": 135015}}}
    rep = AnalysisReport(
        summary="编造数值。",
        findings=[Finding(title="编造", metric="x", value="999,999",
                          evidence_step_id=1, interpretation="x")],
        recommendations=[])
    assert _grounding(rep, results), "编造数值应被拒绝"


def test_summary_count_check() -> None:
    results = {1: {"status": "success", "result": {"下降商品数": 12}}}
    f = Finding(title="t", metric="m", value="12", evidence_step_id=1, interpretation="i")
    rep = AnalysisReport(summary="共12种商品呈下降趋势。", findings=[f], recommendations=[])
    errs = _grounding(rep, results)
    assert any("数量" in e for e in errs), "summary 数量与 findings 不一致应报错"


if __name__ == "__main__":
    test_norm()
    test_grounding_comma()
    test_grounding_wan_unit()
    test_grounding_reject_fake()
    test_summary_count_check()
    print("GROUNDING_TESTS_OK (8 组归一化 + 4 组校验用例全过)")
