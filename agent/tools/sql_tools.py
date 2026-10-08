"""SQL 工具：Text-to-SQL 助手的"手"，负责安全地查询演示数据库。

这是 W1 阶段的核心文件。两个供 Agent 调用的工具：
- execute_readonly_sql：执行只读 SELECT 查询，返回表格文本
- list_tables：列出库里有哪几张表、各多少行

安全设计是"两道防线"：
1. 代码层校验：validate_readonly_sql 检查 SQL 是否只读（第一道防线）；
2. 文件系统层限制：用 `mode=ro` 只读 URI 打开 SQLite（第二道防线），
   即使第一道防线被绕过，数据库文件本身也写不进去。

已知局限（W1 故意的保守策略，W5 会换成 sqlglot 做 AST 级解析）：
- 黑名单用整词匹配，写在字符串字面量里的关键字（如 WHERE status = 'update'）
  会被误拒——宁可错杀，不可放过；
- enforce_limit 无法识别子查询里的 LIMIT，会误判为"已有 LIMIT"而不追加。
"""

import re
import sqlite3
from pathlib import Path

from langchain.tools import tool

from agent.config import DB_PATH as DEFAULT_DB_PATH

# 模块级数据库路径：两个工具都从这里取路径。
# 初始值来自 config（可被环境变量 DEMO_DB_PATH 覆盖）；
# 测试时用 set_db_path() 注入临时库路径，避免碰真实数据文件。
DB_PATH: Path = DEFAULT_DB_PATH


def set_db_path(p: Path) -> None:
    """替换本模块使用的数据库路径（主要供测试注入临时库使用）。"""
    global DB_PATH  # 全局变量是有意为之的测试注入点
    DB_PATH = Path(p)


# ---------------------------------------------------------------------------
# 第一道防线：SQL 只读校验
# ---------------------------------------------------------------------------

# 写操作关键字黑名单。用 \b 词边界匹配，大小写不敏感。
# 说明：\b 是"单词字符与非单词字符的边界"，下划线属于单词字符，
# 所以 created_at / updated_at 这类列名不会被 \bUPDATE\b 误伤；
# 但字符串字面量 'update' 里的 update 会被误拒——W1 接受这种保守误杀。
_BLACKLIST_KEYWORDS = (
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

    # 3) 多语句检查：只允许结尾一个分号（SQLite 一次 execute 本来也只跑一条），
    #    内部再出现分号说明想一次跑多条语句，直接拒绝。
    body = cleaned[:-1].strip() if cleaned.endswith(";") else cleaned
    if ";" in body:
        return False, "拒绝执行：一次只能执行一条 SQL 语句（检测到语句中间的分号）。请去掉多余的语句，一次只执行一条查询。"

    # 4) 首个关键字必须是 SELECT 或 WITH（CTE 查询也允许），大小写不敏感
    first_word = re.match(r"[A-Za-z]+", body)
    if first_word is None or first_word.group(0).upper() not in ("SELECT", "WITH"):
        return False, "拒绝执行：SQL 必须以 SELECT 或 WITH 开头。本工具只允许只读查询。"

    # 5) 黑名单整词检查：命中任何写操作关键字即拒绝。
    #    用 \b 词边界避免把 created_at 这类下划线词误判成 UPDATE。
    for keyword in _BLACKLIST_KEYWORDS:
        if re.search(rf"\b{keyword}\b", body, flags=re.IGNORECASE):
            return False, f"拒绝执行：检测到写操作关键字 {keyword}。本工具只允许 SELECT 查询。请改写为只读查询后重试。"

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


@tool
def execute_readonly_sql(sql: str) -> str:
    """对电商演示库执行只读 SQL 查询（SQLite 方言），返回表格文本结果。

    只允许 SELECT / WITH 开头的查询，任何写操作（INSERT/UPDATE/DELETE/DROP 等）
    都会被拒绝。查询结果最多返回 20 行，超出会截断并提示。

    Args:
        sql: 完整的 SQLite SELECT 语句。例如：
            SELECT u.city, COUNT(*) AS order_count
            FROM orders o JOIN users u ON o.user_id = u.id
            GROUP BY u.city ORDER BY order_count DESC
    """
    # 第一步：代码层只读校验（第一道防线）
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

    # 第三步：以只读模式连接（第二道防线）。
    # Path.as_uri() 生成 file:///C:/... 形式的 URI，再加 ?mode=ro（read only），
    # 这样即使 SQL 校验被绕过，SQLite 也会从文件系统层面拒绝写入。
    uri = f"{DB_PATH.resolve().as_uri()}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
        try:
            cursor = conn.execute(safe_sql)
            # cursor.description 是查询结果的列信息元组，每项第 0 个元素是列名
            columns = [description[0] for description in cursor.description]
            # 多取一行（21 行）用于判断"是否被截断"
            rows = cursor.fetchmany(MAX_ROWS_SHOWN + 1)
        finally:
            conn.close()
    except sqlite3.Error as exc:
        # 把 SQLite 的原始错误信息透传给模型：比如列名拼错时，
        # SQLite 会提示 "no such column: xxx"，模型看到就能自己修正 SQL。
        return f"SQL 错误：{exc}。请检查表名、列名和语法后修正 SQL 重试。"

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

# 每张表的一句话用途说明，帮助模型（和人）快速理解这个库是干什么的
_TABLE_DESCRIPTIONS = {
    "users": "用户表：用户 ID、姓名、所在城市、注册日期",
    "products": "商品表：商品 ID、名称、类目、单价（元）",
    "orders": "订单表：订单 ID、下单用户、购买商品、数量、金额、状态、下单时间",
}


@tool
def list_tables() -> str:
    """列出电商演示库中的所有数据表，包括每张表的行数和用途说明。

    在写 SQL 之前调用它，可以快速了解库里有哪些表可以查。
    """
    uri = f"{DB_PATH.resolve().as_uri()}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
        try:
            # sqlite_master 是 SQLite 的系统表，存放所有表的定义；
            # 过滤掉 name 以 sqlite_ 开头的内部表
            table_rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
            lines = []
            for (name,) in table_rows:
                # 表名加双引号包裹：虽然这里 name 来自 sqlite_master、连接是只读，
                # 仍按 SQL 习惯做标识符卫生，避免特殊字符表名出问题
                row_count = conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
                description = _TABLE_DESCRIPTIONS.get(name, "（暂无说明）")
                lines.append(f"- {name}（{row_count} 行）：{description}")
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return f"读取数据库失败：{exc}"

    if not lines:
        return "数据库中没有任何表。请先运行 python -m agent.db.seed 建库。"
    return f"数据库中共有 {len(lines)} 张表：\n" + "\n".join(lines)
