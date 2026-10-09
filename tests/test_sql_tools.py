"""SQL 工具的单元测试。

设计原则：**默认套件完全不依赖 MySQL 和 DEEPSEEK_API_KEY**——
validate_readonly_sql / enforce_limit 都是纯函数，任何机器裸跑全绿；
执行类测试依赖真实 MySQL（mart 库），用环境变量门控 + skipif 跳过：
门控条件 = 测试文件顶部自己 load_dotenv() 后检查 MYSQL_ADMIN_PASSWORD
已配置且 MySQL 端口可连通（见下方 _mysql_reachable）。
"""

import os
import socket
from pathlib import Path

import pytest
from dotenv import load_dotenv

from agent.tools import sql_tools

# ---------------------------------------------------------------------------
# MySQL 门控条件（无 MySQL 的机器：以下用例全部跳过，默认套件不受影响）
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[1]
# pytest 不会自动加载 .env：文件顶部显式 load 一次（agent.config 导入时也会
# load，这里不依赖导入顺序，保证门控条件自成一体、可独立读懂）。
load_dotenv(REPO_ROOT / ".env")


def _mysql_reachable() -> bool:
    """门控条件：只读账号凭证可用（MYSQL_PASSWORD 或回退 MYSQL_RO_PASSWORD），且 MySQL 端口可连通。"""
    if not (os.environ.get("MYSQL_PASSWORD") or os.environ.get("MYSQL_RO_PASSWORD")):
        return False
    host = os.environ.get("MYSQL_HOST", "127.0.0.1")
    port = int(os.environ.get("MYSQL_PORT", "3306"))
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False


MYSQL_AVAILABLE = _mysql_reachable()
requires_mysql = pytest.mark.skipif(
    not MYSQL_AVAILABLE,
    reason="需要本机 MySQL：.env 配置 MYSQL_ADMIN_PASSWORD 且端口可连通",
)


# ---------------------------------------------------------------------------
# validate_readonly_sql：只读校验（纯函数）
# ---------------------------------------------------------------------------


def test_validate_select_passes():
    """普通 SELECT 应该通过。"""
    ok, reason = sql_tools.validate_readonly_sql("SELECT * FROM orders")
    assert ok
    assert reason == ""


def test_validate_lowercase_select_passes():
    """小写 select 也要能通过（关键字匹配必须大小写不敏感）。"""
    ok, reason = sql_tools.validate_readonly_sql("select name from users limit 3")
    assert ok
    assert reason == ""


def test_validate_with_cte_passes():
    """WITH 开头的 CTE 查询是合法的只读查询，应通过。"""
    sql = """
    WITH city_orders AS (
        SELECT user_id, COUNT(*) AS cnt FROM orders GROUP BY user_id
    )
    SELECT * FROM city_orders
    """
    ok, reason = sql_tools.validate_readonly_sql(sql)
    assert ok
    assert reason == ""


def test_validate_select_with_line_comment_passes():
    """带行注释的 SELECT：注释应该被剥掉后再判断，不影响通过。"""
    sql = "SELECT * FROM users -- 查询所有用户\n"
    ok, reason = sql_tools.validate_readonly_sql(sql)
    assert ok
    assert reason == ""


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM orders WHERE id = 1",
        "DROP TABLE users",
        "INSERT INTO users VALUES (99, '黑客', '月球', '2026-01-01')",
        "UPDATE users SET city = '火星' WHERE id = 1",
        "PRAGMA table_info(users)",
        "TRUNCATE TABLE orders",
    ],
)
def test_validate_write_operations_rejected(sql: str):
    """各类写操作 / 危险关键字必须被拒绝，且原因里带'拒绝执行'。"""
    ok, reason = sql_tools.validate_readonly_sql(sql)
    assert not ok
    assert "拒绝执行" in reason


