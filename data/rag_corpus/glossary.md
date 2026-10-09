<!-- 用途：业务口径文档（RAG 三路语料之一），检索命中后按条注入 prompt 约束 SQL 口径。
     切分方式：按 "## 指标：<中文名>（<english_id>）" 二级标题切条，每条一片，source_id = 括号内 english_id。
     新增/修改条目后需运行 python -m agent.rag.build_index 全量重建索引。 -->

# 业务口径文档（glossary）

> 11 个核心指标的计算口径。所有"统计期"指数据期 2025-10-01 至 2026-09-30 内由提问指定的区间，时间过滤一律左闭右开。

## 指标：GMV（gmv）
**定义**：统计期内成功支付的订单金额总和。
**精确计算口径**：SUM(payments.amount)，按支付时间过滤，如 `WHERE paid_at >= '2026-09-01' AND paid_at < '2026-10-01'`。
**常见误用与陷阱**：① SUM(orders.order_amount) 不滤状态——pending/cancelled 订单未支付，会虚增；② GMV 不冲减退款（冲减后是"净销售额"）。
**关联列**：payments.amount、payments.paid_at

## 指标：净销售额（net_sales）
**定义**：GMV − 统计期内退款金额，即扣除退款后实际留存的销售额。
**精确计算口径**：SUM(payments.amount) − SUM(refunds.amount)；支付按 paid_at、退款按 refunded_at 各自落在统计期内。
**常见误用与陷阱**：① 与 GMV 混用且回答不报口径，是典型错答；② 用 orders.status 估算退款额会漏掉部分退款订单（其状态仍是 completed）。
**关联列**：payments.amount、refunds.amount、refunds.refunded_at

## 指标：客单价（avg_order_value）
**定义**：GMV ÷ 统计期内支付成功的订单数，即笔均支付金额。
**精确计算口径**：SUM(payments.amount) / COUNT(DISTINCT payments.order_id)，按 paid_at 定期；payments 一单恰一条，COUNT(*) 等价但 DISTINCT 更稳。
**常见误用与陷阱**：① 分母用订单总数（含 pending/cancelled 未支付）会低估；② 分母用用户数算出的不是客单价，是 ARPU。
**关联列**：payments.amount、payments.order_id、payments.paid_at

## 指标：ARPU（arpu）
**定义**：每用户平均收入 = GMV ÷ 统计期在册用户总数。
**精确计算口径**：SUM(payments.amount) / (SELECT COUNT(*) FROM users)；分母是全部 20,000 在册用户，不是期内下单用户。
**常见误用与陷阱**：① 与客单价（ARPPU）混淆——客单价分母是订单数，ARPU 分母是用户数；② 分母误用"期内活跃用户"会虚高。
**关联列**：payments.amount、users.id

## 指标：复购率（repurchase_rate）
**定义**：统计期内下单 ≥2 次的用户数 ÷ 期内下单用户数。
**精确计算口径**：按 orders.created_at 圈期内下单用户并 GROUP BY user_id，分子 = HAVING COUNT(*) >= 2 的用户数，分母 = COUNT(DISTINCT user_id)。
**常见误用与陷阱**：① 把"买 ≥2 件"当复购——件数不等于次数，看下单次数；② 统计期间不锁定，跨期口径不可比。
**关联列**：orders.user_id、orders.created_at

## 指标：退款率（refund_rate）
**定义**：双口径——金额口径 = 退款金额 ÷ 支付金额；订单口径 = 有退款记录的订单数 ÷ 支付成功订单数。
**精确计算口径**：金额口径 SUM(refunds.amount)/SUM(payments.amount)；订单口径 COUNT(DISTINCT refunds.order_id)/COUNT(payments.order_id)；时间分别按 refunded_at/paid_at，回答必须报口径。
**常见误用与陷阱**：① orders.status='refunded' 会漏掉部分退款订单（状态仍是 completed）——退款订单必须查 refunds 表；② 双口径数值不同且都合法。
**关联列**：refunds.amount、refunds.order_id ↔ orders.status、payments.paid_at

## 指标：同比与环比（yoy_mom）
**定义**：同比 =（本期 − 去年同期）÷ 去年同期；环比 =（本期 − 上期）÷ 上期。
**精确计算口径**：先按 DATE_FORMAT(时间列, '%Y-%m') 聚合出各期值，再算 (本期 − 对比期) / 对比期；同比对比期 = 上一年同月。
**常见误用与陷阱**：① 对比期为 0 时除零，用 NULLIF(对比期, 0) 或明示不可比；② 月边界必须按自然月（左闭右开或 '%Y-%m'），勿用 30 天近似。
**关联列**：orders.created_at、payments.paid_at、refunds.refunded_at（按所算指标选时间列）

## 指标：支付转化率（pay_conversion）
**定义**：支付转化率 = 支付成功订单数 ÷ 全部订单数，分母含 pending/cancelled。
**精确计算口径**：分子 = COUNT(DISTINCT payments.order_id)（等价于 orders.status IN ('paid','shipped','completed','refunded') 的订单数），分母 = COUNT(*) FROM orders，同期。
**常见误用与陷阱**：① 分母漏掉未支付状态会虚高；② pending/cancelled 在 payments 中无记录，对 payments 计数即分子，勿重复 JOIN 计数。
**关联列**：orders.status、payments.order_id、orders.created_at

## 指标：动销率（sell_through）
**定义**：动销率 = 统计期内有售出的商品数 ÷ 在售商品总数。
**精确计算口径**：分子 = COUNT(DISTINCT order_items.product_id)（经关联订单的 created_at 圈期），分母 = (SELECT COUNT(*) FROM products) = 2,000。
**常见误用与陷阱**：① 分母口径要声明——本项目无下架概念，分母即全部商品；② 售出与否看明细表，商品表自身无销量字段。
**关联列**：products.id ↔ order_items.product_id、orders.created_at

## 指标：连带率（items_per_order）
**定义**：连带率（件单比）= 总购买件数 ÷ 订单数，即每单平均件数。
**精确计算口径**：SUM(order_items.quantity) / COUNT(DISTINCT order_items.order_id)；或 SUM(orders.item_count)/COUNT(*)（冗余列等价，免 JOIN）。
**常见误用与陷阱**：① 与客单价混淆——本指标是件数口径，客单价是金额口径；② 订单数勿忘去重，一条明细一行会重复计数。
**关联列**：order_items.quantity、orders.item_count、orders.id

## 指标：新客数与新客占比（new_customers）
**定义**：期内首次下单的用户数（新客数）；新客占比 = 新客数 ÷ 期内下单用户数。
**精确计算口径**：先算每用户首次下单时间 MIN(orders.created_at)（相对数据期起点 2025-10-01 全局判断），落在统计期内者为新客，再除以期内下单的 DISTINCT 用户数。
**常见误用与陷阱**：① 与"新增注册用户"混淆——注册 cohort（users.registered_at）不等于首单时间；② "首次"须全局判断，不能只在期内取最小值。
**关联列**：orders.user_id、orders.created_at、users.registered_at
