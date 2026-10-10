# W3 基线评测报告：multiturn（2026-10-10 15:47:28）

## 运行配置
- 数据集：`eval\dataset\eval_v1_multiturn.jsonl`（4 条，多轮（判定只看最后一轮，§2.7））
- top_n=8 ｜ runs=3 ｜ 本次共 12 次题级运行
- 模型：deepseek-flash（temperature=0）｜ 快照锚点：2026-09-30
- 检索参数：top_k_each=20，RRF k=60（与线上 hybrid_retriever 默认一致，§2.3 同构）

## 四指标汇总

### 执行准确率（比较重放结果集，不看 SQL 文本）
- overall：**16.7%**（pass 2 / fail 10 / error 0，共 12 次）
- 各 run：r1=25.0% ｜ r2=0.0% ｜ r3=25.0%（三次结果不一致的题见 flaky 节，§3.2 不机械取众数）
- 多轮题不设题型/分层维度（MT schema 无 question_type，§1.7 独立报告节）

### Recall@8
- 整体逐片 Recall：**75.0%**（Σ|∩|=18 / Σ|expected|=24）
- 题级全命中率（expected 全部命中）：50.0%（6/12 题）
- 分 doc_type：schema 50.0% ｜ glossary 100.0% ｜ qa_pair 100.0%
- 末轮注入口径（§2.7：取最后一轮的注入，q1 铺垫注入不计入）

### 延迟（wall-clock：search 前起、invoke 返回止，含全部工具往返）
- mean 5.9s ｜ median 5.1s ｜ P95 9.2s ｜ max 9.3s
- P95 口径：statistics.quantiles(n=20, method='inclusive')[18]（线性插值，§8-5 拍板）
- 延迟绝对值受网络波动影响，仅相对可比（见声明③）

### token 成本（累加全部 AIMessage.usage_metadata，计费口径 §2.6）
- 单题均值：input 23265 ｜ output 1411 ｜ total 24677（cache_read 20533 单列，不重复计）
- 总量：input 279182 ｜ output 16937 ｜ total 296119（cache_read 246400）；人民币估算按当期牌价手填旁注

## 对照行对比表
（本次运行未提供 --compare 明细文件；对照行对比由 T3 汇主行与对照行两份 detail JSON 后生成。）

## 错答模式统计
| failure_mode | 次数 | 典型样本（case id） |
| --- | --- | --- |
| other | 10 | MT-02、MT-03、MT-04 |

trap:<id> 与 valid_alt_gloss 分开统计（§4.3）：前者是真陷阱，后者是合法口径但答非所问；other 为错出新花样，人工看明细 JSON 归类后可回填登记表。

## flaky 题记录
| id | 各 run 判定 |
| --- | --- |
| MT-01 | pass → fail → pass |

flaky 题不机械取众数：人工复盘明细 JSON（三次的 SQL 差在哪），结论记入 experiment-log（§3.2）。

## 多轮评测节
- 多轮准确率（只判最后一轮）：**16.7%**（pass 2 / fail 10 / error 0）
| id | 轮数 | 判定 | failure_mode | 末轮 expected | 末轮命中 |
| --- | --- | --- | --- | --- | --- |
| MT-01 | 2 | pass | - | 2 片 | glossary:yoy_mom、qa_pair:qa_003 |
| MT-02 | 2 | fail | other | 2 片 | glossary:avg_order_value |
| MT-03 | 2 | fail | other | 3 片 | glossary:refund_rate、schema:refunds |
| MT-04 | 2 | fail | other | 1 片 | schema:payments |
| MT-01 | 2 | fail | other | 2 片 | glossary:yoy_mom、qa_pair:qa_003 |
| MT-02 | 2 | fail | other | 2 片 | glossary:avg_order_value |
| MT-03 | 2 | fail | other | 3 片 | glossary:refund_rate、schema:refunds |
| MT-04 | 2 | fail | other | 1 片 | schema:payments |
| MT-01 | 2 | pass | - | 2 片 | glossary:yoy_mom、qa_pair:qa_003 |
| MT-02 | 2 | fail | other | 2 片 | glossary:avg_order_value |
| MT-03 | 2 | fail | other | 3 片 | glossary:refund_rate、schema:refunds |
| MT-04 | 2 | fail | other | 1 片 | schema:payments |

### query 拼接行为观察（每轮 query = previous_human_message + 当前输入，§2.7）
- **MT-01**
  - 轮 1：query=`上个月 GMV 多少？` → 注入 8 片[qa_pair:qa_003、qa_pair:qa_001、qa_pair:qa_008、glossary:gmv、glossary:arpu、qa_pair:qa_007、glossary:yoy_mom、qa_pair:qa_009]，工具调用 1 次
  - 轮 2：query=`上个月 GMV 多少？ / 那环比呢` → 注入 8 片[glossary:yoy_mom、qa_pair:qa_003、qa_pair:qa_008、qa_pair:qa_001、schema:products、glossary:arpu、qa_pair:qa_009、glossary:gmv]，工具调用 2 次
