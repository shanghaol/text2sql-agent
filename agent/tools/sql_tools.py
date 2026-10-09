"""SQL 工具：Text-to-SQL 助手的"手"，负责安全地查询 MySQL 分析库（mart 库）。

这是 W2 阶段的核心文件。两个供 Agent 调用的工具：
- execute_readonly_sql：执行只读 SELECT 查询，返回表格文本
- list_tables：列出 mart 库里有哪几张表、各多少行

安全设计是"三层防线"（设计 §1.3，从代码外到代码内）：
1. 只读账号（主防线）：Agent 用 text2sql_ro 连接，仅 SELECT ON mart.*，
   权限在 DBMS 内核层执行，代码任何 bug 都绕不过；
2. 会话只读（辅助防线）：SQLAlchemy Engine 挂 connect 事件，每个池化连接
   建立时执行一次 SET SESSION TRANSACTION READ ONLY——就算将来有人误把
   高权限账号配进 .env，会话级只读仍拦住普通表写入；
3. 代码层校验（保留 W1）：validate_readonly_sql 黑名单 + 首关键字检查，
   在 SQL 出手前给出中文可读拒绝原因，让模型自我修正（前两层只会报英文）。

连接层：SQLAlchemy 2.x Engine + PyMySQL 驱动，模块级懒加载单例
（import 本模块不需要 MySQL 在跑，纯函数测试零依赖）；
测试用 set_engine() 注入（延续 W1 set_db_path 的注入模式，只是注入对象换成了 Engine）。

已知局限（W1 故意的保守策略，W5 会换成 sqlglot 做 AST 级解析）：
- 黑名单用整词匹配，写在字符串字面量里的关键字（如 WHERE status = 'update'）
  会被误拒——宁可错杀，不可放过；
- enforce_limit 无法识别子查询里的 LIMIT，会误判为"已有 LIMIT"而不追加。
"""

import re

from langchain.tools import tool
from sqlalchemy import URL, create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from agent.config import MYSQL_DB, MYSQL_HOST, MYSQL_PASSWORD, MYSQL_PORT, MYSQL_USER

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
        connect_args={"connect_timeout": 5},
        pool_pre_ping=True,
    )


