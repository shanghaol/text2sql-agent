# 表：商品表（products）

## DDL
```sql
CREATE TABLE products (
    id        BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '商品 ID',
    name      VARCHAR(128)    NOT NULL COMMENT '商品名（虚构品牌+类目词+属性词模板拼接）',
    category  VARCHAR(16)     NOT NULL COMMENT '一级类目（数码/家居/服饰/美妆/食品/母婴/运动户外/图书，共 8 类）',
    brand     VARCHAR(32)     NOT NULL COMMENT '品牌（每类目 4-5 个虚构品牌，避免撞真实品牌）',
    price     DECIMAL(10,2)   NOT NULL COMMENT '原价（元）。简化：不做调价历史',
    listed_at DATE            NOT NULL COMMENT '上架日期（恒早于数据期起点）',
    PRIMARY KEY (id),
    KEY idx_products_category (category),
    KEY idx_products_brand (brand)
) ENGINE=InnoDB COMMENT='商品表';
```

## 列说明
| 列 | 类型 | 业务含义 | 示例 |
| --- | --- | --- | --- |
| id | BIGINT UNSIGNED | 商品 ID，主键自增，被 order_items.product_id 引用 | 3051 |
| name | VARCHAR(128) | 商品名，"品牌 + 类目词 + 属性词"模板拼接 | 云杉 数码 降噪蓝牙耳机 |
| category | VARCHAR(16) | 一级类目，共 8 类：数码/家居/服饰/美妆/食品/母婴/运动户外/图书（每类 250 个） | 数码 |
| brand | VARCHAR(32) | 品牌，每类目 4-5 个虚构品牌 | 云杉 |
| price | DECIMAL(10,2) | 原价（元），固定不变（不做调价历史）；价格带按类目差异化 | 1299.00 |
| listed_at | DATE | 上架日期，恒早于数据期起点 2025-10-01 | 2025-06-08 |

## 示例数据（3 行，来自库中真实数据）
（待数据导入后填充真实查询结果——T2 收尾项）

## 关联关系
- products.id ← order_items.product_id（一对多：一个商品出现在 0-N 条订单明细中；类目内幂律热销，少数爆款贡献多数销量）
- 本表无外键指向其他表，是维表（被引用方），共 2,000 行。

## 该表能回答的典型问题
- 各类目的商品数量、平均原价是多少？
- 某品牌/某类目下有哪些商品？
- 商品的价格带分布如何？

## 注意事项
- price 是原价，不是成交价：成交单价在 order_items.unit_price（= ROUND(price × discount_rate, 2) 的下单时快照），统计销售额不要用 price × quantity。
- 本数据集无下架概念，"在售商品总数"就是全部 2,000 行——这是动销率的分母口径（见口径文档"动销率"）。
