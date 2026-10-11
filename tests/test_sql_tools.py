"""SQL 工具的单元测试。

设计原则：**默认套件完全不依赖 MySQL 和 DEEPSEEK_API_KEY**——
validate_readonly_sql / enforce_limit 都是纯函数（W5 起实现迁至
agent/tools/sql_guard.py 的 sqlglot AST 版），任何机器裸跑全绿；
执行类测试依赖真实 MySQL（mart 库），用环境变量门控 + skipif 跳过：
门控条件 = 测试文件顶部自己 load_dotenv() 后检查 MYSQL_ADMIN_PASSWORD
已配置且 MySQL 端口可连通（见下方 _mysql_reachable）。

W5 用例结构（设计 §1.4）：翻转（W1 误杀→放行）/ 不变（安全核心回归）/
收紧（子查询 LIMIT 修复）/ 新增（AST 特有拒绝 + legacy 逃生门回归锁）。
"""

import importlib
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
    """各类写操作 / 危险关键字必须被拒绝，且原因里带'拒绝执行'。

    W5 AST 口径：PRAGMA 在 mysql 方言下解析为 exp.Pragma 根（不在白名单）、
    其余为 Insert/Update/Delete/Drop/TruncateTable 根——R2 一条规则兜住全部。
    """
    ok, reason = sql_tools.validate_readonly_sql(sql)
    assert not ok
    assert "拒绝执行" in reason


def test_validate_call_command_rejected():
    """CALL 等未知语句回退为 exp.Command，被 R2 白名单一票否决（设计 §1.3）。

    Command 是 sqlglot 的"不认识"兜底：宁可拒绝（只读账号也会拒它）
    也不能放行未知形态；文案点名源码首关键字（Command.name='CALL'）。
    """
    ok, reason = sql_tools.validate_readonly_sql("CALL sp()")
    assert not ok
    assert "拒绝执行" in reason
    assert "CALL" in reason


@pytest.mark.parametrize(
    "sql",
    [
        # MySQL 锁读：SELECT ... FOR UPDATE / FOR SHARE 是合法语法，但不是只读
        "SELECT * FROM orders WHERE id = 1 FOR UPDATE",
        "SELECT * FROM payments WHERE id = 1 FOR SHARE",
        # MySQL 特有写文件面：SELECT 开头却能写服务器文件系统。
        # W5 实测：OUTFILE/DUMPFILE 在 mysql 方言下 sqlglot 直接解析报错 → ParseError 文案
        "SELECT * FROM orders INTO OUTFILE '/tmp/dump.csv'",
        "SELECT * FROM orders INTO DUMPFILE '/tmp/dump.bin'",
        "SELECT * FROM orders LOCK IN SHARE MODE",
    ],
)
def test_validate_mysql_specific_syntax_rejected(sql: str):
    """W2 引入的 MySQL 特有攻击面（锁读/写文件/共享锁）在 AST 口径下必须被拒绝。

    断言放宽说明（设计 §1.4）：拒绝理由按规则组织——R3 锁读一条文案覆盖
    FOR UPDATE / FOR SHARE / LOCK IN SHARE MODE；OUTFILE/DUMPFILE 走 ParseError
    文案——不再逐条点名关键字，只断言"拒绝执行"前缀（与写操作清单同口径）。
    """
    ok, reason = sql_tools.validate_readonly_sql(sql)
    assert not ok
    assert "拒绝执行" in reason


def test_validate_for_update_inside_subquery_rejected():
    """含 FOR UPDATE 的子查询也要拒：R3 是树扫描，不看层级。"""
    sql = """
    SELECT * FROM orders
    WHERE user_id IN (SELECT user_id FROM payments WHERE id = 1 FOR UPDATE)
    """
    ok, reason = sql_tools.validate_readonly_sql(sql)
    assert not ok
    assert "FOR UPDATE" in reason  # R3 文案点名锁读语法，模型才知道去掉哪个子句