def _attach_session_read_only(engine: Engine) -> Engine:
    """第二层防线：每个池化连接建立时执行一次会话只读（设计 §1.3）。

    SET SESSION TRANSACTION READ ONLY 作用于该连接的整个生命周期，
    随连接池复用，成本为零。挂在 _build_engine 产物上，
    这样无论引擎从哪条路径创建，防线都不会缺席。
    """

    @event.listens_for(engine, "connect")
    def _set_session_read_only(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        try:
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
# 第一道代码防线：SQL 只读校验
# ---------------------------------------------------------------------------

# 写操作关键字黑名单。用 \b 词边界匹配，大小写不敏感。
# 说明：\b 是"单词字符与非单词字符的边界"，下划线属于单词字符，
# 所以 created_at / updated_at 这类列名不会被 \bUPDATE\b 误伤；
# 但字符串字面量 'update' 里的 update 会被误拒——W1 就接受这种保守误杀。
# 排序注意：多词短语放最前——FOR UPDATE 里含单词 UPDATE，先匹配短语
# 才能给模型报出最具体的原因（"检测到 FOR UPDATE"而不是笼统的 UPDATE）。
# W2 新增 6 个 MySQL 特有词（设计 §1.3）：
# - SELECT ... FOR UPDATE / FOR SHARE 是 MySQL 合法的锁读，SQLite 没有这语法；
# - SELECT ... INTO OUTFILE / DUMPFILE 能以 SELECT 开头写服务器文件系统；
# - LOCK IN SHARE MODE 同为锁读；CALL 调用存储过程（库里有未知代码）。
_BLACKLIST_KEYWORDS = (
    "FOR UPDATE",
    "FOR SHARE",
    "LOCK IN SHARE MODE",
    "INTO OUTFILE",
    "INTO DUMPFILE",
    "CALL",
    "INSERT",
    "UPDATE",
    "DELETE",
    "DROP",
    "ALTER",
    "CREATE",
    "REPLACE",
    "TRUNCATE",
    "ATTACH",
    "DETACH",
    "PRAGMA",
    "VACUUM",
    "REINDEX",
    "GRANT",
    "REVOKE",
)


def _strip_comments(sql: str) -> str:
    """剥掉 SQL 注释，返回处理后的文本。

    先删 /* ... */ 块注释（re.DOTALL 让 . 也能匹配换行），
    再删 -- 开头到行尾的行注释。
    校验、LIMIT 判断都必须基于剥完注释的文本，
    否则注释里的关键字/分号/LIMIT 会造成误判。
    """
    cleaned = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    cleaned = re.sub(r"--[^\n]*", " ", cleaned)
    return cleaned


def validate_readonly_sql(sql: str) -> tuple[bool, str]:
    """校验一段 SQL 是否是安全的只读查询。

    检查顺序：剥注释 → 空检查 → 多语句检查 → 首关键字检查 → 黑名单检查。

    返回:
        (是否通过, 拒绝原因)。通过时第二个元素是空字符串；
        拒绝原因写成中文完整句子，让模型能看懂并自己改 SQL 重试。
    """
    # 1) 剥掉注释，避免"注释里藏关键字"或"注释里藏分号"造成误判
    cleaned = _strip_comments(sql).strip()

    # 2) 剥完注释后为空 → 没有可执行的 SQL
    if not cleaned:
        return False, "拒绝执行：SQL 为空或只包含注释。请提供一条 SELECT 查询语句。"

    # 3) 多语句检查：只允许结尾一个分号（MySQL 一次 execute 本来也只跑一条），
    #    内部再出现分号说明想一次跑多条语句，直接拒绝。
    body = cleaned[:-1].strip() if cleaned.endswith(";") else cleaned
    if ";" in body:
        return False, "拒绝执行：一次只能执行一条 SQL 语句（检测到语句中间的分号）。请去掉多余的语句，一次只执行一条查询。"

    # 4) 首个关键字必须是 SELECT 或 WITH（CTE 查询也允许），大小写不敏感
    first_word = re.match(r"[A-Za-z]+", body)
    if first_word is None or first_word.group(0).upper() not in ("SELECT", "WITH"):
        return False, "拒绝执行：SQL 必须以 SELECT 或 WITH 开头。本工具只允许只读查询。"

    # 5) 黑名单整词检查：命中任何写操作关键字即拒绝。
    #    用 \b 词边界避免把 created_at 这类下划线词误判成 UPDATE；
    #    FOR UPDATE / LOCK IN SHARE MODE 等多词短语同样按 \b... 整体匹配，
    #    结尾 \b 能正确落在 UPDATE/SHARE/OUTFILE 这类词的词尾。
    for keyword in _BLACKLIST_KEYWORDS:
        if re.search(rf"\b{keyword}\b", body, flags=re.IGNORECASE):
            return False, f"拒绝执行：检测到非只读关键字 {keyword}。本工具只允许 SELECT 查询。请改写为只读查询后重试。"

    return True, ""


def enforce_limit(sql: str, default_limit: int = 20) -> str:
    """给没有 LIMIT 的查询追加 ` LIMIT 20`，防止一次捞回全表。

    简化实现：只要 SQL 里出现 `LIMIT <数字>` 就认为用户已经限制了行数。
    判断基于剥掉注释后的文本——否则 `/* LIMIT 5 */` 这种注释会骗过检查。
    局限：无法区分 LIMIT 写在主查询还是子查询里——
    子查询里写了 LIMIT 时主查询会被误判为"已有 LIMIT"而不追加。
    W5 换 sqlglot 后会按 AST 精确判断最外层查询的 LIMIT。
    """
    stripped = _strip_comments(sql).strip()
    # 先摘掉结尾的分号，避免拼出 "...; LIMIT 20" 这种语法错误
    if stripped.endswith(";"):
        stripped = stripped[:-1].rstrip()

    if re.search(r"\bLIMIT\s+\d+\b", stripped, flags=re.IGNORECASE):
        return stripped  # 已有 LIMIT，原样返回
    return f"{stripped} LIMIT {default_limit}"


# ---------------------------------------------------------------------------
# 工具一：执行只读 SQL 查询
# ---------------------------------------------------------------------------

# 单次最多返回给模型的行数：结果太长会挤占上下文、模型也读不过来
MAX_ROWS_SHOWN = 20

# W5 预留（W2 只留占位说明，不实现）：执行超时将用 MySQL 原生
# SET SESSION max_execution_time（毫秒，仅作用于 SELECT）实现，
# 可挂在 get_engine() 的 connect 事件里与"会话只读"一起设置。


def _humanize_db_error(exc: Exception) -> str:
    """把数据库异常翻译成"中文提示 + 原始报错"的可读信息。

    pymysql 的错误码在异常链最内层（exc.orig），按 errno 做中文映射
    （设计 §4.1 映射表）；映射后仍拼接原始 message（截 300 字符）——
    模型靠原文里的表名/列名自我修正 SQL。
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
    # 先记录用户 SQL 里是否本来就写了 LIMIT（基于剥注释后的文本判断，
    # 避免 /* LIMIT 5 */ 这种注释造成误判）：自动追加 LIMIT 的情况，
    # 若恰好返回 20 行，按"可能被截断"处理（见下面第四步）。
    had_explicit_limit = bool(re.search(r"\bLIMIT\s+\d+\b", _strip_comments(sql), flags=re.IGNORECASE))
    safe_sql = enforce_limit(sql)

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