@pytest.mark.parametrize(
    ("sql", "keyword"),
    [
        # MySQL 锁读：SELECT ... FOR UPDATE 是合法语法，但不是只读
        ("SELECT * FROM orders WHERE id = 1 FOR UPDATE", "FOR UPDATE"),
        ("SELECT * FROM payments WHERE id = 1 FOR SHARE", "FOR SHARE"),
        # MySQL 特有写文件面：SELECT 开头却能写服务器文件系统
        ("SELECT * FROM orders INTO OUTFILE '/tmp/dump.csv'", "INTO OUTFILE"),
        ("SELECT * FROM orders INTO DUMPFILE '/tmp/dump.bin'", "INTO DUMPFILE"),
        ("SELECT * FROM orders LOCK IN SHARE MODE", "LOCK IN SHARE MODE"),
    ],
)
def test_validate_mysql_specific_keywords_rejected(sql: str, keyword: str):
    """W2 新增的 MySQL 特有黑名单词（锁读/写文件/共享锁）必须被拒绝。"""
    ok, reason = sql_tools.validate_readonly_sql(sql)
    assert not ok
    assert "拒绝执行" in reason
    assert keyword in reason  # 拒绝原因要点名命中的关键字，模型才知道改哪里


def test_validate_for_update_inside_subquery_rejected():
    """含 FOR UPDATE 的子查询也要拒：黑名单是对全文的整词匹配，不看层级。"""
    sql = """
    SELECT * FROM orders
    WHERE user_id IN (SELECT user_id FROM payments WHERE id = 1 FOR UPDATE)
    """
    ok, reason = sql_tools.validate_readonly_sql(sql)
    assert not ok
    assert "FOR UPDATE" in reason


def test_validate_multi_statement_rejected():
    """一条请求里塞两条语句（SELECT + DROP）必须整体拒绝。"""
    sql = "SELECT 1; DROP TABLE users"
    ok, reason = sql_tools.validate_readonly_sql(sql)
    assert not ok
    assert "拒绝执行" in reason


# 下面这组是安全核心行为的回归测试（来源：W1 审查发现的测试缺口）。
# 这些行为一旦被改坏（比如注释不再剥除），现有的测试抓不住，会造成安全回退。


def test_validate_trailing_semicolon_passes():
    """结尾一个分号是合法写法（MySQL CLI 习惯），应放行。"""
    ok, _reason = sql_tools.validate_readonly_sql("SELECT 1;")
    assert ok


def test_validate_block_comment_with_keyword_passes():
    """块注释里藏关键字（/*DROP*/）应被剥掉，不影响查询通过。"""
    ok, _reason = sql_tools.validate_readonly_sql("/*DROP*/ SELECT 1")
    assert ok


def test_validate_line_comment_with_keyword_passes():
    """行注释里藏写操作（-- DROP TABLE）应被剥掉，不影响查询通过。"""
    ok, _reason = sql_tools.validate_readonly_sql("SELECT 1 -- DROP TABLE users\n")
    assert ok


def test_validate_cte_with_delete_rejected():
    """首关键字是 WITH（能通过首关键字检查），但语句里含 DELETE，必须被黑名单拒绝。

    这是专门防止"用 WITH 绕过首关键字检查"的攻击用例。
    """
    sql = "WITH x AS (SELECT 1) DELETE FROM users"
    ok, reason = sql_tools.validate_readonly_sql(sql)
    assert not ok
    assert "拒绝执行" in reason


def test_validate_keyword_in_string_literal_rejected():
    """已知保守行为（W1 故意）：字符串字面量里的关键字也会被误拒。

    WHERE status = 'update' 里的 update 只是普通数据，不会真的写库，
    但黑名单是纯文本匹配，不解析字符串，所以宁可错杀。
    W5 换 sqlglot AST 解析后会放开这个误杀。
    """
    sql = "SELECT * FROM orders WHERE status = 'update'"
    ok, _reason = sql_tools.validate_readonly_sql(sql)
    assert not ok


def test_validate_underscore_column_not_mistaken_for_update():
    """created_at / updated_at 这类下划线词不应被 \\bUPDATE\\b 误伤。"""
    sql = "SELECT * FROM orders WHERE updated_at = '2026-01-01'"
    ok, _reason = sql_tools.validate_readonly_sql(sql)
    assert ok


# ---------------------------------------------------------------------------
# enforce_limit：自动补 LIMIT（W2 零改动，用例原样保留）
# ---------------------------------------------------------------------------


