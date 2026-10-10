# W3 基线评测报告：no_rag_control（2026-10-10 15:37:34）

## 运行配置
- 数据集：`eval\dataset\eval_v1.jsonl`（30 条，单轮）
- top_n=0 ｜ runs=1 ｜ 本次共 30 次题级运行
- 模型：deepseek-flash（temperature=0）｜ 快照锚点：2026-09-30
- 检索参数：top_k_each=20，RRF k=60（与线上 hybrid_retriever 默认一致，§2.3 同构）

## 四指标汇总

### 执行准确率（比较重放结果集，不看 SQL 文本）
- overall：**6.7%**（pass 2 / fail 28 / error 0，共 30 次）
| 分组 | pass | fail | error | 准确率 |
| --- | --- | --- | --- | --- |
| single_table | 0 | 6 | 0 | 0.0% |
| multi_join | 0 | 7 | 0 | 0.0% |
| aggregation | 2 | 5 | 0 | 28.6% |
| time_window | 0 | 5 | 0 | 0.0% |
| glossary_trap | 0 | 5 | 0 | 0.0% |
| 分组 | pass | fail | error | 准确率 |
| --- | --- | --- | --- | --- |
| 有同款示例（EV-001~011，§1.3） | 0 | 11 | 0 | 0.0% |
| 泛化题（EV-012 起） | 2 | 17 | 0 | 10.5% |

### Recall@8
- 对照行（top_n=0）注入恒空，Recall=0 是设定使然，本节不列数字；报告只做执行准确率对比（§2.5）

### 延迟（wall-clock：search 前起、invoke 返回止，含全部工具往返）
- mean 7.6s ｜ median 6.9s ｜ P95 11.7s ｜ max 14.5s
- P95 口径：statistics.quantiles(n=20, method='inclusive')[18]（线性插值，§8-5 拍板）
- 分题型 P95：single_table 6.7s ｜ multi_join 13.0s ｜ aggregation 10.4s ｜ time_window 8.7s ｜ glossary_trap 11.7s
- 延迟绝对值受网络波动影响，仅相对可比（见声明③）

### token 成本（累加全部 AIMessage.usage_metadata，计费口径 §2.6）
- 单题均值：input 9458 ｜ output 1247 ｜ total 10706（cache_read 8124 单列，不重复计）
- 总量：input 283755 ｜ output 37410 ｜ total 321165（cache_read 243712）；人民币估算按当期牌价手填旁注

## 对照行对比表
（本次运行未提供 --compare 明细文件；对照行对比由 T3 汇主行与对照行两份 detail JSON 后生成。）

## 错答模式统计
| failure_mode | 次数 | 典型样本（case id） |
| --- | --- | --- |
| other | 28 | EV-001、EV-002、EV-003 |

trap:<id> 与 valid_alt_gloss 分开统计（§4.3）：前者是真陷阱，后者是合法口径但答非所问；other 为错出新花样，人工看明细 JSON 归类后可回填登记表。

## flaky 题记录
（无 （本次 runs=1，无从翻转））

## 多轮评测节
（本次未运行多轮评测；多轮题用 `--multiturn` 跑 eval_v1_multiturn.jsonl，独立报告节 §1.7/§2.7。）

## 逐题结果
| run | id | 判定 | failure_mode | sql_source | 行数(金/模) | 延迟s | tokens(total) |
| --- | --- | --- | --- | --- | --- | --- | --- |
| r1 | EV-001 | fail | other | last | 15/15 | 6.384 | 9095 |
| r1 | EV-002 | fail | other | last | 5/5 | 6.572 | 9614 |
| r1 | EV-003 | fail | other | last | 1/1 | 8.664 | 11102 |
| r1 | EV-004 | fail | other | last | 1/1 | 11.728 | 18540 |
| r1 | EV-005 | fail | other | last | 1/2 | 9.577 | 15705 |
| r1 | EV-006 | fail | other | last | 1/1 | 6.292 | 9094 |
| r1 | EV-007 | fail | other | last | 7/1 | 6.67 | 10189 |
| r1 | EV-008 | fail | other | last | 1/6 | 8.179 | 10117 |
| r1 | EV-009 | fail | other | last | 1/1 | 9.89 | 9787 |
| r1 | EV-010 | fail | other | last | 3/5 | 5.05 | 7662 |
| r1 | EV-011 | fail | other | last | 3/3 | 4.886 | 6474 |
| r1 | EV-012 | fail | other | last | 1/6 | 5.711 | 9327 |
| r1 | EV-013 | fail | other | last | 3/1 | 5.206 | 8738 |
| r1 | EV-014 | fail | other | last | 6/6 | 3.918 | 6353 |
| r1 | EV-015 | fail | other | last | 5/5 | 6.872 | 11047 |
| r1 | EV-016 | fail | other | last | 20/1 | 8.402 | 15122 |
| r1 | EV-017 | fail | other | last | 8/8 | 8.979 | 14718 |
| r1 | EV-018 | fail | other | last | 20/0 | 8.965 | 12111 |
| r1 | EV-019 | fail | other | last | 5/1 | 6.681 | 11361 |
| r1 | EV-020 | fail | other | last | 1/3 | 14.467 | 22019 |
| r1 | EV-021 | pass | - | last | 5/5 | 4.272 | 6392 |
| r1 | EV-022 | pass | - | last | 3/3 | 3.563 | 5143 |
| r1 | EV-023 | fail | other | last | 1/6 | 7.869 | 10267 |
| r1 | EV-024 | fail | other | last | 20/1 | 10.561 | 11964 |
| r1 | EV-025 | fail | other | last | 1/2 | 5.257 | 6870 |
| r1 | EV-026 | fail | other | last | 7/7 | 6.404 | 8820 |
| r1 | EV-027 | fail | other | last | 12/12 | 9.181 | 11143 |
| r1 | EV-028 | fail | other | last | 1/6 | 7.009 | 7683 |
| r1 | EV-029 | fail | other | last | 1/1 | 8.548 | 13059 |
| r1 | EV-030 | fail | other | last | 1/1 | 11.7 | 11649 |

## 声明（固定四条，W3 设计 §3.3）
1. 本报告是**系统级数字**：评测对象为"混合检索注入 + Agent"完整链路，含 few-shot 自举
   （qa_pair 片参与检索，11 条评测题原题在示例库中），不得表述为"模型的 NL2SQL 能力"（§1.3）。
2. temperature=0 仍存在服务端非确定性；主行（runs>1）为多次运行结果，W6 定稿前会复测（§3.2）。
3. 延迟绝对值受评测时网络波动影响，仅横向（题型间）与纵向（W6 前后对比）相对可比（§2.6）。
4. token 成本按当次实测累计（逐条 AIMessage.usage_metadata 累加；cache_read 单列，
   已含在 input 内不重复计）；人民币估算按当期牌价手填旁注，不写死单价（§2.6）。