- **MT-02**
  - 轮 1：query=`2026 年 6 月的客单价是多少？` → 注入 8 片[qa_pair:qa_008、qa_pair:qa_001、glossary:gmv、qa_pair:qa_009、glossary:avg_order_value、schema:order_items、qa_pair:qa_003、qa_pair:qa_007]，工具调用 2 次
  - 轮 2：query=`2026 年 6 月的客单价是多少？ / 那 7 月呢` → 注入 8 片[qa_pair:qa_008、qa_pair:qa_001、qa_pair:qa_009、qa_pair:qa_007、glossary:avg_order_value、glossary:gmv、qa_pair:qa_003、schema:order_items]，工具调用 3 次
- **MT-03**
  - 轮 1：query=`9 月的退款率是多少（金额口径）？` → 注入 8 片[qa_pair:qa_004、glossary:refund_rate、schema:refunds、glossary:net_sales、qa_pair:qa_008、qa_pair:qa_010、qa_pair:qa_003、qa_pair:qa_009]，工具调用 2 次
  - 轮 2：query=`9 月的退款率是多少（金额口径）？ / 换成订单口径呢` → 注入 8 片[qa_pair:qa_004、glossary:refund_rate、schema:refunds、glossary:net_sales、qa_pair:qa_008、qa_pair:qa_010、schema:order_items、schema:orders]，工具调用 4 次
- **MT-04**
  - 轮 1：query=`各支付方式的订单占比是多少？` → 注入 8 片[qa_pair:qa_011、glossary:avg_order_value、qa_pair:qa_008、schema:payments、glossary:gmv、schema:order_items、qa_pair:qa_003、glossary:pay_conversion]，工具调用 1 次
  - 轮 2：query=`各支付方式的订单占比是多少？ / 银联的占比是多少` → 注入 8 片[qa_pair:qa_011、schema:payments、glossary:avg_order_value、qa_pair:qa_008、schema:order_items、qa_pair:qa_003、glossary:gmv、qa_pair:qa_010]，工具调用 1 次
- **MT-01**
  - 轮 1：query=`上个月 GMV 多少？` → 注入 8 片[qa_pair:qa_003、qa_pair:qa_001、qa_pair:qa_008、glossary:gmv、glossary:arpu、qa_pair:qa_007、glossary:yoy_mom、qa_pair:qa_009]，工具调用 1 次
  - 轮 2：query=`上个月 GMV 多少？ / 那环比呢` → 注入 8 片[glossary:yoy_mom、qa_pair:qa_003、qa_pair:qa_008、qa_pair:qa_001、schema:products、glossary:arpu、qa_pair:qa_009、glossary:gmv]，工具调用 2 次
- **MT-02**
  - 轮 1：query=`2026 年 6 月的客单价是多少？` → 注入 8 片[qa_pair:qa_008、qa_pair:qa_001、glossary:gmv、qa_pair:qa_009、glossary:avg_order_value、schema:order_items、qa_pair:qa_003、qa_pair:qa_007]，工具调用 1 次
  - 轮 2：query=`2026 年 6 月的客单价是多少？ / 那 7 月呢` → 注入 8 片[qa_pair:qa_008、qa_pair:qa_001、qa_pair:qa_009、qa_pair:qa_007、glossary:avg_order_value、glossary:gmv、qa_pair:qa_003、schema:order_items]，工具调用 2 次
- **MT-03**
  - 轮 1：query=`9 月的退款率是多少（金额口径）？` → 注入 8 片[qa_pair:qa_004、glossary:refund_rate、schema:refunds、glossary:net_sales、qa_pair:qa_008、qa_pair:qa_010、qa_pair:qa_003、qa_pair:qa_009]，工具调用 2 次
  - 轮 2：query=`9 月的退款率是多少（金额口径）？ / 换成订单口径呢` → 注入 8 片[qa_pair:qa_004、glossary:refund_rate、schema:refunds、glossary:net_sales、qa_pair:qa_008、qa_pair:qa_010、schema:order_items、schema:orders]，工具调用 4 次
- **MT-04**
  - 轮 1：query=`各支付方式的订单占比是多少？` → 注入 8 片[qa_pair:qa_011、glossary:avg_order_value、qa_pair:qa_008、schema:payments、glossary:gmv、schema:order_items、qa_pair:qa_003、glossary:pay_conversion]，工具调用 1 次
  - 轮 2：query=`各支付方式的订单占比是多少？ / 银联的占比是多少` → 注入 8 片[qa_pair:qa_011、schema:payments、glossary:avg_order_value、qa_pair:qa_008、schema:order_items、qa_pair:qa_003、glossary:gmv、qa_pair:qa_010]，工具调用 1 次
