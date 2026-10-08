"""演示数据库初始化脚本：一次性生成 SQLite 演示库。

注意：这是 W1 阶段的一次性占位数据，所有数据都是**写死的**（不用 random），
保证每次建出来的库完全一致、测试结果稳定。
W2 会把这里换成 Faker 库生成的正式数据集（量更大、字段更真实）。

包含 3 张表（模拟一个迷你电商）：
- users     用户表：谁注册了、在哪个城市
- products  商品表：卖了什么、多少钱
- orders    订单表：谁在什么时候买了什么、金额多少、订单状态

运行方式（在仓库根目录）：
    .venv/Scripts/python -m agent.db.seed
"""

import sqlite3
from pathlib import Path

# ---------------------------------------------------------------------------
# 写死的演示数据。写成"数据在文件顶部、逻辑在下面"的结构，方便以后替换成 Faker。
# ---------------------------------------------------------------------------

# 10 个用户：中文姓名 + 北上广深杭等城市 + 2026-01~2026-06 的注册日期
USERS = [
    (1, "陈伟", "北京", "2026-01-12"),
    (2, "王芳", "上海", "2026-01-25"),
    (3, "李娜", "广州", "2026-02-03"),
    (4, "张强", "深圳", "2026-02-18"),
    (5, "刘洋", "杭州", "2026-03-07"),
    (6, "赵敏", "北京", "2026-03-22"),
    (7, "孙丽", "上海", "2026-04-10"),
    (8, "周杰", "广州", "2026-04-28"),
    (9, "吴敏", "深圳", "2026-05-15"),
    (10, "郑浩", "杭州", "2026-06-02"),
]

# 8 个商品：数码 / 家居 / 服饰 三个类目
PRODUCTS = [
    (1, "机械键盘", "数码", 399.0),
    (2, "无线鼠标", "数码", 129.0),
    (3, "27英寸显示器", "数码", 1499.0),
    (4, "降噪耳机", "数码", 899.0),
    (5, "陶瓷马克杯", "家居", 49.9),
    (6, "记忆棉枕头", "家居", 199.0),
    (7, "纯棉T恤", "服饰", 79.0),
    (8, "运动卫衣", "服饰", 259.0),
]

# 30 笔订单：created_at 分布在 2026-08 ~ 2026-10，
# status 取值 'paid'（已支付）/ 'refunded'（已退款）/ 'pending'（待支付）。
# amount = 单价 * 数量，保持和 products 表对得上；
# user_id / product_id 的取值范围都在 users / products 的 id 之内
# （W1 不加真正的外键约束，但数据要对得上，不然查询会查出"孤儿订单"）。
ORDERS = [
    (1, 1, 1, 1, 399.0, "paid", "2026-08-01 10:15:00"),
    (2, 2, 5, 2, 99.8, "paid", "2026-08-02 14:30:00"),
    (3, 3, 3, 1, 1499.0, "paid", "2026-08-04 09:20:00"),
    (4, 1, 7, 3, 237.0, "refunded", "2026-08-06 16:45:00"),
    (5, 4, 2, 1, 129.0, "paid", "2026-08-08 11:05:00"),
    (6, 5, 4, 1, 899.0, "paid", "2026-08-10 20:12:00"),
    (7, 6, 6, 1, 199.0, "pending", "2026-08-12 08:40:00"),
    (8, 7, 8, 1, 259.0, "paid", "2026-08-14 13:22:00"),
    (9, 8, 1, 2, 798.0, "paid", "2026-08-16 18:55:00"),
    (10, 9, 5, 1, 49.9, "refunded", "2026-08-18 10:08:00"),
    (11, 10, 2, 2, 258.0, "paid", "2026-08-20 15:33:00"),
    (12, 2, 7, 1, 79.0, "paid", "2026-08-23 09:47:00"),
    (13, 3, 6, 2, 398.0, "paid", "2026-08-26 21:10:00"),
    (14, 5, 3, 1, 1499.0, "pending", "2026-08-29 12:26:00"),
    (15, 7, 4, 1, 899.0, "paid", "2026-09-01 10:02:00"),
    (16, 1, 8, 2, 518.0, "paid", "2026-09-03 17:41:00"),
    (17, 4, 5, 3, 149.7, "paid", "2026-09-05 08:19:00"),
    (18, 6, 7, 4, 316.0, "refunded", "2026-09-08 14:54:00"),
    (19, 8, 2, 1, 129.0, "paid", "2026-09-10 19:36:00"),
    (20, 9, 1, 1, 399.0, "paid", "2026-09-13 11:28:00"),
    (21, 10, 4, 1, 899.0, "pending", "2026-09-15 16:03:00"),
    (22, 2, 3, 1, 1499.0, "paid", "2026-09-18 09:51:00"),
    (23, 3, 8, 1, 259.0, "paid", "2026-09-21 13:14:00"),
    (24, 5, 6, 1, 199.0, "refunded", "2026-09-24 18:29:00"),
    (25, 6, 1, 1, 399.0, "paid", "2026-09-27 10:46:00"),
    (26, 7, 5, 2, 99.8, "paid", "2026-09-30 15:58:00"),
    (27, 9, 7, 2, 158.0, "paid", "2026-10-03 09:34:00"),
    (28, 1, 2, 1, 129.0, "pending", "2026-10-06 20:47:00"),
    (29, 4, 4, 1, 899.0, "paid", "2026-10-08 12:19:00"),
    (30, 8, 8, 1, 259.0, "paid", "2026-10-07 17:52:00"),
]


