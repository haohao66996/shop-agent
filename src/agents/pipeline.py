# -*- coding: utf-8 -*-
"""Agent 编排：Planner → Analyst(沙箱执行,失败纠错重试≤3) → Reporter(grounding 校验)"""
import json
import logging
import re
from pathlib import Path

from pydantic import BaseModel, field_validator

from src import db, llm, sandbox
from src.config import abs_path, settings

log = logging.getLogger("pipeline")

DATA_NOTICE = ("数据文件: input/sales.csv (CSV, UTF-8)。字段: {columns}。"
               "共 {rows} 行，日期范围 {date_range}。前3行: {head}")


def _coerce_step_id(v) -> int:
    """模型可能输出 '步骤2'/'step2'/2 → 统一转 int（容错）"""
    if isinstance(v, int):
        return v
    m = re.search(r"\d+", str(v))
    return int(m.group()) if m else 0


def _coerce_str(v) -> str:
    return "" if v is None else str(v)


# ---------------- 时间语义（P0-2）----------------

def _time_context(file_info: dict) -> dict:
    """从数据日期范围解析时间上下文：最新完整月份 / 上一月"""
    dr = str(file_info.get("date_range") or "")
    dates = re.findall(r"(\d{4})-(\d{2})-(\d{2})", dr)
    if not dates:
        return {}
    y, m, d = (int(x) for x in dates[-1])      # 数据最大日期
    if d >= 28:                                 # 接近月末 → 该月视为完整
        latest = (y, m)
    elif m == 1:
        latest = (y - 1, 12)
    else:
        latest = (y, m - 1)
    py, pm = (latest[0] - 1, 12) if latest[1] == 1 else (latest[0], latest[1] - 1)
    return {"coverage": dr,
            "latest_full_month": f"{latest[0]}-{latest[1]:02d}",
            "prev_month": f"{py}-{pm:02d}"}


def _time_rule(time_ctx: dict, trend_mode: bool = False) -> str:
    """注入三类 Agent 提示词的时间语义规则（趋势模式用全量范围，见趋势方案 §3）"""
    if not time_ctx:
        return ""
    if trend_mode:
        return (f"\n时间语义(必须严格遵守): 数据覆盖 {time_ctx['coverage']}。"
                "趋势/变化类问题必须使用全量日期范围，禁止只分析最新月份，禁止按单月过滤。")
    return (f"\n时间语义(必须严格遵守): 数据覆盖 {time_ctx['coverage']}；"
            f"最新完整月份为 {time_ctx['latest_full_month']}。"
            f"用户说『这个月/本月/当月』一律指 {time_ctx['latest_full_month']}，"
            f"『上个月/上月』指 {time_ctx['prev_month']}。"
            f"涉及月份的分析必须在结论中写明确切年月。")


# ---------------- 趋势问题确定性链路（趋势方案 §2/§4/§5，含评审修正 P-1/P-3）----------------

# 注意: 不含孤立的"变化"——否则"价格变化对销售的影响"会误判（方案 §11.1 反例单测要求）
TREND_WORDS = re.compile(r"越来越|趋势|下降|上升|下滑|衰退|卖得差|减少进货|该少进货")
PRODUCT_WORDS = re.compile(r"商品|销售|销量|卖|进货|品类|产品|单品")


def is_trend_question(question: str) -> bool:
    """双条件命中：趋势词 ∧ 商品/销售维度词"""
    return bool(TREND_WORDS.search(question) and PRODUCT_WORDS.search(question))


TREND_STEP = {
    "step_id": 1,
    "description": ("调用 product_trend(df, freq='W') 计算各商品每周销售额斜率与显著性，"
                    "把完整结果直接赋给 result（禁止过滤），"
                    "并调用 save_chart('trend_by_product.png') 保存趋势图"),
    "expected_output": "DataFrame[商品名称, 每期变化额, 相对变化, 趋势, 显著]",
}


def _is_trend_step(step: dict) -> bool:
    """复用 TREND_WORDS 判定 Planner 步骤是否趋势类（评审修正 P-3：不另维护词表）"""
    return bool(TREND_WORDS.search(str(step.get("description", ""))))


