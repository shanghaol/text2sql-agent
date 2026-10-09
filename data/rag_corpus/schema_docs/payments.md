# 表：支付流水表（payments）

## DDL
```sql
CREATE TABLE payments (
    id       BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '支付 ID',
    order_id BIGINT UNSIGNED NOT NULL COMMENT '对应订单 → orders.id（唯一）',
    amount   DECIMAL(10,2)   NOT NULL COMMENT '实付金额（元）= orders.order_amount（简化：无运费/优惠券）',
    method   ENUM('微信','支付宝','银联') NOT NULL COMMENT '支付方式',
    paid_at  DATETIME        NOT NULL COMMENT '支付成功时间（≥ 订单 created_at，隔数分钟至数小时）',
    PRIMARY KEY (id),
    UNIQUE KEY uk_payments_order (order_id),
    KEY idx_payments_paid_at (paid_at),
    CONSTRAINT fk_payments_order FOREIGN KEY (order_id) REFERENCES orders (id)
) ENGINE=InnoDB COMMENT='支付流水（成功支付一条；pending/cancelled 订单无记录）';
```

## 列说明
| 列 | 类型 | 业务含义 | 示例 |
| --- | --- | --- | --- |
| id | BIGINT UNSIGNED | 支付 ID，主键自增，被 refunds.payment_id 引用 | 400001 |
| order_id | BIGINT UNSIGNED | 对应订单，外键 → orders.id，有唯一键约束（一单至多一条支付） | 500001 |
| amount | DECIMAL(10,2) | 实付金额（元），恒等于 orders.order_amount（简化：无运费/优惠券，勾稽 R4） | 2456.10 |
| method | ENUM('微信','支付宝','银联') | 支付方式，分布约微信 55% / 支付宝 35% / 银联 10% | 支付宝 |
| paid_at | DATETIME | 支付成功时间，≥ 订单 created_at（隔数分钟至数小时）；数据期 2025-10-01 至 2026-09-30 | 2026-03-15 15:03:52 |

## 示例数据（3 行，来自库中真实数据）
以下为库中真实数据（`SELECT * FROM payments LIMIT 3` 的查询结果）：

| id | order_id | amount | method | paid_at |
| --- | --- | --- | --- | --- |
| 1 | 1 | 3306.89 | 支付宝 | 2025-10-01 01:26:40 |
| 2 | 2 | 267.88 | 微信 | 2025-10-01 02:12:14 |
| 3 | 3 | 297.02 | 微信 | 2025-10-01 05:39:57 |

## 关联关系
- payments.order_id → orders.id（一对一：一笔成功订单恰一条支付，order_id 唯一键；pending/cancelled 订单无本表记录）
- payments.id ← refunds.payment_id（一对零或一：一条支付至多对应一条退款）
- 本表（约 89,000 行）是所有"钱"口径（GMV/销售额/客单价/ARPU）的事实表。

## 该表能回答的典型问题
- 每月/每日 GMV（支付金额）趋势？
- 各支付方式的笔数与金额占比？
- 某订单实付多少钱、何时支付成功？

## 注意事项
- pending/cancelled 订单没有支付记录，统计支付金额（GMV、销售额）必须查本表 SUM(amount)，不能 SUM(orders.order_amount)——后者含未付款订单（见口径文档"GMV"）。
- 时间口径用 paid_at（支付成功时间）而非订单 created_at，两者可能跨日，按月统计时结果不同。
- 一笔成功订单恰一条支付，COUNT(*) 即支付订单数，无需去重；有支付记录 ⇔ 订单状态 ∈ {paid, shipped, completed, refunded}。
