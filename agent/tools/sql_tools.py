"""SQL 工具：Text-to-SQL 助手的"手"，负责安全地查询 MySQL 分析库（mart 库）。

两个供 Agent 调用的工具：
- execute_readonly_sql：执行只读 SELECT 查询，返回表格文本
- list_tables：列出 mart 库里有哪几张表、各多少行

安全设计是"四层防线"（W5 设计 §1.3/§2，从代码外到代码内）：
1. 只读账号（主防线）：Agent 用 text2sql_ro 连接，仅 SELECT ON mart.*，
   权限在 DBMS 内核层执行，代码任何 bug 都绕不过；
2. 会话只读 + 执行超时（辅助防线）：SQLAlchemy Engine 挂 connect 事件，每个池化
   连接建立时执行一次 SET SESSION TRANSACTION READ ONLY（拦住普通表写入）+
   SET SESSION max_execution_time（SELECT 超时由服务端主动终止，保护 2G 内存机器）；
3. 代码层校验（W5 升级为 sqlglot AST 白名单）：validate_readonly_sql 在 SQL 出手前
   给出中文可读拒绝原因，让模型自我修正（前两层只会报英文）——实现见
   agent/tools/sql_guard.py，本模块按 config.SQLGUARD_MODE 二选一导入
   （ast=默认 / legacy=W1 正则逃生门）；
4. 驱动层读超时 read_timeout（兜底）：覆盖结果传输卡死、服务器假死等
   服务端 max_execution_time 管不到的边角。

连接层：SQLAlchemy 2.x Engine + PyMySQL 驱动，模块级懒加载单例
（import 本模块不需要 MySQL 在跑，纯函数测试零依赖）；
测试用 set_engine() 注入（延续 W1 set_db_path 的注入模式，只是注入对象换成了 Engine）。

W5 已解决的 W1 已知局限（AST 校验，设计 §1）：
- 字符串字面量里的关键字误杀（WHERE status = 'update'）——AST 只看结构，
  字面量是 exp.Literal 节点，不再误拒；
- 子查询里的 LIMIT 令外层漏追加——AST 只看根节点的 limit 参数，
  外层缺失必追加（`/* LIMIT 5 */` 注释场景同修复）。

新边界（W5 已知的精度损失，设计 §1.5）：
- SELECT ... INTO OUTFILE / DUMPFILE 在 MySQL 方言下 sqlglot 直接解析报错，
  用户看到的是"语法错误"文案而非专门的"写文件"提示——两种路径都拒绝成功，
  只是拒绝理由的针对性不同；SELECT ... INTO @var 有专门的 INTO 拒绝文案。
"""

from langchain.tools import tool
from sqlalchemy import URL, create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError, SQLAlchemyError

from agent.config import (
    MYSQL_DB,
    MYSQL_HOST,
    MYSQL_MAX_EXECUTION_MS,
    MYSQL_PASSWORD,
    MYSQL_PORT,
    MYSQL_USER,
    SQLGUARD_MODE,
)

# ---------------------------------------------------------------------------
# 代码层校验（第三道防线）：按 SQLGUARD_MODE 二选一导入（W5 设计 §1.2/§1.4）
# ---------------------------------------------------------------------------
# ast（默认）：sqlglot AST 白名单（R1-R5 + R6 LIMIT 判定），见 sql_guard.py；
# legacy：W1 正则实现逃生门——线上出现设计未预料的误杀时，
# 环境变量 TEXT2SQL_SQLGUARD=legacy 一键切回正则实现恢复服务（W6 删除）。
# 两套实现的公共签名一致（validate_readonly_sql / enforce_limit / _prepare），
# 调用点零分支。
if SQLGUARD_MODE == "legacy":
    # 注：三条分开写是 ruff isort 的规范形态（as 导入默认不合并且受行宽约束），
    # 不要"顺手"合并成括号多行——会被 I001 打回。
    from agent.tools.sql_guard import _legacy_enforce_limit as enforce_limit
    from agent.tools.sql_guard import _legacy_prepare as _prepare
    from agent.tools.sql_guard import (
        _legacy_validate_readonly_sql as validate_readonly_sql,
    )
else:
    from agent.tools.sql_guard import _prepare, enforce_limit, validate_readonly_sql