def test_validate_select_into_variable_rejected():
    """SELECT ... INTO @c 可向服务器写变量，R4 必须拒绝（设计 §1.3）。"""
    ok, reason = sql_tools.validate_readonly_sql("SELECT COUNT(*) INTO @c FROM orders")
    assert not ok
    assert "拒绝执行" in reason
    assert "INTO" in reason  # R4 有专门的 INTO 文案（OUTFILE/DUMPFILE 才走 ParseError）


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT SLEEP(10)",
        "SELECT BENCHMARK(1000000, MD5('x'))",
        "SELECT LOAD_FILE('/etc/passwd')",
        "SELECT GET_LOCK('demo', 10)",
        "SELECT RELEASE_LOCK('demo')",
    ],
)
def test_validate_forbidden_functions_rejected(sql: str):
    """R5 禁用函数黑名单：延时 DoS / 文件读取 / 命名锁（设计 §1.7）。"""
    ok, reason = sql_tools.validate_readonly_sql(sql)
    assert not ok
    assert "拒绝执行" in reason


def test_validate_forbidden_function_reason_names_function():
    """禁用函数文案要点名函数名，模型才知道去掉哪个。"""
    ok, reason = sql_tools.validate_readonly_sql("SELECT sleep(10)")
    assert not ok
    assert "SLEEP" in reason  # exp.Anonymous 的 name 保留源码大小写，比对前统一 upper


def test_validate_group_concat_passes():
    """GROUP_CONCAT 放行（设计 §1.7 决策的回归锁）。

    它是"拼串"类问题的合理解法（如"列出每个城市的商品名"），禁用属于功能阉割；
    大分组下的慢由 W5 执行超时兜底，不在语法层禁用。
    """
    sql = (
        "SELECT category, GROUP_CONCAT(name ORDER BY name SEPARATOR ', ') AS names "
        "FROM products GROUP BY category"
    )
    ok, reason = sql_tools.validate_readonly_sql(sql)
    assert ok, reason


def test_validate_parse_error_reason_is_readable_chinese():
    """解析失败的拒绝文案 = 中文引导 + 原始报错片段（设计 §1.5）。"""
    ok, reason = sql_tools.validate_readonly_sql("SELECT FROM WHERE ORDER")
    assert not ok
    assert "拒绝执行" in reason
    assert "无法解析" in reason      # 中文定性
    assert "DATE_FORMAT" in reason   # 方言引导（模型靠它换掉 SQLite 习惯写法）
    assert "解析器报错" in reason and ("Line 1" in reason or "Col" in reason)  # 原文片段供定位


def test_validate_multi_statement_rejected():
    """一条请求里塞两条语句（SELECT + DROP）必须整体拒绝。"""
    sql = "SELECT 1; DROP TABLE users"
    ok, reason = sql_tools.validate_readonly_sql(sql)
    assert not ok
    assert "拒绝执行" in reason


def test_validate_benign_multi_statement_rejected():
    """多条良性 SELECT 也必须拒：一次只能执行一条（设计 §1.8 对抗清单）。

    parse() 把 "SELECT 1; SELECT 2" 逐条切分（列表 len=2）→ R1 拒绝。
    sqlglot 的 parse_one 才会把多语句包成 exp.Block；validate 走 parse()，
    多语句恒为 R1 路径——无论语句本身是否危险。
    """
    ok, reason = sql_tools.validate_readonly_sql("SELECT 1; SELECT 2")
    assert not ok
    assert "一次只能执行一条" in reason


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


@pytest.mark.parametrize(
    "sql",
    [
        # 字面量误杀三连（设计 §1.8 对抗清单）：W1 黑名单逐条误杀的形态
        "SELECT * FROM orders WHERE status = 'update'",
        "SELECT * FROM orders WHERE name LIKE '%delete%'",
        "SELECT * FROM orders WHERE note = 'please update this'",
    ],
)
def test_validate_keyword_in_string_literal_passes(sql: str):
    """[W5 翻转项] 字符串字面量里的关键字不再误杀（设计 §1.4）。

    W1 背景：黑名单是纯文本整词匹配，'update' 只是普通数据却被
    "宁可错杀，不可放过"。W5 的 sqlglot AST 只看结构——字面量解析为
    exp.Literal 节点，树中不存在 exp.Update 节点，误杀随之消除（§1.1 实测）。
    """
    ok, reason = sql_tools.validate_readonly_sql(sql)
    assert ok, reason


def test_validate_underscore_column_not_mistaken_for_update():
    """created_at / updated_at 这类下划线词不应被 \\bUPDATE\\b 误伤。"""
    sql = "SELECT * FROM orders WHERE updated_at = '2026-01-01'"
    ok, _reason = sql_tools.validate_readonly_sql(sql)
    assert ok


