# W3 基线评测报告：baseline_full（2026-10-10 15:41:27）

## 运行配置
- 数据集：`eval\dataset\eval_v1.jsonl`（30 条，单轮）
- top_n=8 ｜ runs=3 ｜ 本次共 90 次题级运行
- 模型：deepseek-flash（temperature=0）｜ 快照锚点：2026-09-30
- 检索参数：top_k_each=20，RRF k=60（与线上 hybrid_retriever 默认一致，§2.3 同构）

## 四指标汇总

### 执行准确率（比较重放结果集，不看 SQL 文本）
- overall：**32.2%**（pass 29 / fail 61 / error 0，共 90 次）
- 各 run：r1=40.0% ｜ r2=26.7% ｜ r3=30.0%（三次结果不一致的题见 flaky 节，§3.2 不机械取众数）
| 分组 | pass | fail | error | 准确率 |
| --- | --- | --- | --- | --- |
| single_table | 5 | 13 | 0 | 27.8% |
| multi_join | 7 | 14 | 0 | 33.3% |
| aggregation | 6 | 15 | 0 | 28.6% |
| time_window | 9 | 6 | 0 | 60.0% |
| glossary_trap | 2 | 13 | 0 | 13.3% |
| 分组 | pass | fail | error | 准确率 |
| --- | --- | --- | --- | --- |
| 有同款示例（EV-001~011，§1.3） | 19 | 14 | 0 | 57.6% |
| 泛化题（EV-012 起） | 10 | 47 | 0 | 17.5% |

### Recall@8
- 整体逐片 Recall：**57.6%**（Σ|∩|=102 / Σ|expected|=177）
- 题级全命中率（expected 全部命中）：40.0%（36/90 题）
- 分 doc_type：schema 51.9% ｜ glossary 100.0% ｜ qa_pair 100.0%
- 分题型：single_table 62.5% ｜ multi_join 47.8% ｜ aggregation 70.0% ｜ time_window 60.0% ｜ glossary_trap 61.5%

### 延迟（wall-clock：search 前起、invoke 返回止，含全部工具往返）
- mean 3.9s ｜ median 3.3s ｜ P95 8.3s ｜ max 11.0s
- P95 口径：statistics.quantiles(n=20, method='inclusive')[18]（线性插值，§8-5 拍板）
- 分题型 P95：single_table 4.9s ｜ multi_join 10.4s ｜ aggregation 8.6s ｜ time_window 6.9s ｜ glossary_trap 6.6s
- 延迟绝对值受网络波动影响，仅相对可比（见声明③）

### token 成本（累加全部 AIMessage.usage_metadata，计费口径 §2.6）
- 单题均值：input 8448 ｜ output 694 ｜ total 9142（cache_read 7063 单列，不重复计）
- 总量：input 760282 ｜ output 62467 ｜ total 822749（cache_read 635644）；人民币估算按当期牌价手填旁注

## 对照行对比表
| 题型 | 本行（baseline_full） | 对照（no_rag_control） | 差值（百分点） |
| --- | --- | --- | --- |
| overall | 32.2% | 6.7% | +25.6 |
| single_table | 27.8% | 0.0% | +27.8 |
| multi_join | 33.3% | 0.0% | +33.3 |
| aggregation | 28.6% | 28.6% | +0.0 |
| time_window | 60.0% | 0.0% | +60.0 |
| glossary_trap | 13.3% | 0.0% | +13.3 |

## 错答模式统计
| failure_mode | 次数 | 典型样本（case id） |
| --- | --- | --- |
| other | 61 | EV-004、EV-006、EV-008 |

trap:<id> 与 valid_alt_gloss 分开统计（§4.3）：前者是真陷阱，后者是合法口径但答非所问；other 为错出新花样，人工看明细 JSON 归类后可回填登记表。

## flaky 题记录
| id | 各 run 判定 |
| --- | --- |
| EV-003 | pass → fail → fail |
| EV-005 | pass → fail → pass |
| EV-008 | fail → fail → pass |
| EV-012 | fail → fail → pass |
| EV-014 | pass → fail → fail |
| EV-018 | pass → fail → fail |
| EV-019 | pass → fail → fail |
| EV-021 | pass → fail → fail |
| EV-022 | pass → pass → fail |
| EV-027 | fail → pass → fail |
| EV-028 | fail → pass → pass |

flaky 题不机械取众数：人工复盘明细 JSON（三次的 SQL 差在哪），结论记入 experiment-log（§3.2）。

## 多轮评测节
（本次未运行多轮评测；多轮题用 `--multiturn` 跑 eval_v1_multiturn.jsonl，独立报告节 §1.7/§2.7。）