# 公共兼容面：validate_readonly_sql / enforce_limit 自 W1 起就定义在本模块，
# 评测与测试一直按 sql_tools.<名字> 调用；W5 实现迁入 sql_guard 后在此保持
# 同名再导出（enforce_limit 在执行链里不再被本模块调用，故进 __all__ 声明
# "导出是有意的"，同时安抚 ruff F401）。
__all__ = ["enforce_limit", "execute_readonly_sql", "list_tables", "validate_readonly_sql"]

# ---------------------------------------------------------------------------
# 连接管理：模块级懒加载单例 + 测试注入点
# ---------------------------------------------------------------------------

# 模块级 Engine 缓存：两个工具都从这里取连接（连接池复用，免每轮 TCP 握手）。
_engine: Engine | None = None


def _build_engine() -> Engine:
    """按 config 里的 MySQL 配置创建 SQLAlchemy Engine。

    用 URL.create 而不是手拼连接串：密码里的 @ / : 等特殊字符会被正确转义
    （手拼 f"...{password}@..." 遇到特殊字符密码会连错主机）。
    - connect_timeout=5：MySQL 不在时 5 秒内失败，不吊死用户；
    - read_timeout：驱动层读超时（第四层防线，W5 设计 §2.1 兜底）——socket 级，
      覆盖结果集传输卡死、服务器假死等服务端 max_execution_time 管不到的边角；
      取值 = 服务端超时秒数 + 5 的余量，让 errno 3024 的可读文案
      优先于驱动层超时出现（服务端先超时，客户端后放弃）。
    - pool_pre_ping=True：每次借连接前先 ping 一下，容器重启后旧连接自动换新。
    """
    url = URL.create(
        drivername="mysql+pymysql",
        username=MYSQL_USER,
        password=MYSQL_PASSWORD,
        host=MYSQL_HOST,
        port=MYSQL_PORT,
        database=MYSQL_DB,
        query={"charset": "utf8mb4"},
    )
    return create_engine(
        url,
        connect_args={
            "connect_timeout": 5,
            "read_timeout": MYSQL_MAX_EXECUTION_MS / 1000 + 5,
        },
        pool_pre_ping=True,
    )


