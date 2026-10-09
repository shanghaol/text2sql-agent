"""W2 数据集脚本：生成 6 张表数据 → 勾稽自检 → 写 CSV → 建库建账号 → 导入 MySQL。

用法（一条命令走完三步，见设计文档 §1.8）：

    python data/generate_dataset.py --seed 42          # 只生成 CSV，不需要 MySQL
    python data/generate_dataset.py --seed 42 --init   # 生成 + 建库建表 + 创建只读账号
    python data/generate_dataset.py --seed 42 --load   # 生成 + 建库 + 批量导入 MySQL
    python data/generate_dataset.py --skip-generate --load   # 跳过生成，直接重导已有 CSV

为什么分三步却放一个入口：
- ① 生成慢（Faker 抽中文姓名、逐单随机），导入快（批量 INSERT），分开后"重导不用重算"；
- 中间先落 CSV：a) 可以人工抽查数据质量（看"剧情"是否合理）；
  b) 出问题时能区分是"生成错了"还是"导入错了"；c) CSV 是派生物，已加入 .gitignore。
- 固定 --seed：同时固定 random 和 Faker 两套随机源，同种子逐字节复现同一份 CSV，
  这样 W3 评测集的金标答案才不会随数据漂移。

月度权重、重尾用户分布、部分退款这些"剧情"不是为了让数据逼真而编的，
是为了让 W3 的趋势题/口径陷阱题有可区分的正确答案（设计 §0.3 / §1.6 / §1.7）。
"""

import argparse
import calendar
import csv
import math
import os
import random
import sys
import time
from collections import Counter
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from dotenv import load_dotenv
from faker import Faker

# ---------------------------------------------------------------------------
# 路径与数据期常量（设计 §1.6：快照日期写死，不以"今天"倒推，保证可复现）
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = REPO_ROOT / "data"
DATASET_DIR = DATA_DIR / "dataset"
SCHEMA_FILE = DATA_DIR / "schema.sql"

# 数据期：2025-10-01 00:00:00 → 2026-09-30 23:59:59，整整 12 个自然月。
# 数据截止在"上个月底"，所以"上个月"这类问题有唯一闭合答案。
# naive datetime（无时区）是有意为之：MySQL DATETIME 本身就不带时区（设计 §1.4）。
DATA_START = datetime(2025, 10, 1, 0, 0, 0)  # noqa: DTZ001
DATA_END = datetime(2026, 9, 30, 23, 59, 59)  # noqa: DTZ001

# 用户注册窗口：早于数据期起点（2025-10-01），
# 因此"注册至少早于首单 1 个月"这条勾稽（R7）天然成立。
REGISTER_START = datetime(2024, 1, 1, 0, 0, 0)  # noqa: DTZ001
REGISTER_END = datetime(2025, 8, 31, 23, 59, 59)  # noqa: DTZ001

# 商品上架窗口：同样要求恒早于数据期起点（DDL COMMENT 的约定）。
LISTED_START = date(2024, 1, 1)
LISTED_END = date(2025, 9, 15)

# 行数规模（设计 §1.5）：users/products/orders 精确，其余派生（区间断言见自检）。
N_USERS = 20000
N_PRODUCTS = 2000
N_ORDERS = 100000

# 只读账号（设计 §1.3 第一层防线）：密码从 .env 读，绝不写死在代码或 SQL 里。
RO_USER = "text2sql_ro"

# ---------------------------------------------------------------------------
# 业务"剧情"参数（设计 §1.6 / §1.7）
# ---------------------------------------------------------------------------

# 月度订单量权重（%）：双 11、618 是高峰，2 月春节是淡季。
# 不做均匀分布——没有季节性的月度趋势题就没有可分析的信号。
MONTH_WEIGHTS = [
    ("2025-10", 7.5),
    ("2025-11", 13.0),   # 双 11，全年最高
    ("2025-12", 9.0),
    ("2026-01", 8.0),
    ("2026-02", 6.5),   # 春节淡季，全年最低
    ("2026-03", 8.0),
    ("2026-04", 7.0),
    ("2026-05", 7.5),
    ("2026-06", 12.0),   # 618
    ("2026-07", 7.0),
    ("2026-08", 7.0),
    ("2026-09", 7.5),
]

# 20 城受控加权列表（%）：头部 6 城合计约 45%，让 GROUP BY 有明显的头部和长尾。
# 不用 Faker.address——它自由造地名，会让"各城市订单分布"失去业务含义。
# 省份由城市映射带出，不是独立抽样（城市和省份本来就一一对应）。
CITY_TABLE = {
    "北京": ("北京", 10.0),
    "上海": ("上海", 9.0),
    "广州": ("广东", 8.0),
    "深圳": ("广东", 8.0),
    "杭州": ("浙江", 6.0),
    "成都": ("四川", 4.5),
    "武汉": ("湖北", 4.5),
    "南京": ("江苏", 4.5),
    "重庆": ("重庆", 4.5),
    "西安": ("陕西", 4.5),
    "长沙": ("湖南", 4.5),
    "郑州": ("河南", 4.0),
    "苏州": ("江苏", 4.0),
    "青岛": ("山东", 3.5),
    "宁波": ("浙江", 3.5),
    "无锡": ("江苏", 3.5),
    "厦门": ("福建", 3.5),
    "沈阳": ("辽宁", 3.5),
    "昆明": ("云南", 3.5),
    "大连": ("辽宁", 3.0),
}

