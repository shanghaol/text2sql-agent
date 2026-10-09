<!-- 用途：few-shot 金标问答对（RAG 三路语料之一），检索命中后以"问 + 金标 SQL + 说明"整条注入 prompt 供模型模仿方言与口径。
     切分方式：按 "### QA-xxx" 三级标题切条，每条一片，source_id = qa_001 式三位序号。
     文末"待真库核对清单"为工程核对项，非语料内容：解析器应按其 "## " 标题截断，或 T2 收尾核对通过后整节删除。 -->

# few-shot 金标问答对（qa_pairs）

> 11 条覆盖五类题型：单表 / 多表 JOIN / 聚合统计 / 时间窗口 / 口径陷阱。SQL 为 MySQL 8 方言（mart 库，只读）；时间过滤一律左闭右开；相对时间以数据锚点 '2026-09-30' 为"今天"；数据期 2025-10-01 至 2026-09-30。

### QA-001
**问**：2026 年 3 月每天的订单量是多少？
**金标 SQL**：
```sql
SELECT DATE_FORMAT(created_at, '%Y-%m-%d') AS order_date, COUNT(*) AS order_count
FROM orders
WHERE created_at >= '2026-03-01' AND created_at < '2026-04-01'
GROUP BY order_date
ORDER BY order_date;
```
**说明**：时间过滤用左闭右开区间（>= 当月 1 日 0 点、< 次月 1 日 0 点），规避 BETWEEN 在边界时刻的归属歧义；按天分组用 MySQL 的 DATE_FORMAT，不用 SQLite 的 strftime。

### QA-002
**问**：销售额最高的前 5 个城市是哪些？
**金标 SQL**：
```sql
SELECT u.city, SUM(p.amount) AS sales_amount
FROM payments p
JOIN orders o ON o.id = p.order_id
JOIN users u ON u.id = o.user_id
WHERE p.paid_at >= '2025-10-01' AND p.paid_at < '2026-10-01'
GROUP BY u.city
ORDER BY sales_amount DESC
LIMIT 5;
```
**说明**：销售额口径走 payments.amount（支付成功金额），不能用 SUM(orders.order_amount)（含未支付订单）；经 orders 中转两次 JOIN 才能把支付连到用户城市（口径 1/2）。

### QA-003
**问**：上个月的 GMV 是多少？
**金标 SQL**：
```sql
SELECT SUM(amount) AS gmv
FROM payments
WHERE paid_at >= '2026-09-01' AND paid_at < '2026-10-01';
```
**说明**：数据截至 2026-09-30，"上个月"即 2026-09；GMV = SUM(payments.amount) 按支付时间定期，不用 SUM(orders.order_amount)（会含 pending/cancelled 未支付订单，口径 1）。

### QA-004
**问**：9 月的退款率是多少（金额口径）？
**金标 SQL**：
```sql
SELECT ROUND(SUM(r.amount) / SUM(p.amount), 4) AS refund_rate
FROM payments p
LEFT JOIN refunds r ON r.payment_id = p.id
WHERE p.paid_at >= '2026-09-01' AND p.paid_at < '2026-10-01';
```
**说明**：退款统计必须查 refunds 表——orders.status='refunded' 只覆盖全额退款，部分退款订单状态仍是 completed，按状态算会漏；金额口径 = 当期支付订单中退款金额占比（口径 6）。

### QA-005
**问**：买过数码类目商品的女性用户有多少？
**金标 SQL**：
```sql
SELECT COUNT(DISTINCT o.user_id) AS female_digital_users
FROM orders o
JOIN order_items oi ON oi.order_id = o.id
JOIN products p ON p.id = oi.product_id
JOIN users u ON u.id = o.user_id
WHERE p.category = '数码'
  AND u.gender = '女';
```
**说明**：类目在商品表，必须经 order_items 才能从订单定位到类目（三表 JOIN）；一个用户可有多条数码明细，COUNT(DISTINCT user_id) 去重，否则是"人次"不是"人数"。

### QA-006
**问**：注册后从未成功支付的用户有多少？
**金标 SQL**：
```sql
SELECT COUNT(*) AS never_paid_users
FROM users u
WHERE NOT EXISTS (
    SELECT 1
    FROM orders o
    JOIN payments p ON p.order_id = o.id
    WHERE o.user_id = u.id
);
```
**说明**："成功支付"要落到 payments 表判断（有支付记录 ⇔ 状态 ∈ {paid, shipped, completed, refunded}）；NOT EXISTS 语义直接，优于 LEFT JOIN + IS NULL 的写法。

### QA-007
**问**：近 7 天每天的支付金额是多少？
**金标 SQL**：
```sql
SELECT DATE_FORMAT(paid_at, '%Y-%m-%d') AS pay_date,
       SUM(amount) AS pay_amount
FROM payments
WHERE paid_at >= DATE_SUB('2026-09-30', INTERVAL 6 DAY)
  AND paid_at < DATE_ADD('2026-09-30', INTERVAL 1 DAY)
GROUP BY pay_date
ORDER BY pay_date;
```
**说明**：数据是静态快照，相对时间以锚点 '2026-09-30' 为"今天"（用 NOW() 会查到 0 行）；含当天共 7 天故 INTERVAL 6 DAY 起算，上界取次日零点的左闭右开，避免漏掉当天最后时刻。

### QA-008
**问**：2026 年 6 月的客单价是多少？
**金标 SQL**：
```sql
SELECT ROUND(SUM(amount) / COUNT(DISTINCT order_id), 2) AS avg_order_value
FROM payments
WHERE paid_at >= '2026-06-01' AND paid_at < '2026-07-01';
```
**说明**：客单价分母是支付订单数（DISTINCT order_id 去重），不是订单总数（含未支付），也不是用户数（那是 ARPU）——分子分母同源都在 payments（口径 3）。

### QA-009
**问**：2026 年上半年的复购率是多少？
**金标 SQL**：
```sql
SELECT ROUND(
         COUNT(*) / (SELECT COUNT(DISTINCT user_id) FROM orders
                     WHERE created_at >= '2026-01-01' AND created_at < '2026-07-01'),
         4) AS repurchase_rate
FROM (
    SELECT user_id
    FROM orders
    WHERE created_at >= '2026-01-01' AND created_at < '2026-07-01'
    GROUP BY user_id
    HAVING COUNT(*) >= 2
) t;
```
**说明**：复购率 = 期内下单 ≥2 次的用户 ÷ 期内下单用户；分子按下单次数 HAVING 过滤（次数不是件数），分母是全体下单用户，两层要分开算（口径 5）。

### QA-010
**问**：退款金额最高的 3 笔订单是哪些？
**金标 SQL**：
```sql
SELECT order_id, amount, reason, refunded_at
FROM refunds
ORDER BY amount DESC
LIMIT 3;
```
**说明**：refunds.order_id 是冗余列，免 JOIN payments 即可按订单统计；简单排序取 TOP 用单表查询最稳，不易在 JOIN 中引入重复行。

### QA-011
**问**：各支付方式的订单占比是多少？
**金标 SQL**：
```sql
SELECT method,
       COUNT(*) AS order_count,
       ROUND(COUNT(*) * 100.0 / (SELECT COUNT(*) FROM payments), 2) AS pct
FROM payments
GROUP BY method
ORDER BY order_count DESC;
```
**说明**：一笔成功订单恰一条支付，COUNT(*) 即订单数；分母用子查询取支付总数，乘 100.0 避免整数除法截断，占比保留两位小数、按数量降序排列。
