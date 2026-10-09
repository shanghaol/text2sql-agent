"""jieba 自定义领域词典：保证表名 / 列名 / 指标词不被切碎。

为什么需要它：jieba 默认会把含下划线的英文词切碎
（order_items → order/_/items），也会把部分指标词切错
（客单价 → 客/单价、复购率 → 复/购率）。BM25 路的分词质量直接决定
关键词召回，所以在检索层模块加载时统一 add_word 一次。

这里的词表同时是行为的"回归锁"：tests/test_rag_retriever.py 对每个词
写了 lcut 整词断言，jieba 升级导致切分行为变化时测试会立刻变红。
"""

import jieba

# 表名（英文小写）
TABLE_WORDS: list[str] = [
    "users",
    "products",
    "orders",
    "order_items",
    "payments",
    "refunds",
]

# 高频列名（下划线复合词，jieba 默认必切碎）
COLUMN_WORDS: list[str] = [
    "order_amount",
    "item_count",
    "created_at",
    "paid_at",
    "refunded_at",
    "user_id",
    "product_id",
    "sale_amount",
    "discount_rate",
    "order_no",
]

# 指标词（中文名与缩写）
METRIC_WORDS: list[str] = [
    "GMV",
    "ARPU",
    "客单价",
    "复购率",
    "退款率",
    "动销率",
    "连带率",
    "支付转化率",
    "同比",
    "环比",
    "新客",
    "上个月",
]

# 模块级"已加过词"标记：让 setup_jieba 幂等且重复调用零成本。
_customized = False


def setup_jieba() -> None:
    """把领域词表加入 jieba 词典。幂等，可重复调用。"""
    global _customized
    if _customized:
        return
    for word in (*TABLE_WORDS, *COLUMN_WORDS, *METRIC_WORDS):
        jieba.add_word(word)
    _customized = True
