# 店铺经营分析 Agent

自然语言提问 → 自动写 pandas 代码 → 沙箱执行 → 输出**带溯源、带图表**的经营分析报告。

场景：门店经营决策缺乏数据支撑（无 BI、无数据团队），老板问"这个月卖得怎么样""哪些商品该少进货"需要人工取数做表。

> **项目矩阵**（作者三个 LLM 应用项目，建议按顺序看）：[bidding-rag-system · RAG 检索](https://github.com/haohao66996/bidding-rag-system) ｜ [lawagent · Agentic RAG](https://github.com/haohao66996/lawagent) ｜ [shop-agent · Code Interpreter](https://github.com/haohao66996/shop-agent)
>
> **一句话**：门店经营分析 Agent，「自然语言提问 → 自动生成并执行 pandas 代码 → 带溯源图表报告」全链路。
> **三个数字**：AST + rlimit + 墙钟超时三层沙箱，危险调用全拦截；回归 2/2、单测 15/15；双 vLLM 实例显存占用 27/32GB。
> **技术栈**：vLLM(Qwen2.5-7B/14B-Instruct-AWQ) · Function Calling · pandas · bge-small-zh · ECharts · SSE
>
## 架构：三段式 Agent 流水线

```
用户提问
   ↓
Planner  (Qwen2.5-7B)   拆解分析计划
   ↓
Analyst  (Qwen2.5-14B)  生成 pandas 代码 → 沙箱执行
   ↓                         ↑ 失败则带错误模式提示重试（≤4 次）
Reporter (Qwen2.5-7B)   生成带图表报告 + grounding 数值核验
   ↓
结论附「依据：步骤 N」溯源
```

## 关键技术决策（含选型理由）

| 决策 | 理由 |
|---|---|
| **双模型分层路由**：代码生成走 14B，规划与写作走 7B | 实测 7B 生成代码存在**废弃 API** 与**连环 assert**，通过率不达标；但规划/写作不需要 14B，分层省资源 |
| 双 vLLM 实例 | 两个尺寸模型同时常驻，实测显存占用 **27 / 32 GB** |
| **AST 静态审查 + rlimit + 墙钟超时** 三层沙箱 | 执行 LLM 生成的代码，任一单层都不够：AST 挡危险调用，rlimit 挡资源耗尽，超时挡死循环 |
| **grounding 数值归一化比对** | 报告数字必须与代码执行结果程序化比对，不一致打回重写并打「未验证」标 |
| 趋势题走**确定性链路** | 纯 LLM 容易产生"单月假趋势"；改为 意图识别 → 固定步骤 → 显著性三分类（阈值以真实数据标定） |

### 沙箱实测拦截

AST 黑名单实测拦截：`os` / `open` / `requests` / `subprocess` / `socket` / `__import__`。
rlimit 约束 CPU、内存、文件句柄，配合墙钟超时，死循环与危险调用全拦截。

## 记忆与多轮

- 最近 8 轮对话 + **店铺事实卡**自动注入，正确承接「那第二名呢？」这类指代与省略
- 跨会话检索：**bge 向量 + 关键词 RRF 双路召回**，异常自动降级 LIKE

## 工程质量

- 五页 SPA 工作台（原生 JS + ECharts，零构建）、KPI 看板、Excel 导出
- SSE 五阶段实时展示：`planning → analysis → code_exec → chart → final`
- **vLLM → DeepSeek API 降级链**，本地模型不可用时自动降级
- 异步任务 + 幂等键防重复提交

### 测试

| 项 | 结果 |
|---|---|
| 单元测试 | 15 / 15 |
| 回归测试 | 2 / 2 |
| 测试问题修复 | 8 / 8 |

测试用例：`tests/test_grounding.py`（数值核验）、`tests/test_trend_intent.py`（趋势意图）、`tests/test_product_trend.py`（商品趋势）；回归：`scripts/regression.py`。

## 已知问题（诚实清单）

系统当前为 MVP。测试中发现并记录了 **2 个 P0 + 3 个 P1 + 3 个 P2**，均带根因分析与任务 ID，修复方案已确定但**尚未全部完成**：

- P0-1：报告数值与执行结果不一致时的打回重写链路
- P0-2：千分位逗号导致 grounding 误判（`"1,234"` 被当作非数值）
- P1：含 `rc=-9` 的执行失败需区分「超时」与「资源超限」两种语义

完整清单见测试报告。**不声称已上线供生产使用。**

## 本地运行

```bash
pip install -r requirements.txt
python scripts/make_demo_data.py     # 生成演示销售数据
python scripts/start_all.sh          # 启动 API + 前端
python scripts/regression.py         # 回归测试
```

> 模型权重未纳入仓库，用 `scripts/download_model.py` 拉取。

## 演示数据说明

仓库内演示数据为构造的奶茶/餐饮门店销售流水（12 种商品 × 7 字段），预置了"周末 +30%、冰品夏季 +50%、芋泥波波持续衰减"等规律，用于验证分析链路正确性。
