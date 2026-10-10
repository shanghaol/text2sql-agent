# W3 基线评测报告：no_rag_control_v2（2026-10-10 16:12:42）

## 运行配置
- 数据集：`eval\dataset\eval_v1.jsonl`（30 条，单轮）
- top_n=0 ｜ runs=1 ｜ 本次共 30 次题级运行
- 模型：deepseek-flash（temperature=0）｜ 快照锚点：2026-09-30
- 检索参数：top_k_each=20，RRF k=60（与线上 hybrid_retriever 默认一致，§2.3 同构）

## 四指标汇总

### 执行准确率（SQL 级：比较重放结果集，不看 SQL 文本）
- overall：**6.7%**（SQL 判过 2 ｜ 文本判过 11（计入答案级，见下节）｜ fail 17 ｜ error 0，共 30 次）
| 分组 | SQL 判过 | 文本判过 | fail | error | SQL 级准确率 | 答案级准确率 |
| --- | --- | --- | --- | --- | --- | --- |
| single_table | 1 | 2 | 3 | 0 | 16.7% | 50.0% |
| multi_join | 0 | 1 | 6 | 0 | 0.0% | 14.3% |
| aggregation | 1 | 2 | 4 | 0 | 14.3% | 42.9% |
| time_window | 0 | 1 | 4 | 0 | 0.0% | 20.0% |
| glossary_trap | 0 | 5 | 0 | 0 | 0.0% | 100.0% |
| 分组 | SQL 判过 | 文本判过 | fail | error | SQL 级准确率 | 答案级准确率 |
| --- | --- | --- | --- | --- | --- | --- |
| 有同款示例（EV-001~011，§1.3） | 0 | 5 | 6 | 0 | 0.0% | 45.5% |
| 泛化题（EV-012 起） | 2 | 6 | 11 | 0 | 10.5% | 42.1% |

### 回答准确率（答案级，含文本计算）
- overall：**43.3%**（pass_via：sql 2 + text 11，共 30 次）
- 差值说明：答案级 − SQL 级 = 36.7%（文本判过 11 题）= Agent 文本计算弥补单条 SQL 表达局限的部分，W6 消融时分别跟踪（§2.4 v2 / 声明⑤）

### Recall@8
- 对照行（top_n=0）注入恒空，Recall=0 是设定使然，本节不列数字；报告只做执行准确率对比（§2.5）

### 延迟（wall-clock：search 前起、invoke 返回止，含全部工具往返）
- mean 7.5s ｜ median 7.1s ｜ P95 12.4s ｜ max 15.3s
- P95 口径：statistics.quantiles(n=20, method='inclusive')[18]（线性插值，§8-5 拍板）
- 分题型 P95：single_table 7.5s ｜ multi_join 13.5s ｜ aggregation 10.7s ｜ time_window 9.1s ｜ glossary_trap 12.9s
- 延迟绝对值受网络波动影响，仅相对可比（见声明③）

### token 成本（累加全部 AIMessage.usage_metadata，计费口径 §2.6）
- 单题均值：input 9063 ｜ output 1250 ｜ total 10313（cache_read 7765 单列，不重复计）
- 总量：input 271883 ｜ output 37510 ｜ total 309393（cache_read 232960）；人民币估算按当期牌价手填旁注

## 对照行对比表
（本次运行未提供 --compare 明细文件；对照行对比由 T3 汇主行与对照行两份 detail JSON 后生成。）

## 错答模式统计
| failure_mode | 次数 | 典型样本（case id） |
| --- | --- | --- |
| other | 17 | EV-001、EV-002、EV-007 |

trap:<id> 与 valid_alt_gloss 分开统计（§4.3）：前者是真陷阱，后者是合法口径但答非所问；other 为错出新花样，人工看明细 JSON 归类后可回填登记表。

## flaky 题记录
（无 （本次 runs=1，无从翻转））

## 多轮评测节
（本次未运行多轮评测；多轮题用 `--multiturn` 跑 eval_v1_multiturn.jsonl，独立报告节 §1.7/§2.7。）

## 逐题结果
| run | id | 判定 | pass_via | failure_mode | sql_source | 行数(金/模) | 延迟s | tokens(total) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| r1 | EV-001 | fail | - | other | last | 15/15 | 6.765 | 7743 |
| r1 | EV-002 | fail | - | other | last | 5/5 | 8.573 | 11739 |
| r1 | EV-003 | pass | text | - | last | 1/6 | 11.423 | 15714 |
| r1 | EV-004 | pass | text | - | last | 1/3 | 9.93 | 14555 |
| r1 | EV-005 | pass | text | - | last | 1/1 | 8.251 | 13086 |
| r1 | EV-006 | pass | text | - | last | 1/1 | 7.583 | 8784 |
| r1 | EV-007 | fail | - | other | last | 7/7 | 5.499 | 7001 |
| r1 | EV-008 | pass | text | - | last | 1/1 | 8.53 | 10823 |
| r1 | EV-009 | fail | - | other | last | 1/6 | 9.402 | 11873 |
| r1 | EV-010 | fail | - | other | last | 3/3 | 7.265 | 11469 |
| r1 | EV-011 | fail | - | other | last | 3/3 | 4.737 | 6951 |
| r1 | EV-012 | pass | text | - | last | 1/20 | 6.86 | 11901 |
| r1 | EV-013 | fail | - | other | last | 3/3 | 3.484 | 6720 |
| r1 | EV-014 | pass | sql | - | last | 6/6 | 4.24 | 6228 |
| r1 | EV-015 | fail | - | other | last | 5/5 | 4.103 | 6960 |
| r1 | EV-016 | fail | - | other | last | 20/1 | 6.915 | 10588 |
| r1 | EV-017 | fail | - | other | fallback_to_earlier | 8/8 | 9.355 | 13262 |
| r1 | EV-018 | fail | - | other | last | 20/1 | 8.064 | 11640 |
| r1 | EV-019 | fail | - | other | last | 5/5 | 5.437 | 7020 |
| r1 | EV-020 | fail | - | other | last | 1/1 | 15.347 | 21479 |
| r1 | EV-021 | fail | - | other | last | 5/5 | 5.166 | 6781 |
| r1 | EV-022 | pass | sql | - | last | 3/3 | 4.8 | 6938 |
| r1 | EV-023 | pass | text | - | last | 1/6 | 5.446 | 6802 |
| r1 | EV-024 | fail | - | other | last | 20/1 | 11.325 | 18452 |
| r1 | EV-025 | pass | text | - | last | 1/2 | 4.343 | 6870 |
| r1 | EV-026 | fail | - | other | last | 7/1 | 4.836 | 5688 |
| r1 | EV-027 | fail | - | other | last | 12/12 | 9.505 | 10617 |
| r1 | EV-028 | pass | text | - | last | 1/6 | 7.661 | 7825 |
| r1 | EV-029 | pass | text | - | last | 1/1 | 6.912 | 7791 |
| r1 | EV-030 | pass | text | - | last | 1/1 | 13.263 | 16093 |

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

