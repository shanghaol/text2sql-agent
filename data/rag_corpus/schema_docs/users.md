# 表：用户表（users）

## DDL
```sql
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
```

## 列说明
| 列 | 类型 | 业务含义 | 示例 |
| --- | --- | --- | --- |
| id | BIGINT UNSIGNED | 用户 ID，主键自增，被 orders.user_id 引用 | 10001 |
| name | VARCHAR(64) | 用户姓名 | 王丹 |
| gender | ENUM('男','女') | 性别 | 女 |
| city | VARCHAR(32) | 所在城市，约 20 个受控值（北京/上海/广州/深圳/杭州等），头部 6 城权重合计约 45% | 杭州 |
| province | VARCHAR(32) | 所在省份，由 city 映射带出，非独立抽样 | 浙江 |
| registered_at | DATETIME | 注册时间，恒早于该用户全部订单的下单时间；注册窗口为 2024-01-01 至 2025-08-31 | 2024-03-15 10:22:47 |

## 示例数据（3 行，来自库中真实数据）
（待数据导入后填充真实查询结果——T2 收尾项）

## 关联关系
- users.id ← orders.user_id（一对多：一个用户有 0-N 笔订单；订单数走重尾分布，约 35-40% 用户仅 1 单，头部 1% 用户超 30 单）
- 本表无外键指向其他表，是维表（被引用方），共 20,000 行。

## 该表能回答的典型问题
- 各城市/省份的用户数分布？头部城市是哪些？
- 每月新增注册用户趋势？
- 某用户是谁、何时注册、在哪个城市？

## 注意事项
- "新客"不等于新注册：registered_at 只是注册 cohort，首次下单时间要查 MIN(orders.created_at)（见口径文档"新客数与新客占比"）。
- 统计 ARPU 时分母是本表全部 20,000 在册用户，不是"期内下单/活跃用户"（见口径文档"ARPU"）。
- province 由 city 一一映射，按城市 GROUP BY 即可覆盖省份维度，无需额外关联。