- **MT-01**
  - 轮 1：query=`上个月 GMV 多少？` → 注入 8 片[qa_pair:qa_003、qa_pair:qa_001、qa_pair:qa_008、glossary:gmv、glossary:arpu、qa_pair:qa_007、glossary:yoy_mom、qa_pair:qa_009]，工具调用 1 次
  - 轮 2：query=`上个月 GMV 多少？ / 那环比呢` → 注入 8 片[glossary:yoy_mom、qa_pair:qa_003、qa_pair:qa_008、qa_pair:qa_001、schema:products、glossary:arpu、qa_pair:qa_009、glossary:gmv]，工具调用 2 次
- **MT-02**
  - 轮 1：query=`2026 年 6 月的客单价是多少？` → 注入 8 片[qa_pair:qa_008、qa_pair:qa_001、glossary:gmv、qa_pair:qa_009、glossary:avg_order_value、schema:order_items、qa_pair:qa_003、qa_pair:qa_007]，工具调用 1 次
  - 轮 2：query=`2026 年 6 月的客单价是多少？ / 那 7 月呢` → 注入 8 片[qa_pair:qa_008、qa_pair:qa_001、qa_pair:qa_009、qa_pair:qa_007、glossary:avg_order_value、glossary:gmv、qa_pair:qa_003、schema:order_items]，工具调用 2 次
- **MT-03**
  - 轮 1：query=`9 月的退款率是多少（金额口径）？` → 注入 8 片[qa_pair:qa_004、glossary:refund_rate、schema:refunds、glossary:net_sales、qa_pair:qa_008、qa_pair:qa_010、qa_pair:qa_003、qa_pair:qa_009]，工具调用 2 次
  - 轮 2：query=`9 月的退款率是多少（金额口径）？ / 换成订单口径呢` → 注入 8 片[qa_pair:qa_004、glossary:refund_rate、schema:refunds、glossary:net_sales、qa_pair:qa_008、qa_pair:qa_010、schema:order_items、schema:orders]，工具调用 4 次
- **MT-04**
  - 轮 1：query=`各支付方式的订单占比是多少？` → 注入 8 片[qa_pair:qa_011、glossary:avg_order_value、qa_pair:qa_008、schema:payments、glossary:gmv、schema:order_items、qa_pair:qa_003、glossary:pay_conversion]，工具调用 1 次
  - 轮 2：query=`各支付方式的订单占比是多少？ / 银联的占比是多少` → 注入 8 片[qa_pair:qa_011、schema:payments、glossary:avg_order_value、qa_pair:qa_008、schema:order_items、qa_pair:qa_003、glossary:gmv、qa_pair:qa_010]，工具调用 2 次
- 观察：9/12 个 case 的最后一轮注入集合与首轮不同（query 拼接改变了检索结果——多轮检索歧义量的量化，W2 §3.6 已知妥协的实测数据）

## 逐题结果
| run | id | 判定 | failure_mode | sql_source | 行数(金/模) | 延迟s | tokens(total) |
| --- | --- | --- | --- | --- | --- | --- | --- |
| r1 | MT-01 | pass | - | last | 2/2 | 4.521 | 17971 |
| r1 | MT-02 | fail | other | last | 1/1 | 6.034 | 30098 |
| r1 | MT-03 | fail | other | last | 1/1 | 8.896 | 28558 |
| r1 | MT-04 | fail | other | last | 1/3 | 3.68 | 21125 |
| r2 | MT-01 | fail | other | last | 2/2 | 4.831 | 18352 |
| r2 | MT-02 | fail | other | last | 1/1 | 4.793 | 22308 |
| r2 | MT-03 | fail | other | last | 1/1 | 9.138 | 35031 |
| r2 | MT-04 | fail | other | last | 1/3 | 4.065 | 21004 |
| r3 | MT-01 | pass | - | last | 2/2 | 5.149 | 18519 |
| r3 | MT-02 | fail | other | last | 1/1 | 5.047 | 22104 |
| r3 | MT-03 | fail | other | last | 1/1 | 9.301 | 35100 |
| r3 | MT-04 | fail | other | last | 1/1 | 5.307 | 25949 |

## 声明（固定四条，W3 设计 §3.3）
1. 本报告是**系统级数字**：评测对象为"混合检索注入 + Agent"完整链路，含 few-shot 自举
   （qa_pair 片参与检索，11 条评测题原题在示例库中），不得表述为"模型的 NL2SQL 能力"（§1.3）。
2. temperature=0 仍存在服务端非确定性；主行（runs>1）为多次运行结果，W6 定稿前会复测（§3.2）。
3. 延迟绝对值受评测时网络波动影响，仅横向（题型间）与纵向（W6 前后对比）相对可比（§2.6）。
4. token 成本按当次实测累计（逐条 AIMessage.usage_metadata 累加；cache_read 单列，
   已含在 input 内不重复计）；人民币估算按当期牌价手填旁注，不写死单价（§2.6）。