def test_enforce_limit_appends_when_missing():
    """没有 LIMIT 的查询应被追加 LIMIT 20。"""
    result = sql_tools.enforce_limit("SELECT * FROM orders")
    assert result == "SELECT * FROM orders LIMIT 20"


def test_enforce_limit_keeps_existing():
    """已经有 LIMIT 5 的查询应原样返回，不再追加。"""
    sql = "SELECT * FROM orders LIMIT 5"
    assert sql_tools.enforce_limit(sql) == sql


def test_enforce_limit_strips_trailing_semicolon():
    """结尾分号应被摘掉，避免拼出 '; LIMIT 20' 的语法错误。"""
    assert sql_tools.enforce_limit("SELECT * FROM orders;") == "SELECT * FROM orders LIMIT 20"


def test_enforce_limit_ignores_limit_inside_comment():
    """注释里的 LIMIT（/* LIMIT 5 */）不应被误认为已有 LIMIT，仍要追加。"""
    result = sql_tools.enforce_limit("SELECT * FROM orders /* LIMIT 5 */")
    assert result.endswith("LIMIT 20")
    assert "LIMIT 20" in result.split("/*")[0]  # 追加的 LIMIT 在注释之外


# ---------------------------------------------------------------------------
# MySQL 执行类测试（门控：需要本机 MySQL 的 mart 库）
# W1 的 SQLite demo_db 版本已按设计 §4.7 迁移到这里，行数断言 30 → 100000。
# ---------------------------------------------------------------------------


@requires_mysql
def test_execute_count_orders_mysql():
    """COUNT 查询应返回列名和订单总数 100000（W1 断言 30 的 MySQL 版）。"""
    result = sql_tools.execute_readonly_sql.invoke({"sql": "SELECT COUNT(*) FROM orders"})
    assert "COUNT(*)" in result  # 列名
    assert "100000" in result  # 行数
    assert "共 1 行" in result


@requires_mysql
def test_execute_bad_column_returns_mysql_readable_error():
    """列名拼错应返回可读中文（1054 映射提示含'列'）+ MySQL 原始 message。"""
    result = sql_tools.execute_readonly_sql.invoke({"sql": "SELECT nonexistent_col FROM orders"})
    assert "SQL 错误" in result
    assert "列不存在" in result  # 1054 的中文映射
    assert "Unknown column" in result  # MySQL 原始提示，模型靠它自我修正


@requires_mysql
def test_execute_delete_is_rejected_and_data_unchanged():
    """DELETE 被代码校验拒绝后，库中数据必须原封不动。"""
    result = sql_tools.execute_readonly_sql.invoke({"sql": "DELETE FROM orders"})
    assert "拒绝执行" in result

    # 双重确认：真的去库里数一下，订单还是 100000 条
    count_result = sql_tools.execute_readonly_sql.invoke({"sql": "SELECT COUNT(*) FROM orders"})
    assert "100000" in count_result


@requires_mysql
def test_list_tables_returns_six_tables():
    """list_tables 应返回 mart 库的 6 张表（information_schema + 逐表 COUNT）。"""
    result = sql_tools.list_tables.invoke({})
    assert "共有 6 张表" in result
    for table in ("users", "products", "orders", "order_items", "payments", "refunds"):
        assert table in result


@requires_mysql
def test_session_read_only_guard_is_active():
    """会话只读第二道防线生效：Agent 引擎的每个连接 transaction_read_only = 1。"""
    result = sql_tools.execute_readonly_sql.invoke({"sql": "SELECT @@transaction_read_only"})
    lines = result.splitlines()
    # 输出结构固定：第 1 行行数、第 2 行列名、第 3 行值
    assert len(lines) >= 3
    assert lines[0] == "共 1 行"
    assert lines[2].strip() == "1"


@requires_mysql
def test_for_update_rejected_before_touching_database():
    """SELECT ... FOR UPDATE 在代码校验阶段就被拒（不会真到 MySQL 锁表）。"""
    result = sql_tools.execute_readonly_sql.invoke(
        {"sql": "SELECT * FROM orders WHERE id = 1 FOR UPDATE"}
    )
    assert "拒绝执行" in result
    assert "FOR UPDATE" in result
