# 表：订单明细表（order_items）

## DDL
```sql
CREATE TABLE order_items (
    id            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '明细 ID',
    order_id      BIGINT UNSIGNED NOT NULL COMMENT '所属订单 → orders.id',
    product_id    BIGINT UNSIGNED NOT NULL COMMENT '商品 → products.id',
    quantity      INT UNSIGNED    NOT NULL COMMENT '购买数量（1-5）',
    unit_price    DECIMAL(10,2)   NOT NULL COMMENT '成交单价（元）= ROUND(products.price × discount_rate, 2)，下单时快照',
    discount_rate DECIMAL(3,2)    NOT NULL COMMENT '成交折扣率（1.00/0.95/0.90/0.85/0.80 加权），解释同商品成交价差异',
    sale_amount   DECIMAL(10,2)   NOT NULL COMMENT '行小计（元）= unit_price × quantity',
    PRIMARY KEY (id),
    KEY idx_order_items_order (order_id),
    KEY idx_order_items_product (product_id),
    CONSTRAINT fk_items_order   FOREIGN KEY (order_id)   REFERENCES orders (id),
    CONSTRAINT fk_items_product FOREIGN KEY (product_id) REFERENCES products (id)
) ENGINE=InnoDB COMMENT='订单明细表（一行=订单里一种商品）';
```

## 列说明
| 列 | 类型 | 业务含义 | 示例 |
| --- | --- | --- | --- |
| id | BIGINT UNSIGNED | 明细 ID，主键自增 | 700101 |
| order_id | BIGINT UNSIGNED | 所属订单，外键 → orders.id；每单 1-3 条明细 | 500001 |
| product_id | BIGINT UNSIGNED | 商品，外键 → products.id | 3051 |
| quantity | INT UNSIGNED | 购买数量，取值 1-5，偏小值分布 | 2 |
| unit_price | DECIMAL(10,2) | 成交单价（元），下单时按折扣快照，恒不等于商品原价打折前的动态价 | 1169.10 |
| discount_rate | DECIMAL(3,2) | 成交折扣率，取值 1.00/0.95/0.90/0.85/0.80 加权抽样 | 0.90 |
| sale_amount | DECIMAL(10,2) | 行小计（元）= unit_price × quantity；同单各行求和恒等于 orders.order_amount（勾稽 R1/R3） | 2338.20 |

## 示例数据（3 行，来自库中真实数据）
（待数据导入后填充真实查询结果——T2 收尾项）

## 关联关系
- order_items.order_id → orders.id（多对一：一条明细属于一笔订单）
- order_items.product_id → products.id（多对一：一条明细对应一个商品）
- 本表是事实表（约 150,000 行），连接 orders 与 products，是商品级分析（畅销榜/类目销量/动销）的必经之路。

## 该表能回答的典型问题
- 最畅销商品 TOP10（按销量或销售额）？
- 平均每单买几种商品、几件？连带率是多少？
- 某订单具体买了哪些商品、打折力度如何？
- 各类目/各商品的销量构成？（需 JOIN products）

## 注意事项
- 订单总额勿用本表重复聚合：orders.order_amount 已汇总（两者按分严格相等，勾稽 R1），本表用于商品级下钻而非订单级汇总。
- "复购"按下单次数（orders 行数）计，不是本表行数也不是 quantity 件数（见口径文档"复购率"）；SUM(quantity)/订单数 是"连带率"（见口径文档"连带率"）。
- 统计商品售出（动销率分子）用 COUNT(DISTINCT product_id)，且时间要以关联订单的 created_at 圈期。