# 每类目：价格带（元）、虚构品牌、商品词、属性词。
# 商品名 = "品牌 类目 商品词+属性词" 模板拼接（如"云杉 数码 降噪蓝牙耳机 旗舰款"）。
# 品牌全部虚构避免撞真实品牌；类目组合数 ≥ 250，够每类目抽满 250 个不重名商品。
CATEGORY_TABLE = {
    "数码": {
        "price": (199, 7999),
        "brands": ["云杉", "星核", "极光", "量子", "蓝鲸"],
        "items": ["降噪蓝牙耳机", "智能手表", "机械键盘", "便携音箱", "充电宝",
                  "无线路由器", "电动牙刷", "行车记录仪", "智能门锁", "平板电脑"],
        "attrs": ["旗舰款", "青春版", "Pro版", "升级款", "极简版", "联名款", "限量款"],
    },
    "家居": {
        "price": (29, 1999),
        "brands": ["木言", "素居", "暖山", "白泽"],
        "items": ["收纳箱", "香薰灯", "床品四件套", "保温杯", "落地灯",
                  "懒人沙发", "加湿器", "晾衣架", "电热毯", "餐具套装"],
        "attrs": ["北欧风", "加大款", "家用款", "便携款", "静音版", "日式款", "经典款"],
    },
    "服饰": {
        "price": (59, 1299),
        "brands": ["布语", "衣栏", "织云", "简杉", "沐野"],
        "items": ["连帽卫衣", "羽绒服", "牛仔裤", "纯棉T恤", "休闲外套",
                  "针织开衫", "运动短裤", "衬衫", "休闲长裤", "连衣裙"],
        "attrs": ["宽松版", "修身款", "加绒款", "经典款", "轻薄款", "国潮款", "基础款"],
    },
    "美妆": {
        "price": (39, 599),
        "brands": ["花肌", "澜妆", "萃语", "颜究所"],
        "items": ["保湿面霜", "氨基酸洁面", "丝绒口红", "精华液", "防晒霜",
                  "卸妆水", "面膜", "眼霜", "散粉", "香水"],
        "attrs": ["滋润型", "清爽型", "修护版", "限量款", "敏感肌适用", "持久版", "补水型"],
    },
    "食品": {
        "price": (9.9, 199),
        "brands": ["谷雨", "鲜工坊", "拾味", "山禾"],
        "items": ["坚果礼盒", "手工饼干", "有机燕麦", "花茶", "牛肉干",
                  "芝麻丸", "果干", "挂面", "蜂蜜", "咖啡豆"],
        "attrs": ["家庭装", "便携装", "经典味", "香辣味", "低糖版", "礼盒装", "早餐款"],
    },
    "母婴": {
        "price": (25, 899),
        "brands": ["萌芽", "贝初", "小鹿斑", "棉花糖"],
        "items": ["婴儿纸尿裤", "儿童保温杯", "婴儿洗衣液", "宝宝辅食机", "婴儿湿巾",
                  "儿童餐具", "婴儿护肤霜", "学步袜", "儿童绘本灯", "吸奶器"],
        "attrs": ["加大号", "柔软款", "新生儿款", "防漏款", "婴幼儿适用", "便携款", "升级款"],
    },
    "运动户外": {
        "price": (49, 2999),
        "brands": ["峰行", "野径", "拓山", "驰风", "岩语"],
        "items": ["跑步鞋", "瑜伽垫", "露营帐篷", "运动水壶", "登山包",
                  "跳绳", "护膝", "钓鱼竿", "骑行手套", "防潮垫"],
        "attrs": ["专业版", "轻量款", "加厚款", "折叠款", "透气款", "耐磨款", "入门款"],
    },
    "图书": {
        "price": (19, 99),
        "brands": ["纸间", "墨白", "阅野", "字里行"],
        "items": ["Python编程入门", "时间管理手册", "世界简史", "散文精选集", "儿童百科",
                  "经济学通识", "悬疑小说", "诗词选", "摄影教程", "育儿手记"],
        "attrs": ["精装版", "平装版", "插图本", "典藏版", "修订版", "口袋本", "礼盒版"],
    },
}

# 订单状态目标比例（%）（设计 §1.7）。有支付记录的状态 = paid/shipped/completed/refunded，
# 合计 89% → payments 约 8.9 万行；pending/cancelled 无支付。
STATUS_WEIGHTS = {
    "completed": 65,
    "shipped": 9,
    "paid": 7,
    "refunded": 8,
    "cancelled": 8,
    "pending": 3,
}
PAY_STATUS = ("paid", "shipped", "completed", "refunded")

# 状态-年龄约束（设计 §1.7 / R8）：下单太近的订单不可能走完后续流程。
# 没有这条规则会出现"9 月 29 日下单、9 月 30 日已完成"的穿帮数据。
# 不满足年龄要求的订单自动降级为 pending/paid/cancelled（这三个任何年龄都合法）。
STATUS_MIN_AGE_DAYS = {"completed": 7, "refunded": 10, "shipped": 2}
FALLBACK_STATUS_WEIGHTS = {"pending": 4, "paid": 3.5, "cancelled": 2.5}

# 每单商品种数：1 种 60% / 2 种 30% / 3 种 10% → 期望 1.5 种/单 → order_items ≈ 15 万行。
ITEM_KIND_WEIGHTS = {1: 60, 2: 30, 3: 10}

# 购买数量 1-5，偏小值。
QUANTITY_WEIGHTS = {1: 55, 2: 20, 3: 12, 4: 8, 5: 5}

# 折扣率（设计 §1.7）：解释"同商品成交价为什么不一样"。单价 = 原价 × 折扣率（下单快照）。
DISCOUNT_TABLE = {
    Decimal("1.00"): 40,
    Decimal("0.95"): 25,
    Decimal("0.90"): 15,
    Decimal("0.85"): 12,
    Decimal("0.80"): 8,
}

# 支付方式（%）
PAY_METHOD_WEIGHTS = {"微信": 55, "支付宝": 35, "银联": 10}

# 退款原因（%）
REFUND_REASON_WEIGHTS = {
    "质量问题": 30,
    "七日无理由": 25,
    "物流问题": 20,
    "错拍不想要": 15,
    "其他": 10,
}

# 部分退款设计（设计 §1.7）：发生退款的行为分两类——
#   60% 全额退款 → 订单 status = refunded；
#   40% 部分退款（退 20%-90%）→ status 保持 completed。
# 这是故意制造"退款率"的两个合法口径（只看 orders.status 会漏掉部分退款订单），
# 是 W3 口径陷阱题的命题依据。 completed 订单有 PARTIAL_REFUND_RATE 的概率带部分退款，
# 使部分退款 ≈ completed 的 2%（设计 §1.5 推算）。
PARTIAL_REFUND_RATE = 0.02

# ---------------------------------------------------------------------------
# 各表行布局（元组下标常量，避免满屏 magic number）
# ---------------------------------------------------------------------------
U_ID, U_NAME, U_GENDER, U_CITY, U_PROV, U_REG = range(6)
P_ID, P_NAME, P_CAT, P_BRAND, P_PRICE, P_LISTED = range(6)
O_ID, O_NO, O_USER, O_STATUS, O_AMOUNT, O_COUNT, O_CREATED = range(7)
I_ID, I_ORDER, I_PRODUCT, I_QTY, I_UPRICE, I_RATE, I_SAMT = range(7)
PAY_ID, PAY_ORDER, PAY_AMOUNT, PAY_METHOD, PAY_AT = range(5)
R_ID, R_PAY, R_ORDER, R_AMOUNT, R_REASON, R_AT = range(6)