def seed_demo_db(db_path: Path) -> Path:
    """删除旧库文件后重建演示库，返回库文件路径。

    参数:
        db_path: 目标 SQLite 文件路径（会覆盖同名旧文件）。
    返回:
        传入的 db_path，方便链式调用 / 断言。
    """
    # 确保父目录存在（比如 data/ 还没建出来的情况）
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    # 删除旧文件：保证"每次建库 = 干净的全量重建"，避免残留脏数据
    if db_path.exists():
        db_path.unlink()

    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.cursor()

        # 建表。字段类型尽量简单：INTEGER / TEXT / REAL
        cursor.execute(
            """
            CREATE TABLE users (
                id INTEGER PRIMARY KEY,      -- 用户 ID
                name TEXT,                   -- 用户姓名
                city TEXT,                   -- 所在城市
                registered_at TEXT           -- 注册日期（YYYY-MM-DD）
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE products (
                id INTEGER PRIMARY KEY,      -- 商品 ID
                name TEXT,                   -- 商品名称
                category TEXT,               -- 类目（数码/家居/服饰）
                price REAL                   -- 单价（元）
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE orders (
                id INTEGER PRIMARY KEY,      -- 订单 ID
                user_id INTEGER,             -- 下单用户，对应 users.id
                product_id INTEGER,          -- 购买商品，对应 products.id
                quantity INTEGER,            -- 购买数量
                amount REAL,                 -- 订单金额（元）= 单价 * 数量
                status TEXT,                 -- 状态：paid/refunded/pending
                created_at TEXT              -- 下单时间（YYYY-MM-DD HH:MM:SS）
            )
            """
        )

        # executemany = 批量插入，比循环 execute 更简洁高效
        cursor.executemany("INSERT INTO users VALUES (?, ?, ?, ?)", USERS)
        cursor.executemany("INSERT INTO products VALUES (?, ?, ?, ?)", PRODUCTS)
        cursor.executemany("INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?, ?)", ORDERS)

        conn.commit()
        return db_path
    finally:
        # finally 保证即使中途出错也会关闭连接
        conn.close()


if __name__ == "__main__":
    # 直接运行本模块：建库到默认位置，并打印每张表的行数做 sanity check
    from agent.config import DB_PATH

    path = seed_demo_db(DB_PATH)
    print(f"演示数据库已生成：{path.resolve()}")

    conn = sqlite3.connect(path)
    try:
        for table in ("users", "products", "orders"):
            row_count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            print(f"  {table:<10} {row_count} 行")
    finally:
        conn.close()
    print("完成。可以运行 .venv/Scripts/python -m agent.main 启动对话了。")