def _trend_code_issues(code: str, time_ctx: dict) -> list[str]:
    """趋势步骤代码硬性校验（评审修正 P-1：在 Analyst 阶段拦截并重写，
    而非等 Reporter 阶段——step_code 此时已无法通过重试 Reporter 修复）"""
    issues: list[str] = []
    if "product_trend" not in code:
        issues.append("未调用 product_trend(df, freq='W')")
    lm = (time_ctx or {}).get("latest_full_month", "")
    if lm and lm in code:
        issues.append(f"出现单月字面量 {lm}（疑似单月过滤）")
    if re.search(r"dt\.month\s*(==|>=|<=|<|>)", code):
        issues.append("使用 dt.month 做单月过滤")
    if re.search(r"to_period\(\s*['\"]M['\"]\s*\)\s*(==|>=|<=|<|>)", code):
        issues.append("使用 to_period('M') 做单月过滤")
    return issues


# ---------------- 数值归一化（P0-1）----------------

_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")


def _norm_numbers(text: str) -> list[float]:
    """提取数值前先归一化：去千分位逗号、货币符号，识别 万/亿 单位后缀"""
    t = re.sub(r"(?<=[0-9])[,，](?=[0-9])", "", str(text))    # 135,015 → 135015
    t = re.sub(r"[¥￥$€£元圆]", " ", t)
    out: list[float] = []
    for m in _NUM_RE.finditer(t):
        v = float(m.group())
        tail = t[m.end():m.end() + 2].lstrip()
        if tail.startswith("亿"):
            v *= 1e8
        elif tail.startswith("万"):
            v *= 1e4
        out.append(v)
    return out


# ---------------- 常见错误自动修复提示（P1-1）----------------

def _repair_hint(err: str) -> str:
    """按异常模式给出针对性修复提示，附加到 Analyst 重试反馈"""
    e = err or ""
    hints: list[str] = []
    if "KeyError" in e:
        hints.append("KeyError: 不要用字符串日期(如 '2026-06-01')当列名或索引键取数；"
                     "按日期过滤用布尔条件 df[(df['日期']>='2026-06-01')&...]，"
                     "并检查列名与数据字段完全一致")
    if "no longer supported" in e or "Invalid frequency" in e:
        hints.append("频率别名错误: 月='ME'，年='YE'，'M'/'Y' 已移除")
    if "If using all scalar values" in e:
        hints.append("构造 DataFrame 不要传全标量 dict，请用记录列表 pd.DataFrame([...])")
    if "out-of-bounds" in e:
        hints.append("对空结果取了 iloc[0]：先判断 len(df)>0 再取")
    if "AttributeError" in e and "append" in e:
        hints.append("df.append 已移除：用 pd.concat([df1, df2])")
    if "to_period" in e or "period" in e.lower():   # 趋势方案 §10：第六类（实测高频翻车点）
        hints.append("周期转换错误: 优先使用 product_trend(df, freq='W')；"
                     "如必须手写，用 groupby+pd.Grouper 或 resample('W')，"
                     "不要用 to_period('M') 做单月过滤")
    if "AssertionError" in e:
        hints.append("禁止 assert/数据校验（提示词已禁）——直接分析，把结论放进 result 变量")
    return ("\n修复提示: " + "；".join(hints)) if hints else ""


class Finding(BaseModel):
    title: str = ""
    metric: str = ""
    value: str = ""            # 必须原样来自步骤执行结果
    evidence_step_id: int = 0  # 引用哪一步
    interpretation: str = ""

    # 容错: 模型可能输出 '步骤2'/'step2'/None → 统一归一化
    _step = field_validator("evidence_step_id", mode="before")(_coerce_step_id)
    _strs = field_validator("title", "metric", "value", "interpretation",
                            mode="before")(_coerce_str)


class Recommendation(BaseModel):
    action: str = ""
    reason: str = ""
    confidence: str = "medium"   # high / medium / low
    data_gaps: list[str] = []    # 缺什么数据导致置信度低

    _strs = field_validator("action", "reason", "confidence", mode="before")(_coerce_str)


class AnalysisReport(BaseModel):
    summary: str
    findings: list[Finding]
    recommendations: list[Recommendation]
    charts: list[str] = []
    caveats: list[str] = []
    verified: bool = True


# ---------------- Planner ----------------

