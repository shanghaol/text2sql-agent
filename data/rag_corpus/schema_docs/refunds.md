# 表：退款记录表（refunds）

## DDL
```sql
CREATE TABLE refunds (
    id          BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '退款 ID',
    payment_id  BIGINT UNSIGNED NOT NULL COMMENT '对应支付 → payments.id',
    order_id    BIGINT UNSIGNED NOT NULL COMMENT '冗余订单 ID（= payments.order_id，免 JOIN 直接统计）',
    amount      DECIMAL(10,2)   NOT NULL COMMENT '退款金额（元，≤ 对应支付金额；= 全额时订单状态置 refunded）',
    reason      VARCHAR(64)     NOT NULL COMMENT '退款原因（质量问题/七日无理由/物流问题/错拍不想要/其他，加权）',
    refunded_at DATETIME        NOT NULL COMMENT '退款完成时间（≥ 支付时间 + 数小时，≤ 数据期终点）',
    PRIMARY KEY (id),
    KEY idx_refunds_payment (payment_id),
    KEY idx_refunds_order (order_id),
    KEY idx_refunds_refunded_at (refunded_at),
    CONSTRAINT fk_refunds_payment FOREIGN KEY (payment_id) REFERENCES payments (id)
) ENGINE=InnoDB COMMENT='退款记录（一单至多一条；部分退款订单状态仍为 completed）';
```

## 列说明
| 列 | 类型 | 业务含义 | 示例 |
| --- | --- | --- | --- |
| id | BIGINT UNSIGNED | 退款 ID，主键自增 | 80001 |
| payment_id | BIGINT UNSIGNED | 对应支付，外键 → payments.id | 400001 |
| order_id | BIGINT UNSIGNED | 冗余订单 ID，恒等于 payments.order_id，免 JOIN 直接按订单统计 | 500001 |
| amount | DECIMAL(10,2) | 退款金额（元），≤ 对应支付金额；全额退款（= 支付金额）时订单状态置 refunded，部分退款（20%-90%）时状态保持 completed（勾稽 R6） | 736.83 |
| reason | VARCHAR(64) | 退款原因，五类加权：质量问题 30% / 七日无理由 25% / 物流问题 20% / 错拍不想要 15% / 其他 10% | 质量问题 |
| refunded_at | DATETIME | 退款完成时间，≥ 支付时间 + 数小时；按"退款发生期"统计用本列，数据期 2025-10-01 至 2026-09-30 | 2026-03-20 09:41:07 |

## 示例数据（3 行，来自库中真实数据）
以下为库中真实数据（`SELECT * FROM refunds LIMIT 3` 的查询结果）：

| id | payment_id | order_id | amount | reason | refunded_at |
| --- | --- | --- | --- | --- | --- |
| 1 | 2 | 2 | 267.88 | 质量问题 | 2025-10-08 13:28:16 |
| 2 | 5 | 6 | 837.74 | 七日无理由 | 2025-10-03 05:41:21 |
| 3 | 6 | 7 | 3835.16 | 质量问题 | 2025-10-04 00:55:49 |

## 关联关系
- refunds.payment_id → payments.id（多对一：一条退款对应一条支付；一单至多一条退款）
- refunds.order_id → orders.id（多对一；冗余列，值恒等于 payments.order_id）
- 本表（约 9,300 行）中约 60% 为全额退款、40% 为部分退款。

## 该表能回答的典型问题
- 每月退款金额/笔数趋势？
- 各退款原因的分布？
- 退款率是多少（金额口径/订单口径）？
- 全额退款与部分退款各占多少？

## 注意事项
- 退款率必须查本表：orders.status='refunded' 只覆盖全额退款，部分退款订单状态仍是 completed，按状态数会漏掉约四成退款（见口径文档"退款率"）。
- refunded_at 是退款完成时间，与支付时间（payments.paid_at）不是同一时间轴；按月统计退款时用 refunded_at 定期。
- amount = 对应支付金额 ⇔ 订单状态为 refunded；amount < 支付金额即部分退款，可据此区分两类退款。