# CSV 表头 = DDL 列顺序（含 id，导入时显式指定主键，保证外键引用得上）
CSV_HEADERS = {
    "users": ("id", "name", "gender", "city", "province", "registered_at"),
    "products": ("id", "name", "category", "brand", "price", "listed_at"),
    "orders": ("id", "order_no", "user_id", "status",
               "order_amount", "item_count", "created_at"),
    "order_items": ("id", "order_id", "product_id", "quantity",
                    "unit_price", "discount_rate", "sale_amount"),
    "payments": ("id", "order_id", "amount", "method", "paid_at"),
    "refunds": ("id", "payment_id", "order_id", "amount", "reason", "refunded_at"),
}

# 导入顺序 = 外键依赖顺序；清表则完全反过来
IMPORT_ORDER = ("users", "products", "orders", "order_items", "payments", "refunds")
IMPORT_BATCH_ROWS = 5000  # executemany 每批行数（设计 §1.8）


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def round2(value: Decimal) -> Decimal:
    """保留两位小数，四舍五入（ROUND_HALF_UP）。

    注意不能用 Python 默认的银行家舍入（ROUND_HALF_EVEN）：
    MySQL 的 ROUND 是"四舍五入"，两边规则不一致时，导入后的库内校验（T5 断言）会对不上。
    """
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def rand_datetime(start: datetime, end: datetime) -> datetime:
    """在 [start, end] 闭区间内均匀抽一个时刻（精确到秒）。"""
    span_seconds = int((end - start).total_seconds())
    return start + timedelta(seconds=random.randint(0, span_seconds))


def poisson(lam: float) -> int:
    """泊松分布抽样（Knuth 算法）。

    标准库 random 没有自带 Poisson，自己实现：反复乘以 (0,1) 随机数，
    直到乘积小于 e^-λ，乘的次数就是样本值。λ 不大时（本项目 λ ≤ 25）效率够用。
    """
    threshold = math.exp(-lam)
    product = random.random()
    count = 0
    while product > threshold:
        count += 1
        product *= random.random()
    return count


def weighted_choice(table: dict):
    """按 {取值: 权重} 字典加权抽一个 key（权重是百分比数字，不必加和为 100）。

    key 一般是字符串，也可以是别的类型（如折扣率表里是 Decimal）。
    """
    return random.choices(list(table), weights=list(table.values()))[0]


def build_months() -> list[tuple[str, float, datetime, datetime]]:
    """把 'YYYY-MM' 权重表展开成 (标签, 权重, 月初, 月末)，月末取该月真实最后一天。"""
    months = []
    for label, weight in MONTH_WEIGHTS:
        year, month = int(label[:4]), int(label[5:7])
        start = datetime(year, month, 1)  # noqa: DTZ001  与 MySQL DATETIME 一致，无时区
        last_day = calendar.monthrange(year, month)[1]
        end = datetime(year, month, last_day, 23, 59, 59)  # noqa: DTZ001
        months.append((label, weight, start, end))
    return months


MONTHS = build_months()


# ---------------------------------------------------------------------------
# ① 生成：六张表（顺序：users → products → orders/items/payments/refunds）
# ---------------------------------------------------------------------------
def generate_users() -> list[tuple]:
    """20,000 个用户：中文姓名用 Faker；城市用受控加权列表（不是 Faker.address）。

    种子已由 generate_and_write 统一设置（random.seed + Faker.seed），这里不再动。
    """
    fake = Faker("zh_CN")
    rows = []
    city_names = list(CITY_TABLE)
    city_weights = [CITY_TABLE[c][1] for c in city_names]
    for uid in range(1, N_USERS + 1):
        name = fake.name()
        gender = random.choices(("男", "女"), weights=(51, 49))[0]
        city = random.choices(city_names, weights=city_weights)[0]
        registered_at = rand_datetime(REGISTER_START, REGISTER_END)
        rows.append((uid, name, gender, city, CITY_TABLE[city][0], registered_at))
    return rows