def _plan(question: str, file_info: dict, notes: list[str],
          time_ctx: dict | None = None, trend_mode: bool = False,
          dialog: str = "") -> list[dict]:
    notice = DATA_NOTICE.format(**file_info)
    sys_p = ("你是小商户经营数据分析规划师。根据数据结构和用户问题制定 1-4 步分析计划。"
             "每一步都必须能由一段 pandas 代码完成。第一步通常是整体概览。"
             "涉及相对时间(这个月/上个月)的计划必须写明确切年月。"
             "只输出 JSON：{\"steps\":[{\"step_id\":1,\"description\":\"...\","
             "\"expected_output\":\"...\"}]}，不要输出其他内容。")
    dlg = (f"\n最近对话(用于理解指代，如'那第二名呢'):\n{dialog}\n" if dialog else "")
    user_p = (f"数据: {notice}\n历史分析结论(供参考): {notes}\n{dlg}"
              f"用户问题: {question}{_time_rule(time_ctx or {}, trend_mode)}")
    for attempt in range(3):
        out = llm.chat([{"role": "system", "content": sys_p},
                        {"role": "user", "content": user_p}], temperature=0.2, max_tokens=1200)
        data = llm.extract_json(out)
        if isinstance(data, dict) and data.get("steps"):
            if trend_mode:
                # 趋势方案 §4 + 实测修正: 趋势问题用单步确定性计划——
                # product_trend 一步即完整答案；保留模型自拟步骤只会引入
                # "初步检查"类垃圾步骤（实测其 assert 代码连挂 4 次拖垮任务）
                steps = [dict(TREND_STEP)]
            else:
                steps = data["steps"][:4]
            return [{"step_id": i + 1, "description": s.get("description", ""),
                     "expected_output": s.get("expected_output", "")}
                    for i, s in enumerate(steps)]
    raise RuntimeError("Planner 无法产出合法分析计划")


# ---------------- Analyst ----------------

_CODE_RE = re.compile(r"```(?:python)?\s*(.*?)```", re.S)


def _analyst_code(question: str, step: dict, file_info: dict,
                  prior: list[dict], last_err: str | None,
                  time_ctx: dict | None = None, is_trend_step: bool = False,
                  context: str = "") -> str:
    notice = DATA_NOTICE.format(**file_info)
    sys_p = (
        "你是数据分析工程师。写一段 Python 代码完成分析步骤。\n"
        "环境: pandas/matplotlib/numpy 已导入为 pd/plt/np；当前目录有 input/(只读)和 output/(可写)。\n"
        "版本: Python 3.11 + pandas 3.x。pandas 3.x 注意: resample/Grouper 的月/年频率别名用 'ME'/'YE'，"
        "'M'/'Y' 已移除；applymap 已移除(用 map)。\n"
        "代码开头必须用 load_data() 加载数据（日期列已自动转好，禁止自己 read_csv + to_datetime）:\n"
        "  df = load_data()\n"
        "内置工具函数（优先组合它们，不要手写复杂循环/自定义聚合逻辑）:\n"
        "  df = load_data()                 # 读 input/sales.csv，日期已转 datetime\n"
        "  product_trend(df)                # 各商品销售额趋势与显著性 "
        "DataFrame[商品名称,每期变化额,相对变化,趋势,显著]，显著下降排最前；df 不传则自动加载\n"
        "  save_chart('xx.png')             # 保存当前图\n"
        "规则:\n"
        "1. 不要写任何数据检查/校验函数/assert/raise——直接分析。\n"
        "2. 图表: 先画图，最后调用 save_chart('名字.png') 保存（文件名用英文）；"
        "不要重新定义/覆盖 save_chart/load_data/product_trend，不要用 plt.savefig 自己存\n"
        "3. 结果: 把结论赋给变量 result(dict)，值只能是 数字/字符串/DataFrame/列表，"
        "键名用英文，值用中文；构造 DataFrame 用列表/dict 数组，不要全标量\n"
        "4. 禁止: os/subprocess/socket/requests/open/eval/exec 等模块与函数；禁止读写 input/output 之外路径\n"
        "5. 任何输出只放进 result 变量返回；禁止 open/写任何文件(不要 to_csv/txt)；图表只能用 save_chart\n"
        "6. 禁止硬编码行数/日期等来自样本的数值\n"
        "6. 对 1~10 万行数据 35 秒内要能跑完\n"
        "7. 禁止用字符串日期当列名/索引键取数(如 df['2026-06-01'])；"
        "按日期过滤用布尔条件 df[(df['日期']>='2026-06-01')&...]；"
        "按月分组用 df['日期'].dt.to_period('M')\n"
        "8. 只输出一个 ```python 代码块，不要解释")
    user_p = (f"数据: {notice}\n用户问题: {question}\n"
              f"已完成步骤结果摘要: {json.dumps(prior, ensure_ascii=False)[:3000]}\n"
              f"当前步骤 {step['step_id']}: {step['description']}"
              f"(期望输出: {step['expected_output']})\n"
              f"{_time_rule(time_ctx or {}, is_trend_step)}")
    if context:
        user_p += f"\n上文口径(优先沿用，不要重新推断):\n{context[:500]}"
    if is_trend_step:   # 趋势方案 §5: 硬约束（评审修正 P-2：补图表要求）
        user_p += ("\n当前步骤为固定趋势分析步骤，硬性要求:\n"
                   "- 必须调用 product_trend(df, freq='W')，禁止手写趋势算法/回归\n"
                   "- 禁止按单月过滤、禁止只分析最新月份，代码中不得出现任何具体月份字面量\n"
                   "- 必须把 product_trend 的完整输出直接赋给 result（result = t），"
                   "禁止对它做过滤/子集/筛选（不要写 t[t['显著']==...] 之类）——"
                   "显著性筛选由系统在报告阶段完成\n"
                   "- 画趋势图后必须调用 save_chart('trend_by_product.png')")
    if last_err:
        user_p += f"\n上一次执行失败，请修正后再写:\n{last_err[:1500]}{_repair_hint(last_err)}"
    for _ in range(2):
        out = llm.chat([{"role": "system", "content": sys_p},
                        {"role": "user", "content": user_p}], temperature=0.0,
                       max_tokens=2500, prefer="analyst")
        m = _CODE_RE.search(out)
        if m:
            return m.group(1).strip()
    raise RuntimeError("Analyst 无法产出代码")