# ---------------------------------------------------------------------------
# enforce_limit：自动补 LIMIT（W5 AST 版：行为修复见各用例注释）
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


def test_enforce_limit_appends_outer_for_subquery_limit():
    """[W5 收紧修复项] 子查询里的 LIMIT 不再骗过检查，外层被正确追加（设计 §1.6）。

    W1 行为：正则看到子查询的 LIMIT 5 就认为"已有 LIMIT"，外层漏追加。
    AST 口径：root.args.get("limit") 只反映最外层——外层缺失必追加。
    """
    sql = "SELECT * FROM (SELECT * FROM orders LIMIT 5) t"
    assert sql_tools.enforce_limit(sql) == f"{sql} LIMIT 20"


def test_enforce_limit_union_root_appends():
    """UNION 根的 LIMIT 判定与语义一致：MySQL 里 LIMIT 作用于整个联合结果。"""
    sql = "SELECT city FROM users UNION SELECT province FROM users"
    assert sql_tools.enforce_limit(sql) == f"{sql} LIMIT 20"


def test_enforce_limit_trailing_line_comment_not_swallowed():
    """行注释收尾的极端场景：追加的 LIMIT 不能被注释吞掉（设计 §1.6）。

    "SELECT * FROM orders -- 全表查询" 直接拼接会得到
    "... -- 全表查询 LIMIT 20"，执行时 LIMIT 落在注释里等于没加——
    此时以清注释副本为拼接目标；LIMIT 的"是否已有"判定永远来自 AST。
    """
    result = sql_tools.enforce_limit("SELECT * FROM orders -- 全表查询")
    assert result == "SELECT * FROM orders LIMIT 20"


def test_enforce_limit_dash_dash_inside_literal_not_corrupted():
    """字面量里的 '--' 不能被行注释收尾启发式误伤：原文追加，LIMIT 落在字面量外。"""
    sql = "SELECT * FROM orders WHERE note = 'a--b'"
    result = sql_tools.enforce_limit(sql)
    assert result == f"{sql} LIMIT 20"  # 原文完整保留，未把 'a--b' 当注释剥离


# ---------------------------------------------------------------------------
# _humanize_db_error：超时错误可读化（W5 设计 §2.3，纯函数零 MySQL 依赖）
# ---------------------------------------------------------------------------


def _make_db_error(errno: int, message: str) -> Exception:
    """构造与 SQLAlchemy 真实链路同形的异常：pymysql 错误包装在 exc.orig。"""
    from pymysql.err import OperationalError as PyMySQLOperationalError
    from sqlalchemy.exc import OperationalError as SAOperationalError

    return SAOperationalError("SELECT ...", {}, PyMySQLOperationalError(errno, message))


def test_humanize_db_error_errno_3024_server_timeout():
    """errno 3024（max_execution_time 命中）映射为服务端超时文案（设计 §2.3 原文）。"""
    exc = _make_db_error(
        3024, "Query execution was interrupted, maximum statement execution time exceeded"
    )
    msg = sql_tools._humanize_db_error(exc)
    assert "SQL 执行超时" in msg
    assert "超过 10 秒被数据库终止" in msg  # 默认 MYSQL_MAX_EXECUTION_MS=10000
    assert "缩小时间范围" in msg


def test_humanize_db_error_driver_read_timeout():
    """驱动层读超时映射：errno 2013 + message 含 timed out（§8-⑩ 实测形态）。

    文案不承诺服务端已终止——终止语义留给服务端 max_execution_time 防线。
    """
    exc = _make_db_error(2013, "Lost connection to MySQL server during query (timed out)")
    msg = sql_tools._humanize_db_error(exc)
    assert "查询等待超时" in msg
    assert "缩小时间范围" in msg


def test_humanize_db_error_lost_connection_without_timeout_stays_generic():
    """errno 2013 但 message 不含 timed out（真断连）不走超时文案，保持通用映射。"""
    exc = _make_db_error(2013, "Lost connection to MySQL server during query")
    msg = sql_tools._humanize_db_error(exc)
    assert "查询等待超时" not in msg
    assert "SQL 错误" in msg


# ---------------------------------------------------------------------------
# SQLGUARD_MODE=legacy 逃生门回归锁（W5 设计 §1.2/§1.4）
# ---------------------------------------------------------------------------