def generate_products() -> list[tuple]:
    """2,000 个商品 = 8 类目 × 250 个；名字模板拼接，价格落在类目价格带内（均匀）。"""
    rows = []
    pid = 0
    for category, spec in CATEGORY_TABLE.items():
        lo, hi = spec["price"]
        # 先拼出该类目所有"品牌×商品词×属性词"组合，再无放回抽 250 个 → 名字保证不重复
        combos = [f"{b} {category} {i}{a}"
                  for b in spec["brands"] for i in spec["items"] for a in spec["attrs"]]
        chosen = random.sample(combos, N_PRODUCTS // len(CATEGORY_TABLE))
        for name in chosen:
            pid += 1
            price = round2(Decimal(str(random.uniform(lo, hi))))
            listed_at = date.fromordinal(random.randint(
                LISTED_START.toordinal(), LISTED_END.toordinal()))
            rows.append((pid, name, category, name.split()[0], price, listed_at))
    return rows


def build_product_weights(product_rows: list[tuple]) -> list[float]:
    """类目内热销权重（幂律）：类目内排名越靠前越容易被买到，制造"爆款"。

    若商品被均匀购买，任何商品都卖不动也没有爆款，"动销率/热销 TOP"类题就没有剧情。
    实现上：给每个类目的 250 个商品随机分配排名 rank，权重 = 1/rank（Zipf 形状）。
    """
    weights = [0.0] * len(product_rows)
    ranks = list(range(1, N_PRODUCTS // len(CATEGORY_TABLE) + 1))
    for category in CATEGORY_TABLE:
        cat_ids = [row[P_ID] for row in product_rows if row[P_CAT] == category]
        random.shuffle(ranks)
        for pos, pid in enumerate(cat_ids):
            weights[pid - 1] = 1.0 / ranks[pos]
    return weights


def generate_orders(
    product_rows: list[tuple], product_weights: list[float]
) -> tuple[list[tuple], list[tuple], list[tuple], list[tuple]]:
    """生成 orders / order_items / payments / refunds 四张表。

    返回 (orders, order_items, payments, refunds)，行布局见文件顶部下标常量。
    """
    product_price = {row[P_ID]: row[P_PRICE] for row in product_rows}
    product_ids = [row[P_ID] for row in product_rows]
    month_weights = [m[1] for m in MONTHS]

    # --- 第 1 步：每用户订单数（重尾分布，设计 §1.7） -----------------------
    # 先给每用户抽活跃参数 λ（对数均匀截断到 [0.2, 25]），再按泊松抽订单数。
    # 为什么刻意重尾：均匀分布下复购率≈100%、全员 1 单则≈0%，都没有区分度；
    # 重尾让"多数人低频 + 少数人高频"，复购率/ARPU 分位数类评测题答案才稳定。
    # 泊松抽样总和是随机的，最后把总数精确调整到 100,000（设计要求订单表精确 10 万行）。
    counts = []
    for _ in range(N_USERS):
        lam = math.exp(random.uniform(math.log(0.2), math.log(25.0)))
        counts.append(poisson(lam))
    total = sum(counts)
    if total < N_ORDERS:  # 不够就均匀随机补
        while total < N_ORDERS:
            counts[random.randrange(N_USERS)] += 1
            total += 1
    elif total > N_ORDERS:  # 多了就按"单数加权"删：单越多越可能被删，保住重尾形状
        bag = [uid for uid, c in enumerate(counts, start=1) for _ in range(c)]
        while total > N_ORDERS:
            i = random.randrange(len(bag))
            uid = bag[i]
            bag[i] = bag[-1]
            bag.pop()
            if counts[uid - 1] > 0:
                counts[uid - 1] -= 1
                total -= 1

    # --- 第 2 步：逐单生成（暂存，最后统一排序、编号） -----------------------
    # pending_orders 每个元素：
    #   (user_id, status, created_at, paid_at|None, method|None, refund_info|None, items)
    # refund_info = (退款金额, 原因, 退款完成时间)；items = [(商品id, 折扣率, 单价, 数量, 小计)]
    pending_orders = []
    for uid in range(1, N_USERS + 1):
        for _ in range(counts[uid - 1]):
            # 下单时间：先按月权重抽月，再月内均匀抽时刻。
            # 月内均匀是设计明确的取舍（周末/时段波动属过度设计，不做）。
            _, _, month_start, month_end = random.choices(
                MONTHS, weights=month_weights)[0]
            created_at = month_start + timedelta(
                seconds=random.randint(0, int((month_end - month_start).total_seconds())))

            # 状态：按目标比例加权抽，再套年龄约束（R8）
            status = weighted_choice(STATUS_WEIGHTS)
            age_days = (DATA_END - created_at).days
            if age_days < STATUS_MIN_AGE_DAYS.get(status, 0):
                status = weighted_choice(FALLBACK_STATUS_WEIGHTS)

            # 明细：每单 1-3 种商品（类目内幂律热销加权），数量 1-5 偏小
            n_kinds = random.choices(list(ITEM_KIND_WEIGHTS),
                                     weights=list(ITEM_KIND_WEIGHTS.values()))[0]
            items = []
            chosen_pids: set[int] = set()
            for _ in range(n_kinds):
                while True:  # 同一单里商品不重复，抽重了就重抽（最多 3 种，代价极小）
                    pid = random.choices(product_ids, weights=product_weights)[0]
                    if pid not in chosen_pids:
                        chosen_pids.add(pid)
                        break
                rate = weighted_choice(DISCOUNT_TABLE)
                unit_price = round2(product_price[pid] * rate)  # R3：单价 = 原价×折扣率
                quantity = random.choices(list(QUANTITY_WEIGHTS),
                                          weights=list(QUANTITY_WEIGHTS.values()))[0]
                sale_amount = round2(unit_price * quantity)  # R3：小计 = 单价×数量
                items.append((pid, rate, unit_price, quantity, sale_amount))

            order_amount = sum((it[4] for it in items), Decimal("0.00"))

            # 支付与退款：有支付记录的状态恰一条支付（R4/R5）；退款两类行为（R6）
            paid_at = None
            method = None
            refund_info = None
            if status in PAY_STATUS:
                # 支付发生在下单后 5 分钟 ~ 6 小时内；贴着数据期终点则截断（不会早于下单）
                paid_at = min(created_at + timedelta(seconds=random.randint(300, 21600)),
                              DATA_END)
                method = weighted_choice(PAY_METHOD_WEIGHTS)
                if status == "refunded":
                    # 全额退款：金额 = 实付，status 已是 refunded
                    refund_info = _make_refund(order_amount, paid_at, full=True)
                elif status == "completed" and random.random() < PARTIAL_REFUND_RATE:
                    # 部分退款：只发生在 completed 订单上（退 20%-90%，
                    # status 保持 completed）——这是"退款率双口径"陷阱的设计依据
                    refund_info = _make_refund(order_amount, paid_at, full=False)

            pending_orders.append((uid, status, created_at, paid_at, method,
                                   refund_info, items))

    # --- 第 3 步：按下单时间排序，分配订单 ID / 订单号，摊平成四张表 ----------
    # 让 id 顺序 = 时间顺序，贴近真实自增主键的语义，演示数据也更自然。
    sorted_idx = sorted(range(len(pending_orders)),
                        key=lambda i: pending_orders[i][2])
    orders: list[tuple] = []
    payments: list[tuple] = []
    refunds: list[tuple] = []
    items_pending: list[tuple] = []  # (原 pending 下标, 商品id, 折扣率, 单价, 数量, 小计)
    for new_id, old_idx in enumerate(sorted_idx, start=1):
        uid, status, created_at, paid_at, method, refund_info, items = \
            pending_orders[old_idx]
        order_no = f"SO{created_at:%Y%m%d}{new_id:08d}"  # SO+yyyyMMdd+8位序号 = 18 字符
        orders.append((new_id, order_no, uid, status, order_amount_of(items),
                       item_count_of(items), created_at))
        if paid_at is not None:
            pay_id = len(payments) + 1
            payments.append((pay_id, new_id, order_amount_of(items), method, paid_at))
            if refund_info is not None:
                r_amount, r_reason, r_at = refund_info
                refunds.append((len(refunds) + 1, pay_id, new_id,
                                r_amount, r_reason, r_at))
        for it in items:
            items_pending.append((old_idx, it[0], it[1], it[2], it[3], it[4]))

    # 明细行重编号：order_id 换成排序后的新订单 ID
    idx_to_id = {old: new for new, old in enumerate(sorted_idx, start=1)}
    order_items = [
        (i + 1, idx_to_id[row[0]], row[1], row[4], row[3], row[2], row[5])
        for i, row in enumerate(items_pending)
    ]
    return orders, order_items, payments, refunds


def order_amount_of(items: list[tuple]) -> Decimal:
    """订单应付金额 = 明细小计之和（Decimal 精确相加，R1）。"""
    return sum((it[4] for it in items), Decimal("0.00"))


def item_count_of(items: list[tuple]) -> int:
    """订单件数 = 明细数量之和（R2）。"""
    return sum(it[3] for it in items)


def _make_refund(order_amount: Decimal, paid_at: datetime, full: bool) -> tuple:
    """构造一条退款：(金额, 原因, 退款完成时间)。

    full=True  → 全额（订单 status='refunded'）
    full=False → 部分退款，退实付的 20%-90%（订单 status 仍是 completed）
    """
    if full:
        amount = order_amount
    else:
        ratio = Decimal(str(random.uniform(0.20, 0.90)))
        amount = round2(order_amount * ratio)
    reason = weighted_choice(REFUND_REASON_WEIGHTS)
    # 退款完成时间：支付后 2 小时 ~ 10 天内；年龄约束保证此时 paid_at 距终点 ≥ 7 天，
    # 截断到 DATA_END 也不会早于支付时间。
    refunded_at = min(paid_at + timedelta(seconds=random.randint(7200, 864000)),
                      DATA_END)
    return (amount, reason, refunded_at)


# ---------------------------------------------------------------------------
# ①' 勾稽自检（设计 §1.7 R1-R8）：写 CSV 之前对内存数据核验，任何一条不过立即报错。
# 慢规则用聚合字典核验、快规则全量逐行核验——10 万行的纯加法循环在 Python 里不到 1 秒。
# ---------------------------------------------------------------------------
def _fail(rule: str, detail: str) -> None:
    raise AssertionError(f"勾稽自检 {rule} 未通过：{detail}")


def check_reconciliation(
    users: list[tuple],
    products: list[tuple],
    orders: list[tuple],
    order_items: list[tuple],
    payments: list[tuple],
    refunds: list[tuple],
) -> None:
    # ---- R1 / R2：订单金额、件数 = 明细聚合（按 order_id 归组聚合，一遍扫完） ----
    amount_by_order: dict[int, Decimal] = {}
    count_by_order: dict[int, int] = {}
    for it in order_items:
        amount_by_order[it[I_ORDER]] = \
            amount_by_order.get(it[I_ORDER], Decimal("0.00")) + it[I_SAMT]
        count_by_order[it[I_ORDER]] = count_by_order.get(it[I_ORDER], 0) + it[I_QTY]
    for o in orders:
        if amount_by_order.get(o[O_ID], Decimal("0.00")) != o[O_AMOUNT]:
            _fail("R1", f"订单 {o[O_ID]} 的 order_amount 与明细小计之和不一致")
        if count_by_order.get(o[O_ID], 0) != o[O_COUNT]:
            _fail("R2", f"订单 {o[O_ID]} 的 item_count 与明细件数之和不一致")

    # ---- R3：行小计 = 单价×数量；单价 = 原价×折扣率（全量逐行，含舍入规则） ----
    price_by_pid = {row[P_ID]: row[P_PRICE] for row in products}
    for it in order_items:
        if it[I_UPRICE] != round2(price_by_pid[it[I_PRODUCT]] * it[I_RATE]):
            _fail("R3", f"明细 {it[I_ID]} 的 unit_price ≠ 原价×折扣率")
        if it[I_SAMT] != round2(it[I_UPRICE] * it[I_QTY]):
            _fail("R3", f"明细 {it[I_ID]} 的 sale_amount ≠ 单价×数量")

    # ---- R4 / R5：有支付订单恰一条支付且金额相等；pending/cancelled 无支付 ----
    pay_amount_by_order: dict[int, Decimal] = {}
    for p in payments:
        if p[PAY_ORDER] in pay_amount_by_order:
            _fail("R4", f"订单 {p[PAY_ORDER]} 有多条支付记录")
        pay_amount_by_order[p[PAY_ORDER]] = p[PAY_AMOUNT]
    for o in orders:
        has_pay = o[O_ID] in pay_amount_by_order
        if o[O_STATUS] in PAY_STATUS:
            if not has_pay:
                _fail("R4", f"订单 {o[O_ID]}（{o[O_STATUS]}）缺少支付记录")
            elif pay_amount_by_order[o[O_ID]] != o[O_AMOUNT]:
                _fail("R4", f"订单 {o[O_ID]} 支付金额 ≠ 订单金额")
        elif has_pay:  # pending/cancelled 等状态不应有支付
            _fail("R5", f"订单 {o[O_ID]}（{o[O_STATUS]}）不该有支付记录")

    # ---- R6：退款 ≤ 支付；全额退款 ⇔ status=refunded；部分退款 status=completed ----
    status_by_order = {o[O_ID]: o[O_STATUS] for o in orders}
    refund_count: Counter = Counter()
    for r in refunds:
        refund_count[r[R_ORDER]] += 1
        pay_amount = pay_amount_by_order[r[R_ORDER]]
        if r[R_AMOUNT] > pay_amount:
            _fail("R6", f"订单 {r[R_ORDER]} 退款金额超过支付金额")
        if r[R_AMOUNT] == pay_amount and status_by_order[r[R_ORDER]] != "refunded":
            _fail("R6", f"订单 {r[R_ORDER]} 全额退款但状态不是 refunded")
        if r[R_AMOUNT] < pay_amount and status_by_order[r[R_ORDER]] != "completed":
            _fail("R6", f"订单 {r[R_ORDER]} 部分退款但状态不是 completed")
    for o in orders:
        n = refund_count.get(o[O_ID], 0)
        if o[O_STATUS] == "refunded" and n != 1:
            _fail("R6", f"全额退款订单 {o[O_ID]} 应恰有 1 条退款记录，实际 {n}")
        if o[O_STATUS] != "refunded" and n > 1:
            _fail("R6", f"订单 {o[O_ID]} 有多条退款记录")

    # ---- R7：时间链 registered_at < created_at ≤ paid_at ≤ refunded_at，都在各自窗口 ----
    # 注册时间在数据期之前的专属窗口（2024-01-01~2025-08-31），
    # 订单/支付/退款时间必须全部落在数据期 2025-10-01~2026-09-30 内。
    registered_by_uid = {u[U_ID]: u[U_REG] for u in users}
    for u in users:
        if not (REGISTER_START <= u[U_REG] <= REGISTER_END):
            _fail("R7", f"用户 {u[U_ID]} 注册时间越出注册窗口")
    for p_row in products:
        if p_row[P_LISTED] >= DATA_START.date():
            _fail("R7", f"商品 {p_row[P_ID]} 上架日期不早于数据期起点")
    paid_at_by_order = {p[PAY_ORDER]: p[PAY_AT] for p in payments}
    for o in orders:
        if not (registered_by_uid[o[O_USER]] < o[O_CREATED]):
            _fail("R7", f"订单 {o[O_ID]} 早于用户注册时间")
        if not (DATA_START <= o[O_CREATED] <= DATA_END):
            _fail("R7", f"订单 {o[O_ID]} 下单时间越出数据期")
        if o[O_ID] in paid_at_by_order:
            paid_at = paid_at_by_order[o[O_ID]]
            if not (o[O_CREATED] <= paid_at <= DATA_END):
                _fail("R7", f"订单 {o[O_ID]} 支付时间不满足 created_at ≤ paid_at ≤ 数据期")
    for r in refunds:
        refunded_at = r[R_AT]
        paid_at = paid_at_by_order[r[R_ORDER]]
        if not (paid_at <= refunded_at <= DATA_END):
            _fail("R7", f"订单 {r[R_ORDER]} 退款时间不满足 paid_at ≤ refunded_at ≤ 数据期")

    # ---- R8：状态-年龄约束 + 记录存在性与状态的互斥完备 ----
    for o in orders:
        age_days = (DATA_END - o[O_CREATED]).days
        min_age = STATUS_MIN_AGE_DAYS.get(o[O_STATUS], 0)
        if age_days < min_age:
            _fail("R8", f"订单 {o[O_ID]} 下单仅 {age_days} 天却处于 {o[O_STATUS]}")
        has_pay = o[O_ID] in pay_amount_by_order
        if (o[O_STATUS] in PAY_STATUS) != has_pay:
            _fail("R8", f"订单 {o[O_ID]} 状态与支付记录存在性不一致")
        if o[O_STATUS] == "refunded" and refund_count.get(o[O_ID], 0) != 1:
            _fail("R8", f"订单 {o[O_ID]} 状态 refunded 但退款记录数 ≠ 1")

    # ---- 行数规模（设计 §1.5）：三表精确，派生表区间断言 ----
    if len(users) != N_USERS:
        _fail("行数", f"users 应为 {N_USERS} 行，实际 {len(users)}")
    if len(products) != N_PRODUCTS:
        _fail("行数", f"products 应为 {N_PRODUCTS} 行，实际 {len(products)}")
    if len(orders) != N_ORDERS:
        _fail("行数", f"orders 应为 {N_ORDERS} 行，实际 {len(orders)}")
    for name, n, lo, hi in (("order_items", len(order_items), 140000, 160000),
                            ("payments", len(payments), 85000, 93000),
                            ("refunds", len(refunds), 8000, 11000)):
        if not (lo <= n <= hi):
            _fail("行数", f"{name} 应在 [{lo}, {hi}] 内，实际 {n}")

    # ---- 月度分布 sanity：每月 > 5000 单，且 2025-11（双 11）是最大月 ----
    month_counter = Counter(o[O_CREATED].strftime("%Y-%m") for o in orders)
    for label, weight, _, _ in MONTHS:
        if month_counter.get(label, 0) <= 5000:
            _fail("月度", f"{label} 订单量 {month_counter.get(label, 0)} ≤ 5000")
    if month_counter["2025-11"] != max(month_counter.values()):
        _fail("月度", "2025-11 不是订单量最大的月，季节权重未生效")


def print_dataset_summary(
    orders: list[tuple], payments: list[tuple], refunds: list[tuple]
) -> None:
    """打印数据"剧情"摘要，供人工审查（月度/状态/重尾/退款）。"""
    print("  数据剧情摘要：")
    month_counter = Counter(o[O_CREATED].strftime("%Y-%m") for o in orders)
    print("    月度订单量：" + ", ".join(
        f"{label}={month_counter.get(label, 0)}" for label, *_ in MONTHS))
    status_counter = Counter(o[O_STATUS] for o in orders)
    print("    状态分布：" + ", ".join(
        f"{s}={status_counter.get(s, 0)}" for s in STATUS_WEIGHTS))

    # 重尾分布实际效果
    per_user = Counter(o[O_USER] for o in orders)
    single = sum(1 for c in per_user.values() if c == 1)
    zero = N_USERS - len(per_user)
    top = sorted(per_user.values(), reverse=True)
    top1 = top[:N_USERS // 100]
    print(f"    重尾分布：人均 {len(orders) / N_USERS:.2f} 单；"
          f"0 单用户 {zero} 人（{zero / N_USERS:.0%}）；"
          f"仅 1 单 {single} 人（{single / N_USERS:.0%}）；"
          f"头部 1% 用户人均 {sum(top1) / len(top1):.0f} 单（最高 {top[0]} 单）")

    # 退款两类行为（双口径素材）
    pay_amount_by_order = {p[PAY_ORDER]: p[PAY_AMOUNT] for p in payments}
    full_n = sum(1 for r in refunds if r[R_AMOUNT] == pay_amount_by_order[r[R_ORDER]])
    print(f"    退款：共 {len(refunds)} 条（全额 {full_n} 条 / 部分 {len(refunds) - full_n} 条）")


# ---------------------------------------------------------------------------
# ①'' 写 CSV
# ---------------------------------------------------------------------------
def write_csvs(dataset: dict[str, list[tuple]]) -> None:
    """把六张表写到 data/dataset/*.csv（覆盖旧文件，可重复执行）。"""
    DATASET_DIR.mkdir(parents=True, exist_ok=True)
    for table in IMPORT_ORDER:
        path = DATASET_DIR / f"{table}.csv"
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(CSV_HEADERS[table])
            writer.writerows(dataset[table])
        print(f"  已写出 {path.relative_to(REPO_ROOT)}（{len(dataset[table])} 行）")


# ---------------------------------------------------------------------------
# 连接与错误提示（无 MySQL 时不甩英文堆栈，给中文排查清单）
# ---------------------------------------------------------------------------
def mysql_connect_or_die():
    """用管理员账号连 MySQL；连不上就打印中文排查清单并退出（返回 pymysql 连接）。"""
    import pymysql  # 延迟导入：只生成 CSV 时完全不需要装/加载 MySQL 驱动

    host = os.environ.get("MYSQL_HOST", "127.0.0.1")
    port = int(os.environ.get("MYSQL_PORT", "3306"))
    admin_user = os.environ.get("MYSQL_ADMIN_USER", "root")
    admin_password = os.environ.get("MYSQL_ADMIN_PASSWORD", "")
    try:
        return pymysql.connect(host=host, port=port, user=admin_user,
                               password=admin_password, charset="utf8mb4",
                               autocommit=False, connect_timeout=5)
    except pymysql.MySQLError as exc:  # 连接/认证类错误统一给中文排查清单
        print("\n[错误] 连不上 MySQL（--init/--load 需要本机 MySQL 已就绪）。请逐条排查：")
        print("  1. Docker 方式（推荐）：确认 Docker Desktop 已安装且正在运行"
              "（任务栏托盘有鲸鱼图标），终端执行 docker --version 能出版本号；")
        print("     没有容器就先启动一个（密码换成你自己的）：")
        print("     docker run -d --name w2-mysql -p 3306:3306 "
              "-e MYSQL_ROOT_PASSWORD=你的密码 mysql:8.0")
        print("  2. 原生安装方式：确认 MySQL 服务已启动——")
        print("     Win+R 输入 services.msc 找到 MySQL80 点“启动”，"
              "或管理员终端执行 net start MySQL80")
        print("  3. 端口检查：终端执行 netstat -ano | findstr 3306，"
              "确认 3306 在监听且没有被其他进程占用；改了端口要同步改 .env 的 MYSQL_PORT")
        print("  4. 密码核对：打开仓库根目录 .env，确认 MYSQL_ADMIN_USER / "
              "MYSQL_ADMIN_PASSWORD 与数据库实际 root 账号一致")
        print("  5. 若错误信息提到 caching_sha2_password 或 cryptography："
              "在 venv 里执行 pip install cryptography 后重试")
        print(f"  原始错误信息：{exc}")
        sys.exit(1)


def read_env_or_die() -> tuple[str, str]:
    """读取管理员密码与只读账号密码；缺失直接报中文错误（不猜默认值）。"""
    admin_password = os.environ.get("MYSQL_ADMIN_PASSWORD", "")
    ro_password = os.environ.get("MYSQL_RO_PASSWORD", "")
    if not admin_password:
        print("[错误] 缺少管理员密码：请在仓库根目录 .env 中配置 MYSQL_ADMIN_PASSWORD"
              "（可参考 .env.example 的 W2 段）。")
        sys.exit(1)
    if not ro_password:
        print("[错误] 缺少只读账号密码：请在仓库根目录 .env 中配置 MYSQL_RO_PASSWORD"
              "（该密码用于创建只读账号 text2sql_ro，Agent 查询就用这个账号）。")
        sys.exit(1)
    return admin_password, ro_password


# ---------------------------------------------------------------------------
# ② 建库建表 + 创建只读账号（--init 或 --load 时执行；幂等：先清表再建）
# ---------------------------------------------------------------------------
def init_database() -> None:
    import pymysql  # 延迟导入，理由同上

    # 只校验配置齐全；管理员密码由 mysql_connect_or_die 内部从环境变量读取
    _, ro_password = read_env_or_die()
    print("② 建库：执行 schema.sql（建 app/mart 库 + 6 张表）并创建只读账号 ...")
    conn = mysql_connect_or_die()
    try:
        with conn.cursor() as cur:
            # 幂等第一步：按外键逆序清掉旧表（外键开着会拦 DROP，先关检查是标准做法）
            cur.execute("SET FOREIGN_KEY_CHECKS = 0")
            cur.execute("DROP TABLE IF EXISTS "
                        "mart.refunds, mart.payments, mart.order_items, "
                        "mart.orders, mart.products, mart.users")
            # 执行 schema.sql：按分号切分语句逐条执行。
            # 本文件只有简单 DDL（无存储过程/触发器），按分号切分是安全的。
            statements = _split_sql_statements(SCHEMA_FILE.read_text(encoding="utf-8"))
            for stmt in statements:
                cur.execute(stmt)
            # 只读账号：密码从 .env 读，拼进 SQL 前先转义引号和反斜杠
            safe_pwd = ro_password.replace("\\", "\\\\").replace("'", "''")
            cur.execute(f"CREATE USER IF NOT EXISTS '{RO_USER}'@'%' "
                        f"IDENTIFIED BY '{safe_pwd}'")
            cur.execute(f"GRANT SELECT ON mart.* TO '{RO_USER}'@'%'")
            cur.execute("FLUSH PRIVILEGES")
            conn.commit()
            cur.execute("SET FOREIGN_KEY_CHECKS = 1")
        print(f"  建库完成：app / mart 两库 + 6 张表；只读账号 {RO_USER} 已授权 SELECT ON mart.*")
    except pymysql.err.Error as exc:
        conn.rollback()
        print(f"\n[错误] 建库失败：{exc}")
        print("  常见原因：MYSQL_ADMIN_PASSWORD 不对；或该账号没有建库建表/建用户权限"
              "（用 root 账号最省事）。修正 .env 后重跑即可（脚本幂等）。")
        sys.exit(1)
    finally:
        conn.close()


def _split_sql_statements(sql_text: str) -> list[str]:
    """把 schema.sql 拆成一条条可执行语句：去掉 -- 注释行，按分号切分。"""
    lines = [ln for ln in sql_text.splitlines() if not ln.strip().startswith("--")]
    return [s.strip() for s in "\n".join(lines).split(";") if s.strip()]


# ---------------------------------------------------------------------------
# ③ 批量导入（仅 --load）：读 CSV → executemany 分批插入，每表一个事务
# ---------------------------------------------------------------------------
def load_database() -> None:
    import pymysql  # 延迟导入，理由同上

    print("③ 导入：把 6 个 CSV 批量写入 MySQL（每批 "
          f"{IMPORT_BATCH_ROWS} 行，每表一个事务）...")
    missing = [f"{t}.csv" for t in IMPORT_ORDER
               if not (DATASET_DIR / f"{t}.csv").exists()]
    if missing:
        print(f"[错误] 缺少 CSV 文件：{', '.join(missing)}。"
              f"请先执行 python data/generate_dataset.py --seed 42 生成数据。")
        sys.exit(1)

    read_env_or_die()  # 导入前同样确保配置齐全（保持与 --init 一致的报错口径）
    conn = mysql_connect_or_die()
    try:
        with conn.cursor() as cur:
            # 管理员连接不带默认数据库，必须显式选中 mart，
            # 否则 INSERT 会报 (1046, 'No database selected')
            db_name = os.environ.get("MYSQL_DB", "mart")
            cur.execute(f"USE `{db_name}`")
            # 导入提速的标准组合拳：关外键检查（导完恢复）+ 手动事务
            cur.execute("SET FOREIGN_KEY_CHECKS = 0")
            for table in IMPORT_ORDER:
                t0 = time.perf_counter()
                with open(DATASET_DIR / f"{table}.csv", newline="",
                          encoding="utf-8") as f:
                    rows = list(csv.reader(f))[1:]  # 第一行是表头，跳过
                columns = ", ".join(CSV_HEADERS[table])
                placeholders = ", ".join(["%s"] * len(CSV_HEADERS[table]))
                sql = f"INSERT INTO `{table}` ({columns}) VALUES ({placeholders})"
                # CSV 里的值都是字符串，MySQL 会按列类型自动转成 DATETIME/DECIMAL/INT
                for i in range(0, len(rows), IMPORT_BATCH_ROWS):
                    cur.executemany(sql, rows[i:i + IMPORT_BATCH_ROWS])
                conn.commit()  # 每表一个事务：要么整表进，要么整表回滚
                print(f"  {table}: 导入 {len(rows)} 行"
                      f"（耗时 {time.perf_counter() - t0:.1f}s）")
            cur.execute("SET FOREIGN_KEY_CHECKS = 1")
            # 打印各表"数据库里实际"的行数摘要（不是 CSV 行数，以库为准）
            print("  导入完成，数据库实际行数：")
            for table in IMPORT_ORDER:
                cur.execute(f"SELECT COUNT(*) FROM `{table}`")
                print(f"    {table}: {cur.fetchone()[0]}")
        conn.commit()
    except pymysql.err.Error as exc:
        conn.rollback()
        print(f"\n[错误] 导入失败：{exc}")
        print("  已回滚本次导入。常见原因：磁盘满 / max_allowed_packet 过小 / "
              "字符集问题。修正后重跑即可（导入前会先清表，可重复执行）。")
        sys.exit(1)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def generate_and_write(seed: int) -> dict[str, list[tuple]]:
    """① 生成阶段：设种子 → 生成六表 → 勾稽自检 → 写 CSV。返回数据供打印摘要。"""
    # 固定种子要同时固定两套随机源：random（订单/状态/时间抽样）和 Faker（中文姓名），
    # 这样同 --seed 生成逐字节相同的 CSV，全链路可复现。
    random.seed(seed)
    Faker.seed(seed)

    t = time.perf_counter()
    print(f"① 生成：seed={seed}，目标行数 users={N_USERS} products={N_PRODUCTS} "
          f"orders={N_ORDERS} ...")
    users = generate_users()
    print(f"  users 生成完毕（{len(users)} 行，耗时 {time.perf_counter() - t:.1f}s）")

    t = time.perf_counter()
    products = generate_products()
    product_weights = build_product_weights(products)
    orders, order_items, payments, refunds = generate_orders(products, product_weights)
    print(f"  orders 及派生表生成完毕（耗时 {time.perf_counter() - t:.1f}s）")

    t = time.perf_counter()
    check_reconciliation(users, products, orders, order_items, payments, refunds)
    print(f"  勾稽自检 R1-R8 全部通过（耗时 {time.perf_counter() - t:.1f}s）")
    print_dataset_summary(orders, payments, refunds)

    t = time.perf_counter()
    dataset = {
        "users": users,
        "products": products,
        "orders": orders,
        "order_items": order_items,
        "payments": payments,
        "refunds": refunds,
    }
    write_csvs(dataset)
    print(f"  CSV 写出完毕（耗时 {time.perf_counter() - t:.1f}s）")
    return dataset


def main() -> None:
    parser = argparse.ArgumentParser(
        description="W2 数据集：生成 CSV / 建库建只读账号 / 批量导入 MySQL",
        epilog="示例：python data/generate_dataset.py --seed 42 --load",
    )
    parser.add_argument("--seed", type=int, default=42,
                        help="随机种子（默认 42，同种子生成完全相同的数据）")
    parser.add_argument("--init", action="store_true",
                        help="建库建表 + 创建只读账号（需要 .env 的 MYSQL_ADMIN_* / MYSQL_RO_PASSWORD）")
    parser.add_argument("--load", action="store_true",
                        help="建库 + 把 CSV 导入 MySQL（包含 --init 的全部动作）")
    parser.add_argument("--skip-generate", action="store_true",
                        help="跳过生成阶段，直接用已有 CSV 做 --init/--load")
    args = parser.parse_args()
    if args.skip_generate and not (args.init or args.load):
        parser.error("--skip-generate 只能与 --init 或 --load 搭配使用")

    # 读仓库根目录的 .env（存在才生效），后续 MySQL 步骤从环境变量取连接信息
    load_dotenv(REPO_ROOT / ".env")

    t_all = time.perf_counter()
    if not args.skip_generate:
        generate_and_write(args.seed)
    else:
        print("① 生成：已跳过（--skip-generate），直接使用 data/dataset/ 下已有 CSV")

    if args.init or args.load:
        init_database()
    if args.load:
        load_database()

    if not (args.init or args.load):
        print("\n生成完成，CSV 已就绪；启动 MySQL 后执行 --load 导入"
              "（连接信息配置在仓库根目录 .env，可参考 .env.example）")
    print(f"总耗时 {time.perf_counter() - t_all:.1f}s")


if __name__ == "__main__":
    main()