def _compact(res: dict) -> dict:
    return {"status": res.get("status"), "result": res.get("result")}


# ---------------- Reporter + grounding ----------------

def _report(question: str, file_info: dict, plan: list[dict],
            results: dict[int, dict], errors: list[str] | None = None,
            time_ctx: dict | None = None, trend_top: list[dict] | None = None) -> AnalysisReport:
    notice = DATA_NOTICE.format(**file_info)
    sys_p = ("你是小商户经营顾问。基于代码执行结果生成分析报告，给店主看得懂、用得上的结论。\n"
             "只输出 JSON：{\"summary\":\"3句话以内总结\","
             "\"findings\":[{\"title\",\"metric\",\"value\",\"evidence_step_id\",\"interpretation\"}],"
             "\"recommendations\":[{\"action\",\"reason\",\"confidence\":\"high|medium|low\",\"data_gaps\":[]}]}\n"
             "硬性要求:\n"
             "1. 每个 finding 的 value 必须原样照抄自对应步骤执行结果里的数字/文字，禁止编造或推算\n"
             "2. evidence_step_id 必须是纯数字(如 2，不要写 '步骤2' 或 'step2')，"
             "且必须指向真实存在的步骤；每个 finding 必须包含全部五个字段"
             "(title/metric/value/evidence_step_id/interpretation)\n"
             "3. 建议要具体可执行（进什么、进多少、什么时候）；数据不足时 confidence 填 low 并写明 data_gaps\n"
             "4. findings 最多 5 条，只保留最关键的；summary 中的数量描述必须与 findings 实际条数一致\n"
             "5. 趋势/排行类结论聚焦最需要行动的 Top 3~5 个商品，其余用『等 N 种商品』概括"
             "（N 以执行结果为准）；value 里禁止粘贴整个 DataFrame/字典\n"
             "6. 涉及月份的结论必须写明确切年月")
    steps_txt = "\n".join(
        f"步骤{s['step_id']}({plan[s['step_id']-1]['description']}): "
        f"{json.dumps(_compact(r), ensure_ascii=False)[:1800]}"
        for s, r in [(plan[i - 1], results[i]) for i in results])
    user_p = (f"数据: {notice}\n用户问题: {question}\n执行结果:\n{steps_txt}"
              f"{_time_rule(time_ctx or {}, trend_top is not None)}")
    if trend_top is not None:   # 趋势方案 §8: Reporter 只基于确定性清单写报告
        sys_p += ("\n7. 趋势报告模式(优先级高于第4/5条): findings 必须完全基于下方 trend_top 清单"
                  "(每个商品一条)，不得自行筛选/增删/改写商品，不得把非显著商品写进 findings\n"
                  "8. 每个 finding 的 evidence_step_id 使用清单中标注的步骤号；value 使用清单中的 每期变化额"
                  "(如 -248.49)；interpretation 说明该商品持续下降\n"
                  "9. 若 trend_top 为空: findings 输出空数组 []，summary 写明『未发现显著下降商品』\n"
                  "10. 报告中禁止出现 product_trend 等函数名/代码术语")
        user_p += ("\ntrend_top(显著下降商品清单, findings 必须完全基于它):\n"
                   + json.dumps(trend_top, ensure_ascii=False)[:2500])
    if errors:
        user_p += ("\n上一版报告未通过校验，请修正:\n- " + "\n- ".join(errors))
    for _ in range(2):
        out = llm.chat([{"role": "system", "content": sys_p},
                        {"role": "user", "content": user_p}], temperature=0.3, max_tokens=2500,
                       prefer="reporter")
        data = llm.extract_json(out)
        if isinstance(data, dict) and "findings" in data:
            data["summary"] = _coerce_str(data.get("summary"))
            data["recommendations"] = data.get("recommendations") or []
            data["charts"] = sorted({c for r in results.values() for c in r.get("charts", [])})
            return AnalysisReport(**{k: data[k] for k in
                                     AnalysisReport.model_fields if k in data})
    raise RuntimeError("Reporter 无法产出合法报告")