class TestLegacyEscapeHatch:
    """TEXT2SQL_SQLGUARD=legacy 时，sql_tools 应回到 W1 正则实现的行为。

    逃生门的语义是"线上出现设计未预料的误杀时切回旧行为恢复服务"——
    因此这里锁定的恰是 W1 的已知误杀/漏判（它们是 W5 的修复项）：
    逃生门必须真的能还原 W1，而不是名存实亡。W6 删除 legacy 时本类一并删除。
    """

    @pytest.fixture(autouse=True)
    def _use_legacy_mode(self):
        """逐用例切换：设环境变量 → 重载 config → 重载 sql_tools（二选一导入在 import 时决定）。"""
        import agent.config as config_module

        os.environ["TEXT2SQL_SQLGUARD"] = "legacy"
        importlib.reload(config_module)
        importlib.reload(sql_tools)
        yield
        os.environ.pop("TEXT2SQL_SQLGUARD", None)
        importlib.reload(config_module)
        importlib.reload(sql_tools)

    def test_legacy_string_literal_keyword_still_rejected(self):
        """W1 已知误杀在 legacy 模式下原样保留（旧行为锚点）。"""
        ok, _ = sql_tools.validate_readonly_sql("SELECT * FROM orders WHERE status = 'update'")
        assert not ok

    def test_legacy_reject_reason_comes_from_regex_implementation(self):
        """拒绝文案是 legacy 正则实现的专属措辞，证明导入的确实是旧实现。

        legacy 的检查顺序是首关键字→黑名单：DELETE 开头的语句命中
        "必须以 SELECT 或 WITH 开头"（首关键字检查），走不到黑名单——
        这个措辞 AST 版没有（AST 报"只允许 SELECT 查询语句…不允许 DELETE"）。
        """
        ok, reason = sql_tools.validate_readonly_sql("DELETE FROM orders")
        assert not ok
        assert "SELECT 或 WITH" in reason

    def test_legacy_subquery_limit_still_not_appended(self):
        """W1 已知漏判在 legacy 模式下原样保留：子查询 LIMIT 令外层不追加。"""
        sql = "SELECT * FROM (SELECT * FROM orders LIMIT 5) t"
        assert sql_tools.enforce_limit(sql) == sql

    def test_legacy_write_operation_still_rejected(self):
        """legacy 模式的安全底线不变：写操作仍被拒绝。"""
        ok, reason = sql_tools.validate_readonly_sql("DROP TABLE users")
        assert not ok
        assert "拒绝执行" in reason


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
def test_session_max_execution_time_guard_is_active():
    """执行超时防线生效（W5 §2）：Agent 引擎的每个连接 max_execution_time 已设置。"""
    result = sql_tools.execute_readonly_sql.invoke({"sql": "SELECT @@max_execution_time"})
    lines = result.splitlines()
    assert len(lines) >= 3
    assert lines[0] == "共 1 行"
    assert lines[2].strip() == str(int(os.environ.get("MYSQL_MAX_EXECUTION_MS", "10000")))


@requires_mysql
def test_server_side_timeout_kills_runaway_query():
    """主防线端到端（W5 §2.1）：跑飞的长查询在 10 秒被服务端终止并返回可读文案。

    用"无连接条件 + 逐行哈希"的查询制造必然超过 max_execution_time 的负载——
    COUNT(*) 积和等值 JOIN 都会被 MySQL 8 优化器/哈希 JOIN 秒算（开发机实测），
    逐行 MD5(CONCAT(a,b)) 无法走任何捷径，2.25e10 行对必然超时。
    SLEEP/BENCHMARK 被 R5 禁用函数拦下到不了执行层，故必须用"合法但慢"的查询
    验证服务端超时路径（errno 3024 → 中文文案）。
    """
    result = sql_tools.execute_readonly_sql.invoke(
        {"sql": "SELECT SUM(LENGTH(MD5(CONCAT(a.id, b.id)))) FROM order_items a, order_items b"}
    )
    assert "SQL 执行超时" in result
    assert "缩小时间范围" in result


@requires_mysql
def test_for_update_rejected_before_touching_database():
    """SELECT ... FOR UPDATE 在代码校验阶段就被拒（不会真到 MySQL 锁表）。"""
    result = sql_tools.execute_readonly_sql.invoke(
        {"sql": "SELECT * FROM orders WHERE id = 1 FOR UPDATE"}
    )
    assert "拒绝执行" in result
    assert "FOR UPDATE" in result