## 逐题结果
| run | id | 判定 | failure_mode | sql_source | 行数(金/模) | 延迟s | tokens(total) |
| --- | --- | --- | --- | --- | --- | --- | --- |
| r1 | EV-001 | pass | - | last | 15/15 | 2.903 | 7478 |
| r1 | EV-002 | pass | - | last | 5/5 | 3.351 | 9990 |
| r1 | EV-003 | pass | - | last | 1/1 | 2.335 | 5446 |
| r1 | EV-004 | fail | other | last | 1/1 | 5.268 | 11505 |
| r1 | EV-005 | pass | - | last | 1/1 | 3.046 | 8094 |
| r1 | EV-006 | fail | other | last | 1/1 | 3.412 | 9865 |
| r1 | EV-007 | pass | - | last | 7/7 | 2.851 | 7083 |
| r1 | EV-008 | fail | other | last | 1/1 | 2.576 | 7170 |
| r1 | EV-009 | fail | other | last | 1/1 | 3.14 | 5596 |
| r1 | EV-010 | pass | - | last | 3/3 | 2.586 | 10773 |
| r1 | EV-011 | pass | - | last | 3/3 | 3.802 | 8468 |
| r1 | EV-012 | fail | other | last | 1/6 | 3.598 | 9124 |
| r1 | EV-013 | fail | other | last | 3/3 | 2.189 | 8467 |
| r1 | EV-014 | pass | - | last | 6/6 | 2.388 | 7349 |
| r1 | EV-015 | fail | other | last | 5/5 | 3.662 | 8274 |
| r1 | EV-016 | fail | other | last | 20/20 | 5.29 | 12118 |
| r1 | EV-017 | fail | other | last | 8/8 | 7.834 | 14265 |
| r1 | EV-018 | pass | - | last | 20/20 | 4.295 | 8694 |
| r1 | EV-019 | pass | - | last | 5/5 | 3.849 | 8746 |
| r1 | EV-020 | fail | other | last | 1/1 | 11.017 | 18309 |
| r1 | EV-021 | pass | - | last | 5/5 | 3.651 | 7013 |
| r1 | EV-022 | pass | - | last | 3/3 | 3.09 | 9276 |
| r1 | EV-023 | fail | other | last | 1/1 | 3.238 | 8960 |
| r1 | EV-024 | fail | other | last | 20/20 | 4.032 | 7634 |
| r1 | EV-025 | fail | other | last | 1/2 | 2.853 | 7022 |
| r1 | EV-026 | fail | other | last | 7/7 | 2.681 | 7390 |
| r1 | EV-027 | fail | other | last | 12/12 | 6.807 | 9686 |
| r1 | EV-028 | fail | other | last | 1/1 | 3.281 | 7104 |
| r1 | EV-029 | fail | other | last | 1/1 | 2.696 | 5570 |
| r1 | EV-030 | fail | other | last | 1/1 | 9.59 | 9454 |
| r2 | EV-001 | pass | - | last | 15/15 | 2.542 | 7506 |
| r2 | EV-002 | pass | - | last | 5/5 | 2.982 | 9903 |
| r2 | EV-003 | fail | other | last | 1/1 | 1.962 | 5305 |
| r2 | EV-004 | fail | other | last | 1/1 | 2.885 | 7259 |
| r2 | EV-005 | fail | other | last | 1/1 | 5.155 | 12969 |
| r2 | EV-006 | fail | other | last | 1/1 | 3.413 | 9912 |
| r2 | EV-007 | pass | - | last | 7/7 | 2.858 | 7045 |
| r2 | EV-008 | fail | other | last | 1/1 | 2.672 | 7175 |
| r2 | EV-009 | fail | other | last | 1/1 | 4.056 | 8414 |
| r2 | EV-010 | pass | - | last | 3/3 | 3.029 | 10693 |
| r2 | EV-011 | pass | - | last | 3/3 | 2.679 | 8340 |
| r2 | EV-012 | fail | other | last | 1/6 | 3.463 | 13349 |
| r2 | EV-013 | fail | other | last | 3/3 | 2.016 | 8489 |
| r2 | EV-014 | fail | other | last | 6/1 | 3.177 | 7340 |
| r2 | EV-015 | fail | other | last | 5/5 | 4.524 | 8803 |
| r2 | EV-016 | fail | other | last | 20/1 | 4.501 | 11103 |
| r2 | EV-017 | fail | other | last | 8/1 | 5.022 | 8785 |
| r2 | EV-018 | fail | other | last | 20/1 | 4.018 | 12713 |
| r2 | EV-019 | fail | other | last | 5/5 | 4.166 | 8833 |
| r2 | EV-020 | fail | other | last | 1/1 | 2.853 | 7063 |
| r2 | EV-021 | fail | other | last | 5/5 | 4.275 | 11313 |
| r2 | EV-022 | pass | - | last | 3/3 | 3.38 | 9340 |
| r2 | EV-023 | fail | other | last | 1/1 | 4.758 | 13617 |
| r2 | EV-024 | fail | other | last | 20/20 | 9.43 | 13826 |
| r2 | EV-025 | fail | other | last | 1/2 | 2.904 | 6863 |
| r2 | EV-026 | fail | other | last | 7/7 | 3.194 | 7537 |
| r2 | EV-027 | pass | - | last | 12/12 | 4.711 | 5927 |
| r2 | EV-028 | pass | - | last | 1/1 | 3.032 | 6901 |
| r2 | EV-029 | fail | other | last | 1/1 | 3.747 | 8850 |
| r2 | EV-030 | fail | other | last | 1/1 | 4.968 | 8291 |
| r3 | EV-001 | pass | - | last | 15/15 | 2.703 | 7652 |
| r3 | EV-002 | pass | - | last | 5/5 | 2.869 | 9863 |
| r3 | EV-003 | fail | other | last | 1/1 | 2.149 | 5309 |
| r3 | EV-004 | fail | other | last | 1/1 | 4.464 | 7730 |
| r3 | EV-005 | pass | - | last | 1/1 | 2.608 | 8033 |
| r3 | EV-006 | fail | other | last | 1/1 | 3.423 | 10007 |
| r3 | EV-007 | pass | - | last | 7/7 | 3.01 | 7142 |
| r3 | EV-008 | pass | - | last | 1/1 | 2.627 | 7198 |
| r3 | EV-009 | fail | other | last | 1/1 | 2.71 | 5558 |
| r3 | EV-010 | pass | - | last | 3/3 | 3.261 | 10765 |
| r3 | EV-011 | pass | - | last | 3/3 | 2.48 | 8407 |
| r3 | EV-012 | pass | - | last | 1/1 | 2.517 | 8602 |
| r3 | EV-013 | fail | other | last | 3/3 | 2.69 | 8457 |
| r3 | EV-014 | fail | other | last | 6/6 | 4.825 | 11989 |
| r3 | EV-015 | fail | other | last | 5/5 | 5.4 | 8941 |
| r3 | EV-016 | fail | other | last | 20/1 | 6.543 | 11929 |
| r3 | EV-017 | fail | other | last | 8/8 | 10.354 | 20580 |
| r3 | EV-018 | fail | other | last | 20/1 | 5.172 | 12890 |
| r3 | EV-019 | fail | other | last | 5/5 | 2.994 | 8635 |
| r3 | EV-020 | fail | other | last | 1/8 | 6.975 | 16256 |
| r3 | EV-021 | fail | other | last | 5/5 | 3.164 | 7069 |
| r3 | EV-022 | fail | other | last | 3/3 | 3.229 | 9350 |
| r3 | EV-023 | fail | other | last | 1/1 | 4.203 | 13555 |
| r3 | EV-024 | fail | other | last | 20/1 | 8.62 | 12601 |
| r3 | EV-025 | fail | other | last | 1/2 | 3.025 | 7093 |
| r3 | EV-026 | fail | other | last | 7/7 | 2.852 | 7396 |
| r3 | EV-027 | fail | other | last | 12/12 | 6.97 | 9699 |
| r3 | EV-028 | pass | - | last | 1/1 | 2.588 | 6860 |
| r3 | EV-029 | fail | other | last | 1/1 | 3.902 | 6086 |
| r3 | EV-030 | fail | other | last | 1/1 | 3.452 | 7710 |

## 声明（固定四条，W3 设计 §3.3）
1. 本报告是**系统级数字**：评测对象为"混合检索注入 + Agent"完整链路，含 few-shot 自举
   （qa_pair 片参与检索，11 条评测题原题在示例库中），不得表述为"模型的 NL2SQL 能力"（§1.3）。
2. temperature=0 仍存在服务端非确定性；主行（runs>1）为多次运行结果，W6 定稿前会复测（§3.2）。
3. 延迟绝对值受评测时网络波动影响，仅横向（题型间）与纵向（W6 前后对比）相对可比（§2.6）。
4. token 成本按当次实测累计（逐条 AIMessage.usage_metadata 累加；cache_read 单列，
   已含在 input 内不重复计）；人民币估算按当期牌价手填旁注，不写死单价（§2.6）。

