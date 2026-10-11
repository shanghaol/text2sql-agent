# W3 基线评测报告：w5_sqlguard_smoke（2026-10-11 17:35:03）

## 运行配置
- 数据集：`eval\dataset\eval_v1.jsonl`（2 条，单轮）
- top_n=8 ｜ runs=1 ｜ 本次共 2 次题级运行
- 模型：deepseek-flash（temperature=0）｜ 快照锚点：2026-09-30
- 检索参数：top_k_each=20，RRF k=60（与线上 hybrid_retriever 默认一致，§2.3 同构）
- 语料：schema 6 / glossary 11 / qa_pair 11（共 28 片，索引自 data/rag_corpus 删除重建）

## 四指标汇总

### 执行准确率（SQL 级：比较重放结果集，不看 SQL 文本）
- overall：**100.0%**（SQL 判过 2 ｜ 文本判过 0（计入答案级，见下节）｜ fail 0 ｜ error 0，共 2 次）
| 分组 | SQL 判过 | 文本判过 | fail | error | SQL 级准确率 | 答案级准确率 |
| --- | --- | --- | --- | --- | --- | --- |
| single_table | 0 | 0 | 0 | 0 | 0.0% | 0.0% |
| multi_join | 1 | 0 | 0 | 0 | 100.0% | 100.0% |
| aggregation | 0 | 0 | 0 | 0 | 0.0% | 0.0% |
| time_window | 1 | 0 | 0 | 0 | 100.0% | 100.0% |
| glossary_trap | 0 | 0 | 0 | 0 | 0.0% | 0.0% |
| 分组 | SQL 判过 | 文本判过 | fail | error | SQL 级准确率 | 答案级准确率 |
| --- | --- | --- | --- | --- | --- | --- |
| 有同款示例（EV-001~011，§1.3） | 2 | 0 | 0 | 0 | 100.0% | 100.0% |
| 泛化题（EV-012 起） | 0 | 0 | 0 | 0 | 0.0% | 0.0% |

### 回答准确率（答案级，含文本计算）
- overall：**100.0%**（pass_via：sql 2 + text 0，共 2 次）
- 差值说明：答案级 − SQL 级 = 0.0%（文本判过 0 题）= Agent 文本计算弥补单条 SQL 表达局限的部分，W6 消融时分别跟踪（§2.4 v2 / 声明⑤）

### Recall@8
- 整体逐片 Recall：**25.0%**（Σ|∩|=1 / Σ|expected|=4）
- 题级全命中率（expected 全部命中）：0.0%（0/2 题）
- 分 doc_type：schema 25.0% ｜ glossary 0.0% ｜ qa_pair 0.0%
- 分题型：single_table 0.0% ｜ multi_join 33.3% ｜ aggregation 0.0% ｜ time_window 0.0% ｜ glossary_trap 0.0%

### 延迟（wall-clock：search 前起、invoke 返回止，含全部工具往返）
- mean 2.9s ｜ median 2.9s ｜ P95 3.1s ｜ max 3.1s
- P95 口径：statistics.quantiles(n=20, method='inclusive')[18]（线性插值，§8-5 拍板）
- 分题型 P95：multi_join 3.1s ｜ time_window 2.7s
- 延迟绝对值受网络波动影响，仅相对可比（见声明③）

### token 成本（累加全部 AIMessage.usage_metadata，计费口径 §2.6）
- 单题均值：input 8239 ｜ output 504 ｜ total 8744（cache_read 7872 单列，不重复计）
- 总量：input 16478 ｜ output 1009 ｜ total 17487（cache_read 15744）；人民币估算按当期牌价手填旁注

## 对照行对比表
（本次运行未提供 --compare 明细文件；对照行对比由 T3 汇主行与对照行两份 detail JSON 后生成。）

## 错答模式统计
（本次运行无 fail / error 记录。）

## flaky 题记录
（无 （本次 runs=1，无从翻转））

## 多轮评测节
（本次未运行多轮评测；多轮题用 `--multiturn` 跑 eval_v1_multiturn.jsonl，独立报告节 §1.7/§2.7。）

## 逐题结果
| run | id | 判定 | pass_via | failure_mode | sql_source | 行数(金/模) | 延迟s | tokens(total) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| r1 | EV-001 | pass | sql | - | last | 15/15 | 2.703 | 7543 |
| r1 | EV-002 | pass | sql | - | last | 5/5 | 3.091 | 9944 |

## 声明（固定五条，W3 设计 §3.3）
1. 本报告是**系统级数字**：评测对象为"混合检索注入 + Agent"完整链路，含 few-shot 自举
   （qa_pair 片参与检索，11 条评测题原题在示例库中），不得表述为"模型的 NL2SQL 能力"（§1.3）。
2. temperature=0 仍存在服务端非确定性；主行（runs>1）为多次运行结果，W6 定稿前会复测（§3.2）。
3. 延迟绝对值受评测时网络波动影响，仅横向（题型间）与纵向（W6 前后对比）相对可比（§2.6）。
4. token 成本按当次实测累计（逐条 AIMessage.usage_metadata 累加；cache_read 单列，
   已含在 input 内不重复计）；人民币估算按当期牌价手填旁注，不写死单价（§2.6）。
5. 双层指标分列：SQL 级执行准确率只认重放结果集一致（pass_via=sql），答案级回答准确率
   额外计入最终回答文本计算命中金标的题（pass_via=text）；两者差值 = Agent 文本计算弥补
   SQL 单查询表达局限的部分，W6 消融时分别跟踪（§2.4 判定规则 v2）。