def _grounding(report: AnalysisReport, results: dict[int, dict]) -> list[str]:
    """校验 finding.value 中的数值确实出现在其引用步骤的结果里。
    P0-1: 两侧都先做格式归一化（千分位/货币符号/万亿单位）再比对。"""
    errs: list[str] = []
    pool = {sid: json.dumps(_compact(r), ensure_ascii=False) for sid, r in results.items()}
    pool_nums = {sid: _norm_numbers(t) for sid, t in pool.items()}
    for f in report.findings:
        sid = f.evidence_step_id
        if sid not in pool:
            errs.append(f"'{f.title}' 引用了不存在的步骤 {sid}")
            continue
        if len(str(f.value)) > 60:   # value 必须是简洁的数值/文字，禁止粘贴整段数据
            errs.append(f"'{f.title}' 的 value 过长，请只保留最关键的数值或结论")
            continue
        for v in _norm_numbers(str(f.value)):
            ok = any(abs(x - v) <= max(0.01, abs(v) * 0.01) for x in pool_nums.get(sid, [])
                     if abs(v) < 1e12)
            if not ok:
                errs.append(f"'{f.title}' 中的数值 {v:g} 未在步骤 {sid} 结果中出现")
                break
    # P2-2: summary 中的数量描述必须与 findings 条数一致
    m = re.search(r"(\d+)\s*[种个条项类]", report.summary or "")
    if m and report.findings and int(m.group(1)) != len(report.findings):
        errs.append(f"summary 中的数量({m.group(1)})与 findings 实际条数({len(report.findings)})不一致，"
                    "请让摘要与 findings 一致，或只总结最关键的发现")
    return errs


# ---------------- 趋势结果确定性提取与校验（趋势方案 §7/§9）----------------

def _find_df_rows(obj):
    """从 _plain 序列化结构递归找 DataFrame 行（兼容 result 直接是 DF / 嵌在 dict / list）"""
    if isinstance(obj, dict):
        if obj.get("type") == "dataframe":
            return obj.get("head") or []
        for v in obj.values():
            r = _find_df_rows(v)
            if r:
                return r
        return []
    if isinstance(obj, list):
        for v in obj:
            r = _find_df_rows(v)
            if r:
                return r
        return obj if obj and isinstance(obj[0], dict) else []
    return []


def _extract_trend_top(results: dict[int, dict], top_n: int = 5) -> list[dict]:
    """按步骤顺序扫描沙箱结果提取显著下降商品——不重算，evidence 指向提供数据的步骤。
    （兜底：即使 step1 被模型过滤成空，后续步骤的完整表也能救回）"""
    for sid in sorted(results):
        top: list[dict] = []
        for r in _find_df_rows((results.get(sid) or {}).get("result")):
            if not isinstance(r, dict) or r.get("趋势") != "下降":
                continue
            if r.get("显著") not in (True, "True", "true", 1):
                continue
            v = r.get("每期变化额")
            if isinstance(v, (int, float)):
                top.append({"商品名称": r.get("商品名称"), "每期变化额": v,
                            "相对变化": r.get("相对变化"), "evidence_step_id": sid})
        if top:
            top.sort(key=lambda t: t["每期变化额"])
            return top[:top_n]
    return []