def _attach_session_read_only(engine: Engine) -> Engine:
    """第二层防线：每个池化连接建立时执行一次会话加固（W5 设计 §1.3/§2.1）。

    两条 SET 都作用于该连接的整个生命周期，随连接池复用，成本为零：
    - SET SESSION TRANSACTION READ ONLY：会话只读（W2 起）；
    - SET SESSION max_execution_time：SELECT 执行超时（W5 新增，主防线）——
      服务端主动终止超时查询、释放 DB 资源，而非客户端单方面放弃；
      R2 白名单已限定纯 SELECT，"仅 SELECT 生效"的限制恰好不构成覆盖缺口。
    挂在 _build_engine 产物上，这样无论引擎从哪条路径创建，防线都不会缺席。
    """

    @event.listens_for(engine, "connect")
    def _harden_session(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        try:
            # 先设超时再设只读：两条都是会话级 SET，顺序无硬约束，
            # 把"会话属性"的设置集中放在只读生效之前，读代码时不易误读
            cursor.execute(f"SET SESSION max_execution_time={MYSQL_MAX_EXECUTION_MS}")
            cursor.execute("SET SESSION TRANSACTION READ ONLY")
        finally:
            cursor.close()

    return engine


def get_engine() -> Engine:
    """返回本模块使用的 Engine；首次调用才真正创建（懒加载单例）。"""
    global _engine
    if _engine is None:
        _engine = _attach_session_read_only(_build_engine())
    return _engine


def set_engine(engine: Engine | None) -> None:
    """替换本模块使用的 Engine（主要供测试注入使用）。传 None 恢复懒加载。"""
    global _engine  # 全局变量是有意为之的测试注入点
    if _engine is not None:
        _engine.dispose(close=False)  # best-effort 释放旧池，防 TCP 连接泄漏到 GC
    _engine = engine


# ---------------------------------------------------------------------------
# 工具一：执行只读 SQL 查询
# ---------------------------------------------------------------------------

# 单次最多返回给模型的行数：结果太长会挤占上下文、模型也读不过来
MAX_ROWS_SHOWN = 20


def _humanize_db_error(exc: Exception) -> str:
    """把数据库异常翻译成"中文提示 + 原始报错"的可读信息。

    pymysql 的错误码在异常链最内层（exc.orig），按 errno 做中文映射
    （W2 设计 §4.1 映射表 + W5 设计 §2.3 超时两条）；映射后仍拼接原始 message
    （截 300 字符）——模型靠原文里的表名/列名自我修正 SQL。
    """
    orig = getattr(exc, "orig", None)
    raw = str(orig) if orig is not None else str(exc)
    raw = raw[:300]

    errno = None
    if orig is not None:
        args = getattr(orig, "args", ())
        if args and isinstance(args[0], int):
            errno = args[0]

    if errno == 1054:
        return f"SQL 错误：查询的列不存在，请对照参考资料中的列名检查拼写。原始报错：{raw}"
    if errno == 1146:
        return f"SQL 错误：查询的表不存在，可用表请先用 list_tables 工具确认。原始报错：{raw}"
    if errno == 1064:
        return f"SQL 错误：SQL 语法错误。注意本项目是 MySQL 方言（日期用 DATE_FORMAT/DATE_SUB，字符串拼接用 CONCAT，不能用 SQLite 的 strftime）。原始报错：{raw}"
    if errno == 1055:
        return f"SQL 错误：GROUP BY 查询中出现了既未聚合也未出现在 GROUP BY 里的列（MySQL 默认 ONLY_FULL_GROUP_BY），请为该列套聚合函数或加入 GROUP BY。原始报错：{raw}"
    if errno in (1142, 1144):
        return f"SQL 错误：只读账号权限不足，该操作被数据库拒绝。请确认这是纯 SELECT 查询。原始报错：{raw}"
    # W5 §2.3：服务端执行超时（max_execution_time 命中，pymysql errno 3024）。
    # 秒数取自 config：默认 10000ms → 文案即设计原文的"超过 10 秒"，env 调大后文案同步。
    if errno == 3024:
        seconds = max(MYSQL_MAX_EXECUTION_MS // 1000, 1)
        return (
            f"SQL 执行超时（超过 {seconds} 秒被数据库终止）。"
            "请缩小时间范围（如把全年改查最近 30 天）、减少聚合粒度或增加过滤条件后重试。"
        )
    # W5 §2.3：驱动层读超时（read_timeout 命中）。§8-⑩ 实测（2026-10-11，pymysql 1.2.3）：
    # socket 超时被 pymysql 包装成 OperationalError(2013, "Lost connection ... (timed out)")，
    # errno 不为 None；识别条件取 errno ∈ {None, 2013} 且 message 含 timed out，
    # 同时覆盖"无 errno 的裸形态"（设计原判据）与实测形态。
    # 文案不承诺服务端已终止——终止语义留给服务端 max_execution_time 防线。
    if (
        isinstance(exc, OperationalError)
        and errno in (None, 2013)
        and "timed out" in raw.lower()
    ):
        return "查询等待超时，请缩小时间范围或增加过滤条件后重试。"
    return f"SQL 错误：{raw}。请检查表名、列名和语法后修正 SQL 重试。"


@tool
def execute_readonly_sql(sql: str) -> str:
    """对电商分析库（MySQL 8.0 的 mart 库，只读）执行 SQL 查询，返回表格文本结果。

    只允许 SELECT / WITH 开头的查询，任何写操作（INSERT/UPDATE/DELETE/DROP 等）
    都会被拒绝。SQL 必须使用 MySQL 方言：日期格式化用 DATE_FORMAT(列, '%Y-%m')，
    当前日期用 CURDATE()，日期回退用 DATE_SUB('2026-09-30', INTERVAL n DAY)，
    字符串拼接用 CONCAT(a, b)，不能用 SQLite 的 strftime/date('now')。
    查询结果最多返回 20 行，超出会截断并提示。

    Args:
        sql: 完整的 MySQL SELECT 语句。例如：
            SELECT DATE_FORMAT(created_at, '%Y-%m') AS month, COUNT(*) AS order_count
            FROM orders
            GROUP BY month ORDER BY month
    """
    # 第一步：代码层只读校验（第三道防线，见模块 docstring 的防线清单）
    is_valid, reason = validate_readonly_sql(sql)
    if not is_valid:
        # 把拒绝原因原样返回：模型读到后能理解规则并自我纠正
        return reason

    # 第二步：补 LIMIT，防止一条 SQL 捞回全表。
    # safe_sql（执行用原文，只摘尾分号）与 had_explicit_limit 一并来自 _prepare
    # （W5：AST 判定最外层 LIMIT，旧的正则二次判断删除）。自动追加 LIMIT 的情况，
    # 若恰好返回 20 行，按"可能被截断"处理（见下面第四步）。
    safe_sql, had_explicit_limit = _prepare(sql)

    # 第三步：从连接池取连接执行（账号只读 + 会话只读两道防线已就位）。
    try:
        with get_engine().connect() as conn:
            result = conn.execute(text(safe_sql))
            # result.keys() 是查询结果的列名视图，list() 后按顺序取第 0 列名
            columns = list(result.keys())
            # 多取一行（21 行）用于判断"是否被截断"
            rows = result.fetchmany(MAX_ROWS_SHOWN + 1)
    except SQLAlchemyError as exc:  # 含死锁(1213)/池超时等，统一走可读化而非穿透
        # 按 pymysql errno 翻译成中文提示 + 原始报错（模型靠原文自我修正）
        return _humanize_db_error(exc)

    # 第四步：格式化成模型易读的表格文本
    if not rows:
        return "查询成功，但结果为空。"

    is_truncated = len(rows) > MAX_ROWS_SHOWN
    shown_rows = rows[:MAX_ROWS_SHOWN]

    # 两种情况都要提示"可能被截断"：
    # a) 实际取回了超过 20 行（fetchmany 多取的那 1 行）；
    # b) 我们自动补了 LIMIT 20，且结果恰好是 20 行——很可能真实结果不止 20 行。
    #    这种情况下提醒模型：如果数据不对劲，可以加更精确的条件重新查询。
    maybe_truncated = not had_explicit_limit and len(shown_rows) == MAX_ROWS_SHOWN

    lines = [f"共 {len(shown_rows)} 行"]
    lines.append(" | ".join(columns))
    for row in shown_rows:
        lines.append(" | ".join(str(value) for value in row))
    if is_truncated or maybe_truncated:
        lines.append(f"（结果已截断，仅显示前 {MAX_ROWS_SHOWN} 行）")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 工具二：列出库中的表
# ---------------------------------------------------------------------------

# 每张表的一句话用途说明（与 data/rag_corpus/schema_docs/ 表文档口径一致），
# 帮助模型（和人）快速理解这个库是干什么的
_TABLE_DESCRIPTIONS = {
    "users": "用户表：用户 ID、姓名、性别、城市/省份、注册时间（约 2 万行）",
    "products": "商品表：商品 ID、名称、一级类目（8 类）、品牌、原价（元）、上架日期（约 2 千行）",
    "orders": "订单主表（表头）：订单号、下单用户、状态、订单金额/件数（冗余汇总列）、下单时间（约 10 万行）",
    "order_items": "订单明细表：一行 = 订单里的一种商品（数量、成交单价、折扣率、行小计）（约 15 万行）",
    "payments": "支付流水表：一笔成功支付恰一条（实付金额、支付方式、支付时间）（约 8.9 万行）",
    "refunds": "退款记录表：退款金额、原因、退款完成时间；一单至多一条（约 9.3 千行）",
}


@tool
def list_tables() -> str:
    """列出电商分析库（MySQL mart 库）中的所有数据表，包括每张表的行数和用途说明。

    在写 SQL 之前调用它，可以快速了解库里有哪些表可以查。
    """
    try:
        with get_engine().connect() as conn:
            # information_schema 是 MySQL 的系统库；table_schema = DATABASE()
            # 表示"当前连接的库"（mart），不会看到别的库
            table_rows = conn.execute(
                text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = DATABASE() ORDER BY table_name"
                )
            ).fetchall()
            lines = []
            for (name,) in table_rows:
                # 行数逐表 COUNT(*) 精确统计：information_schema.tables.table_rows
                # 是 InnoDB 的估算值（不精确），表小 COUNT 很快，准确性优先。
                # 表名加反引号包裹：虽来自 information_schema、连接只读，
                # 仍按 SQL 习惯做标识符卫生。
                row_count = conn.execute(text(f"SELECT COUNT(*) FROM `{name}`")).scalar()
                description = _TABLE_DESCRIPTIONS.get(name, "（暂无说明）")
                lines.append(f"- {name}（{row_count} 行）：{description}")
    except SQLAlchemyError as exc:  # 含死锁(1213)/池超时等，统一走可读化而非穿透
        return f"读取数据库失败：{_humanize_db_error(exc)}"

    if not lines:
        return "数据库中没有任何表。请先运行 python data/generate_dataset.py --seed 42 --load 建库导入。"
    return f"数据库中共有 {len(lines)} 张表：\n" + "\n".join(lines)
