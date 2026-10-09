-- ============================================================
-- data/schema.sql —— W2 分析库建表脚本（MySQL 8.0）
-- 执行者：data/generate_dataset.py --init（用管理员账号，读 .env）
-- 只读账号 text2sql_ro 的创建也在脚本里做（密码读 .env，不写死在 SQL 文件）
-- ============================================================

CREATE DATABASE IF NOT EXISTS app  DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
-- app 库：W2 只建不填。W4 Java 后端使用（用户/历史/反馈），表由 Java 侧建。

CREATE DATABASE IF NOT EXISTS mart DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
USE mart;

-- ------------------------------------------------------------
-- 1. users 用户表（20,000 行）
-- ------------------------------------------------------------
CREATE TABLE users (
    id            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '用户 ID',
    name          VARCHAR(64)     NOT NULL COMMENT '姓名（Faker zh_CN 生成）',
    gender        ENUM('男','女') NOT NULL COMMENT '性别',
    city          VARCHAR(32)     NOT NULL COMMENT '城市（受控加权列表，见 §1.7）',
    province      VARCHAR(32)     NOT NULL COMMENT '省份（与 city 一一对应，非独立抽样）',
    registered_at DATETIME        NOT NULL COMMENT '注册时间（恒早于其全部订单）',
    PRIMARY KEY (id),
    KEY idx_users_registered_at (registered_at),
    KEY idx_users_city (city)
) ENGINE=InnoDB COMMENT='用户表';

-- ------------------------------------------------------------
-- 2. products 商品表（2,000 行）
-- ------------------------------------------------------------
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

-- ------------------------------------------------------------
-- 3. orders 订单主表（100,000 行）—— 表头，含两列冗余汇总
-- ------------------------------------------------------------
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

-- ------------------------------------------------------------
-- 4. order_items 订单明细表（约 150,000 行）—— 一行 = 订单里一种商品
-- ------------------------------------------------------------
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

-- ------------------------------------------------------------
-- 5. payments 支付表（约 89,000 行）—— 一笔成功订单恰一条
-- ------------------------------------------------------------
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

-- ------------------------------------------------------------
-- 6. refunds 退款表（约 9,300 行）
-- ------------------------------------------------------------
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
