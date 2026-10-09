# 表：订单主表（orders）

## DDL
```sql
CREATE TABLE orders (
    id           BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '订单 ID',
    order_no     CHAR(18)        NOT NULL COMMENT '业务订单号（SO+yyyyMMdd+8位序号），全局唯一',
    user_id      BIGINT UNSIGNED NOT NULL COMMENT '下单用户 → users.id',
    status       ENUM('pending','paid','shipped','completed','cancelled','refunded')
                                 NOT NULL DEFAULT 'pending'
                                 COMMENT '订单状态：pending待支付/paid已支付/shipped已发货/completed已完成/cancelled已取消/refunded已全额退款',
    order_amount DECIMAL(10,2)   NOT NULL COMMENT '订单应付金额（元）= SUM(订单项 sale_amount)，冗余列',
    item_count   INT UNSIGNED    NOT NULL COMMENT '订单件数 = SUM(订单项 quantity)，冗余列',
    created_at   DATETIME        NOT NULL COMMENT '下单时间',
    PRIMARY KEY (id),
    UNIQUE KEY uk_orders_order_no (order_no),
    KEY idx_orders_user_id (user_id),
    KEY idx_orders_status (status),
    KEY idx_orders_created_at (created_at),
    CONSTRAINT fk_orders_user FOREIGN KEY (user_id) REFERENCES users (id)
) ENGINE=InnoDB COMMENT='订单主表（表头）';
```

## 列说明
| 列 | 类型 | 业务含义 | 示例 |
| --- | --- | --- | --- |
| id | BIGINT UNSIGNED | 订单 ID，主键自增，被 order_items/payments/refunds 引用 | 500001 |
| order_no | CHAR(18) | 业务订单号（SO+日期+8 位序号），全局唯一 | SO2026031500012345 |
| user_id | BIGINT UNSIGNED | 下单用户，外键 → users.id | 10001 |
| status | ENUM 六值 | 订单状态：pending 待支付 / paid 已支付 / shipped 已发货 / completed 已完成 / cancelled 已取消 / refunded 已全额退款；分布约 completed 65% / shipped 9% / paid 7% / refunded 8% / cancelled 8% / pending 3% | completed |
| order_amount | DECIMAL(10,2) | 订单应付金额（元）= SUM(该单 sale_amount)，冗余汇总列（勾稽 R1） | 2456.10 |
| item_count | INT UNSIGNED | 订单件数 = SUM(该单 quantity)，冗余汇总列（勾稽 R2） | 3 |
| created_at | DATETIME | 下单时间，数据期 2025-10-01 至 2026-09-30，恒晚于用户 registered_at | 2026-03-15 14:22:31 |

## 示例数据（3 行，来自库中真实数据）
（待数据导入后填充真实查询结果——T2 收尾项）

## 关联关系
- orders.user_id → users.id（多对一：一笔订单恰属于一个用户）
- orders.id ← order_items.order_id（一对多：一笔订单含 1-3 条明细）
- orders.id ← payments.order_id（一对一：有支付记录（paid/shipped/completed/refunded）的订单恰一条支付；pending/cancelled 无）
- orders.id ← refunds.order_id（一对零或一：一单至多一条退款）

## 该表能回答的典型问题
- 每月/每日订单量趋势？双 11、618 的峰值有多明显？
- 各状态订单的占比与数量？
- 哪些用户下单最多？某用户的所有订单？

## 注意事项
- status='refunded' 仅表示"已全额退款"；部分退款的订单状态仍是 completed——统计退款必须查 refunds 表（见口径文档"退款率"）。
- 统计"支付成功的钱"不要直接 SUM(order_amount)：pending/cancelled 订单没有支付记录，会把未付款也算进来；金额口径应改查 SUM(payments.amount)（见口径文档"GMV"）。order_amount 适合"订单应收"视角。
- 一笔订单一行，COUNT(*) 即订单数；订单级金额/件数已冗余在本表，常规订单统计无需 JOIN order_items。