def _validate_trend_report(report: AnalysisReport, trend_top: list[dict]) -> list[str]:
    """趋势报告确定性校验（step_code 部分已在 Analyst 阶段拦截——评审修正 P-1）"""
    errs: list[str] = []
    top_names = {str(t["商品名称"]) for t in trend_top if t.get("商品名称")}
    blob = "".join(f.title + f.interpretation + str(f.value) for f in report.findings)
    if top_names:
        if not any(n in blob for n in top_names):
            errs.append(f"报告未包含趋势步骤识别出的显著下降商品: {sorted(top_names)[:3]}")
        for f in report.findings:
            fblob = f.title + f.interpretation + str(f.value)
            if not any(n in fblob for n in top_names):
                errs.append(f"finding '{f.title}' 不在显著下降商品清单内，"
                            "findings 必须完全来自 trend_top")
    elif report.findings:
        errs.append("趋势分析未发现显著下降商品，findings 应为空并在 summary 中说明")
    if "product_trend" in (report.summary or "") or "product_trend" in blob:
        errs.append("报告中不得出现函数名/代码术语 product_trend")
    return errs


# ---------------- 主流程 ----------------

def run_pipeline(task_id: str, emit) -> None:
    task = db.one("SELECT * FROM tasks WHERE id=?", (task_id,))
    assert task, "任务不存在"
    question, merchant_id = task["question"], task["merchant_id"]
    frow = db.one("SELECT * FROM files WHERE id=?", (task["file_id"],))
    file_info = json.loads(frow["preview"])
    notes = db.memory_recall(merchant_id, question)
    # v2.2 记忆注入: 本会话最近 8 轮(16条)对话，1,500 字预算从最早轮次截断
    dlg_rows = db.query(
        "SELECT role, content FROM messages WHERE session_id=? ORDER BY id DESC LIMIT 16",
        (task["session_id"],))
    dlg_rows.reverse()
    buf: list[str] = []
    used = 0
    for m in dlg_rows:
        line = ("用户: " if m["role"] == "user" else "AI: ") + (m["content"] or "")[:200]
        if used + len(line) > 1500 and buf:
            break
        buf.append(line)
        used += len(line)
    dialog = "\n".join(buf)
    context_parts = [f"历史事实: {n}" for n in notes[:3]]
    if dialog:
        context_parts.append(dialog[-400:])
    context = "\n".join(context_parts)[:500]
    source = abs_path(frow["path"])

    # 1) Planner
    emit(task_id, "stage", stage="planning")
    db.execute("UPDATE tasks SET stage='planning' WHERE id=?", (task_id,))
    trend_mode = is_trend_question(question)     # 趋势问题 → 确定性链路
    time_ctx = _time_context(file_info)          # P0-2: 时间语义绑定
    plan = _plan(question, file_info, notes, time_ctx, trend_mode, dialog)
    if trend_mode:
        log.info("[%s] 趋势模式: step1 固定为 product_trend", task_id)
    db.execute("UPDATE tasks SET plan_json=? WHERE id=?", (json.dumps(plan, ensure_ascii=False), task_id))
    log.info("[%s] plan: %s", task_id, [s["description"] for s in plan])

    # 2) Analyst 逐步执行
    results: dict[int, dict] = {}
    prior: list[dict] = []
    for step in plan:
        sid = step["step_id"]
        is_tstep = trend_mode and sid == 1
        emit(task_id, "stage", stage=f"analysis:{sid}")
        db.execute("UPDATE tasks SET stage=? WHERE id=?", (f"analysis:{sid}", task_id))
        code, last_err = "", None
        for attempt in range(1, settings.analyst_max_attempts + 1):
            emit(task_id, "code_exec", step=sid, attempt=attempt, status="writing")
            code = _analyst_code(question, step, file_info, prior, last_err, time_ctx,
                                 is_trend_step=is_tstep, context=context)
            if issues := sandbox.audit(code):
                last_err = (f"安全审查拒绝使用: {issues}。"
                            "请改写: 不用这些函数，分析结果一律放进 result 变量返回，"
                            "不要读写任何文件，图表用 save_chart。")
                emit(task_id, "code_exec", step=sid, attempt=attempt, status="rejected")
                continue
            if is_tstep and (tissues := _trend_code_issues(code, time_ctx)):
                # 评审修正 P-1: 步骤代码校验在 Analyst 阶段拦截并重写（重试 Reporter 无法修代码）
                last_err = ("趋势步骤代码硬性校验未通过: " + "；".join(tissues)
                            + "。必须调用 product_trend(df, freq='W')，禁止单月过滤/月份字面量，"
                              "图表用 save_chart('trend_by_product.png')。")
                emit(task_id, "code_exec", step=sid, attempt=attempt, status="rejected")
                continue
            res = sandbox.run_code(task_id, code, source)
            emit(task_id, "code_exec", step=sid, attempt=attempt, status=res["status"])
            if res["status"] == "success":
                if is_tstep and not _find_df_rows(res.get("result")):
                    # 趋势步骤 result 被模型过滤成空（实测出现过 t[t['显著']=='下降']）→ 打回重写
                    last_err = ("趋势步骤 result 为空: 禁止对 product_trend 的输出做过滤/子集，"
                                "请直接把完整结果赋给 result（result = t）。"
                                "显著/趋势筛选由系统在报告阶段完成。")
                    emit(task_id, "code_exec", step=sid, attempt=attempt, status="rejected")
                    continue
                break
            print(f"[{task_id}] step{sid} attempt{attempt} -> {res['status']}: "
                  f"{(res.get('error') or '')[-600:]}", flush=True)
            last_err = res.get("error") or res.get("stdout") or "未知错误"
        else:
            db.execute("INSERT OR REPLACE INTO task_steps VALUES(?,?,?,?,?,?,?)",
                       (task_id, sid, "failed", code, None, json.dumps([]), attempt))
            raise RuntimeError(f"步骤{sid}在{attempt}次尝试后仍失败")
        db.execute("INSERT OR REPLACE INTO task_steps VALUES(?,?,?,?,?,?,?)",
                   (task_id, sid, "done", code,
                    json.dumps({"result": res.get("result")}, ensure_ascii=False)[:200000],
                    json.dumps(res.get("charts", []), ensure_ascii=False), attempt))
        results[sid] = res
        prior.append({"step": sid, "result": res.get("result")})
        for chart in res.get("charts", []):
            emit(task_id, "chart", file=chart)

    # 3) Reporter + grounding
    emit(task_id, "stage", stage="reporting")
    db.execute("UPDATE tasks SET stage='reporting' WHERE id=?", (task_id,))
    trend_top = _extract_trend_top(results) if trend_mode else None
    if trend_mode:
        log.info("[%s] trend_top: %s", task_id,
                 [(t["商品名称"], t["每期变化额"]) for t in trend_top])
    report = _report(question, file_info, plan, results, time_ctx=time_ctx, trend_top=trend_top)
    errs = _grounding(report, results)
    if trend_mode:
        errs += _validate_trend_report(report, trend_top or [])
    if errs:
        log.warning("[%s] 校验未过: %s", task_id, errs[:3])
        report = _report(question, file_info, plan, results, errors=errs,
                         time_ctx=time_ctx, trend_top=trend_top)
        errs2 = _grounding(report, results)
        if trend_mode:
            errs2 += _validate_trend_report(report, trend_top or [])
        if errs2:
            report.verified = False
            report.caveats += [f"部分结论未经数据溯源校验: {errs2[:2]}"]

    # P0-2: 相对时间问题 → 确定性标注实际分析月份（不依赖模型自觉）
    if re.search(r"这个月|本月|当月|上月|上个月", question) and time_ctx:
        if "分析月份" not in report.summary:
            report.summary = f"【分析月份：{time_ctx['latest_full_month']}】{report.summary}"
    # P2-2: findings 超过 6 条时确定性折叠，聚焦 Top 5
    if len(report.findings) > 6:
        dropped = len(report.findings) - 5
        report.findings = report.findings[:5]
        report.caveats.append(f"另有 {dropped} 条次要发现已折叠，完整清单见分析步骤结果")
    db.execute("INSERT OR REPLACE INTO reports VALUES(?,?,?)",
               (task_id, report.model_dump_json(), db.now()))

    # 4) 写记忆
    recs = "；".join(r.action for r in report.recommendations[:3])
    db.memory_add(merchant_id, "conclusion",
                  f"Q:{question} → {report.summary} 建议:{recs}")
    emit(task_id, "final", verified=report.verified)
