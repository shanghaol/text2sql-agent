# W3 基线的判定器 v2.1 离线复判记录（rejudge_v2.1）

> 本文件由 `eval/rejudge.py` 自动追加生成。复判不重跑 LLM：旧 detail 的 sql_calls/sql_used/final_answer_text + 数据集 join 回来的金标与 ordered，用 v2.1 判定器确定性重放（W6 设计 §1.3）。

## 复判轮次：2026-10-11 15:53:37 UTC（判定器 v2.1（无序比较排序键数值化分桶））

### detail_baseline_full_v2_20261010_161633.json（tag=baseline_full_v2，90 次运行）

- 旧判（v2.0，detail 原始判定）：SQL 级 41.1%（37/90）｜ 答案级 64.4%（pass 58 = sql 37 + text 21｜fail 32｜error 0）
- 新判（v2.1，本次离线复判）：SQL 级 42.2%（38/90）｜ 答案级 64.4%（pass 58 = sql 38 + text 20｜fail 32｜error 0）
- Δ：SQL 级 +1.1pp ｜ 答案级 +0.0pp ｜ 翻转 1 次

| id | run | 旧判 | 新判 | ordered | 成因 | 金标结果集 | 模型结果集 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| EV-006 | r3 | pass（text） | pass（sql） | False | replay_env | 5639 | 5639 |

### detail_no_rag_control_v2_20261010_161242.json（tag=no_rag_control_v2，30 次运行）

- 旧判（v2.0，detail 原始判定）：SQL 级 6.7%（2/30）｜ 答案级 43.3%（pass 13 = sql 2 + text 11｜fail 17｜error 0）
- 新判（v2.1，本次离线复判）：SQL 级 6.7%（2/30）｜ 答案级 43.3%（pass 13 = sql 2 + text 11｜fail 17｜error 0）
- Δ：SQL 级 +0.0pp ｜ 答案级 +0.0pp ｜ 翻转 0 次

### detail_multiturn_v2_20261010_162739.json（tag=multiturn_v2，12 次运行）

- 旧判（v2.0，detail 原始判定）：SQL 级 16.7%（2/12）｜ 答案级 91.7%（pass 11 = sql 2 + text 9｜fail 1｜error 0）
- 新判（v2.1，本次离线复判）：SQL 级 16.7%（2/12）｜ 答案级 91.7%（pass 11 = sql 2 + text 9｜fail 1｜error 0）
- Δ：SQL 级 +0.0pp ｜ 答案级 +0.0pp ｜ 翻转 0 次

**结论**：复判 132 次运行，翻转 1 次，其中判定器 v2.1 修复效应（sort_key_fix）为 0 次——全部翻转源于重放环境差异（replay_env：三级规则取用了不同 SQL），W3 判定数字在 v2.1 下依然稳健；环境差异逐条列于上表，成因点评见下。

### 人工成因点评（EV-006 r3 的 replay_env 归因链，2026-10-11 复查核实）

翻转的唯一一条（EV-006 r3，pass/text → pass/sql）**不是判定器 v2.1 的效应**，归因链如下：

1. 该 run 的调用序列有两条 SQL：call 1 与金标同构（`COUNT(*) FROM users WHERE NOT EXISTS(...)`，答 5,639）；
   call 2 是模型的多指标扩展查询（4 个标量子查询：总用户/下过单/下过单未支付/从未下单）。
2. **W3 当日（2026-10-10）**：call 2 重放成功（1 行 4 列），与金标（1 行 1 列）投影数不符 → SQL 级 fail
   → 文本回退命中 5,639 → pass_via=text。
3. **今日复判**：call 2 稳定触发 MySQL 3024（`maximum statement execution time exceeded`，
   连续 3 次复测均 ~10.0s 被服务端终止）——W5 于 2026-10-11 上线的 SQL 安全防线
   `SET SESSION max_execution_time=10000`（晚于基线运行日）改变了评测重放环境：
   该重查询在 10s 上限内跑不完，重放失败 → 三级规则回退 call 1 → 与金标结果集一致 → pass_via=sql。
4. 处置：答案级数字不变（58/90 = 64.4%）；SQL 级 38/90（42.2%）是重放环境差异所致，
   **不归因判定器 v2.1**——v2.1 口径下 W3 基线的诚实数字仍是 SQL 级 41.1% / 答案级 64.4%（零改口）。
   该发现另记一笔：评测重放面会被安全加固改变，跨周复用旧 detail 做对照时须注意重放环境同源性。

**判定器 v2.1（排序键数值化）对 W3 三份 detail 的修复效应实测为零翻转**——W3 命题里没有
"金标与模型等值数值格式不同 + 多行无序"的碰撞形态（设计 §1.1 用例是构造出来的边界），
修复的意义在于堵住未来 50 条扩题后的假阴性面，而非改写历史数字。
