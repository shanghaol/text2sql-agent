"""SQL 工具的单元测试。

设计原则：**完全不依赖 DEEPSEEK_API_KEY**——这里测的都是本地逻辑
（SQL 校验、LIMIT 注入、只读执行、建库脚本），不需要真的调用模型。

fixture 里的临时数据库通过 set_db_path 注入，
teardown 时恢复原路径，避免影响其他测试。
"""

import sqlite3
from pathlib import Path

import pytest

from agent.db.seed import seed_demo_db
from agent.tools import sql_tools


@pytest.fixture()
def demo_db(tmp_path: Path):
    """在 pytest 临时目录里建一个演示库，并把工具的 DB_PATH 指向它。

    - tmp_path 是 pytest 自带的临时目录 fixture，每个测试独享一份，
      测试结束后由 pytest 自动清理；
    - teardown（yield 之后的代码）负责恢复原来的路径，防止"测试污染全局状态"。
    """
    db_file = tmp_path / "test_demo.db"
    seed_demo_db(db_file)
    original_path = sql_tools.DB_PATH
    sql_tools.set_db_path(db_file)
    yield db_file
    sql_tools.set_db_path(original_path)


# ---------------------------------------------------------------------------
# validate_readonly_sql：只读校验
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


def test_validate_multi_statement_rejected():
    """一条请求里塞两条语句（SELECT + DROP）必须整体拒绝。"""
    sql = "SELECT 1; DROP TABLE users"
    ok, reason = sql_tools.validate_readonly_sql(sql)
    assert not ok
    assert "拒绝执行" in reason


# 下面这组是安全核心行为的回归测试（来源：W1 审查发现的测试缺口）。
# 这些行为一旦被改坏（比如注释不再剥除），现有的测试抓不住，会造成安全回退。


def test_validate_trailing_semicolon_passes():
    """结尾一个分号是合法写法（SQLite 习惯），应放行。"""
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
    但 W1 的黑名单是纯文本匹配，不解析字符串，所以宁可错杀。
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
# enforce_limit：自动补 LIMIT
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
# execute_readonly_sql：用 .invoke 调用真正的工具
# ---------------------------------------------------------------------------


def test_execute_count_orders(demo_db: Path):
    """COUNT 查询应返回列名和正确的行数 30。"""
    result = sql_tools.execute_readonly_sql.invoke({"sql": "SELECT COUNT(*) FROM orders"})
    assert "COUNT(*)" in result  # 列名
    assert "30" in result  # 行数
    assert "共 1 行" in result


def test_execute_truncates_large_result(demo_db: Path):
    """SELECT * 有 30 行结果，应只显示 20 行并附截断提示。"""
    result = sql_tools.execute_readonly_sql.invoke({"sql": "SELECT * FROM orders"})
    assert "共 20 行" in result
    assert "结果已截断" in result


def test_execute_bad_column_returns_readable_error(demo_db: Path):
    """列名拼错应返回包含 'SQL 错误' 的可读中文信息（带 SQLite 原始 message）。"""
    result = sql_tools.execute_readonly_sql.invoke({"sql": "SELECT nonexistent_col FROM orders"})
    assert "SQL 错误" in result
    assert "no such column" in result  # SQLite 的原始提示，模型靠它自我修正


def test_execute_delete_is_rejected_and_data_unchanged(demo_db: Path):
    """DELETE 被拒绝后，库中数据必须原封不动。"""
    result = sql_tools.execute_readonly_sql.invoke({"sql": "DELETE FROM orders"})
    assert "拒绝执行" in result

    # 双重确认：真的去库里数一下，订单还是 30 条
    conn = sqlite3.connect(demo_db)
    try:
        count = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
    finally:
        conn.close()
    assert count == 30


# ---------------------------------------------------------------------------
# seed_demo_db：建库脚本本身
# ---------------------------------------------------------------------------


def test_seed_creates_tables_with_expected_row_counts(tmp_path: Path):
    """建库后三张表都存在，行数分别是 10 / 8 / 30。"""
    db_file = tmp_path / "seed_check.db"
    result_path = seed_demo_db(db_file)

    assert result_path == db_file  # 返回值就是库路径
    assert db_file.exists()

    conn = sqlite3.connect(db_file)
    try:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        }
        assert tables == {"users", "products", "orders"}
        assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 10
        assert conn.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 8
        assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 30
    finally:
        conn.close()
