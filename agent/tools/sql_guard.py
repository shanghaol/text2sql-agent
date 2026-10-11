"""SQL 安全守卫：sqlglot AST 白名单校验 + LIMIT 补齐（W5，设计 §1）。

这是 W5 把"第三道防线"从文本匹配升级为语义理解的新家（W1 的正则实现
整体迁入本文件尾部作逃生门，见 `_legacy_*`）。两条公共函数与旧签名完全一致：

- validate_readonly_sql(sql) -> (bool, reason)：R1-R5 五条拒绝规则，命中即返回；
- enforce_limit(sql, default_limit=20) -> str：R6 判定 + 外层 LIMIT 追加。

核心原则（设计 §1.3 固定）：**校验用 AST、执行用原文**。
sqlglot 只负责"看懂"这条 SQL 是什么、有没有危险结构；真正执行的文本是
用户原文（仅摘尾分号）——不重新生成 SQL，避免丢失注释、规整字面量格式
给"模型写的"与"执行的"对照（F2 自我修正与审计）制造文本差异。

规则集（每条的拒绝文案是 F2 硬性需求：中文可读 + 引导模型自我修正）：
- R1 唯一语句：parse() 返回语句列表，多语句 len>1、空/纯注释首元素为 None；
- R2 根节点白名单：Select/Union/Except/Intersect 之外一律拒绝——一条规则
  兜住全部写操作（Insert/Update/Delete/Drop/Create/Alter/Set/TruncateTable
  不匹配白名单；CALL/GRANT/REPLACE 等未知语句回退为 exp.Command 也不匹配）；
- R3 锁读拦截：树扫描 exp.Lock（FOR UPDATE / FOR SHARE / LOCK IN SHARE MODE，
  根仍是 Select，必须进树里找）；
- R4 INTO 拦截：树扫描 exp.Into（SELECT ... INTO @var 可写服务器变量；
  INTO OUTFILE/DUMPFILE 在 MySQL 方言下 sqlglot 直接解析报错，天然拒绝）；
- R5 禁用函数：小黑名单（LOAD_FILE/SLEEP/BENCHMARK/GET_LOCK/RELEASE_LOCK），
  都是"数据分析零用处 + 明确攻击面"；GROUP_CONCAT 是合法分析函数，放行；
- R6 LIMIT 判定：根节点的 limit 参数（只看最外层——子查询的 LIMIT 不算数，
  Union 根同样有 limit 参数），供 enforce_limit / _prepare 使用。

本模块是纯函数、零 MySQL 依赖（延续"纯函数可单测"惯例）；
sqlglot 的"不认识语法"降级提示在 import 时即被降噪（见下方 logger 设置）。
"""

from __future__ import annotations

import logging
import re

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError, TokenError

# sqlglot 解析不认识的语句时会向自己的 logger 发
# "contains unsupported syntax. Falling back to parsing as a 'Command'." 提示。
# 这是我们的预期路径（Command 会被 R2 白名单一票否决），不该以 WARNING 刷屏。
# W5 设计 §8-⑯ 实测定案：该提示的 logger 名就是 "sqlglot"（warnings 模块
# 捕获不到，走 logging），降到 ERROR 即静音、真正的问题仍会浮出。
logging.getLogger("sqlglot").setLevel(logging.ERROR)

# ---------------------------------------------------------------------------
# R1-R5 规则集的常量与拒绝文案（文案为设计 §1.3 表格原文，F2 自我修正依赖）
# ---------------------------------------------------------------------------

# R2 根节点白名单：只放行四种"纯查询"根。刻意不含 exp.Command——
# Command 是 sqlglot 的"不认识"兜底，语义上等同于未知语句，
# 宁可拒绝（只读账号也会拒它）也不能放行未知形态（设计 §1.3 取舍说明）。
_ALLOWED_ROOTS = (exp.Select, exp.Union, exp.Except, exp.Intersect)

# R5 禁用函数黑名单（设计 §1.7 决策：小黑名单而非全函数白名单）。
# 比对用 name.upper()：exp.Anonymous 的 .name 保留源码大小写（sleep(10) → "sleep"）。
_FORBIDDEN_FUNCTIONS = {"LOAD_FILE", "SLEEP", "BENCHMARK", "GET_LOCK", "RELEASE_LOCK"}

_EMPTY_REASON = "拒绝执行：SQL 为空或只包含注释。请提供一条 SELECT 查询语句。"
_MULTI_STATEMENT_REASON = (
    "拒绝执行：一次只能执行一条 SQL 语句（检测到语句中间的分号）。"
    "请去掉多余的语句，一次只执行一条查询。"
)
_ROOT_NOT_ALLOWED_REASON = (
    "拒绝执行：只允许 SELECT 查询语句（含 CTE/UNION），不允许 {label}。"
    "本工具对数据库只有只读权限，请改写为只读查询后重试。"
)
_LOCK_REASON = (
    "拒绝执行：检测到锁读语法（FOR UPDATE / LOCK IN SHARE MODE）。"
    "本工具只做数据分析查询，不需要也不允许加锁，请去掉锁子句后重试。"
)
_INTO_REASON = (
    "拒绝执行：检测到 INTO 子句（SELECT ... INTO 可向服务器写文件或导出变量）。"
    "请去掉 INTO 子句后重试。"
)
_FORBIDDEN_FUNC_REASON = (
    "拒绝执行：检测到禁用函数 {name}。"
    "数据分析场景不需要文件读取（LOAD_FILE）或延时/锁函数（SLEEP/BENCHMARK/GET_LOCK），"
    "请去掉后重试。"
)
_PARSE_ERROR_REASON = (
    "拒绝执行：SQL 无法解析（语法错误）。"
    "请对照 MySQL 方言检查拼写与子句顺序"
    "（日期函数用 DATE_FORMAT/DATE_SUB，不能用 SQLite 的 strftime）。"
    "解析器报错：{raw}"
)

# R2 拒绝文案的语句类型人话映射：类名 → 用户能看懂的关键词。
# 不在表里的类型回退为节点类名（设计 §1.3：未知类型显示节点名）。
_STATEMENT_LABELS: dict[type[exp.Expression], str] = {
    exp.Insert: "INSERT",
    exp.Update: "UPDATE",
    exp.Delete: "DELETE",
    exp.Drop: "DROP",
    exp.Create: "CREATE",
    exp.Alter: "ALTER",
    exp.Set: "SET",
    exp.TruncateTable: "TRUNCATE",
    exp.Block: "多条语句",
}


def _statement_type_label(root: exp.Expression) -> str:
    """把拒绝文案里的 <语句类型> 映射成人话（设计 §1.3 R2）。

    exp.Command 特殊处理：它是 sqlglot 对 CALL/GRANT/REPLACE 等"不认识"语句的
    兜底节点，name 属性保留源码首关键字（实测 "CALL sp()" → name='CALL'），
    报 "CALL" 比报类名 "Command" 对模型更可读；取不到时回退"未知语句"。
    """
    if isinstance(root, exp.Command):
        head = (root.name or "").strip().upper()
        return head or "未知语句"
    for cls, label in _STATEMENT_LABELS.items():
        if isinstance(root, cls):
            return label
    return type(root).__name__


def _parse_error_reason(exc: Exception) -> str:
    """ParseError/TokenError 的中文包装（设计 §1.5）。

    原文保留 300 字符：模型靠报错原文里的位置信息（Line 1, Col: N）定位改错，
    与 _humanize_db_error"映射 + 原文"的既有惯例一致。
    注：未闭合的字符串字面量（如 WHERE note = 'a）在 sqlglot 30.x 抛的是
    TokenError 而非 ParseError（实测，两者无继承关系）——语义同样是
    "解析不了"，与 ParseError 走同一条中文包装。
    """
    return _PARSE_ERROR_REASON.format(raw=str(exc).strip()[:300])


# ---------------------------------------------------------------------------
# 公共函数（签名与 W1 完全一致，内部实现为 AST 版）
# ---------------------------------------------------------------------------


def validate_readonly_sql(sql: str) -> tuple[bool, str]:
    """校验一段 SQL 是否是安全的只读查询（W5 AST 版，规则 R1-R5）。

    检查顺序：R1 唯一语句 → R2 根节点白名单 → R3 锁读 → R4 INTO → R5 禁用函数，
    命中即返回；解析失败（ParseError/TokenError）也属于拒绝路径——
    sqlglot 解析不了的 SQL，MySQL 也执行不了，不存在"解析失败但合法"的查询。

    返回:
        (是否通过, 拒绝原因)。通过时第二个元素是空字符串；
        拒绝原因写成中文完整句子，让模型能看懂并自己改 SQL 重试。
    """
    try:
        stmts = sqlglot.parse(sql, dialect="mysql")
    except (ParseError, TokenError) as exc:
        return False, _parse_error_reason(exc)

    # R1 唯一语句：parse() 返回语句列表——
    #   多语句 → len > 1（sqlglot 逐条切分，多语句不会被合并成 exp.Block）；
    #   "" / "   " / ";" / "-- 注释" → [None]（长度 1 但首元素为 None，视为空）。
    if len(stmts) != 1:
        return False, _MULTI_STATEMENT_REASON
    if stmts[0] is None:
        return False, _EMPTY_REASON

    root = stmts[0]

    # R2 根节点白名单：一条规则兜住全部写操作与未知语句（见 _ALLOWED_ROOTS 注释）
    if not isinstance(root, _ALLOWED_ROOTS):
        return False, _ROOT_NOT_ALLOWED_REASON.format(label=_statement_type_label(root))

    # R3 锁读拦截：FOR UPDATE / FOR SHARE / LOCK IN SHARE MODE 解析为 exp.Lock，
    # 根节点仍是 Select，必须树扫描才能发现（含子查询里的锁读）
    if root.find(exp.Lock) is not None:
        return False, _LOCK_REASON

    # R4 INTO 拦截：SELECT ... INTO @var 解析为 exp.Into；
    # INTO OUTFILE/DUMPFILE 在 mysql 方言下是 ParseError，走不到这里（见 _parse_error_reason）
    if root.find(exp.Into) is not None:
        return False, _INTO_REASON

    # R5 禁用函数：黑名单五函数（§1.7）。exp.Anonymous 的 .name 保留源码大小写，
    # 比对统一 .upper()；GROUP_CONCAT 是 exp.GroupConcat（合法分析函数），不在黑名单。
    for func in root.find_all(exp.Func):
        if func.name.upper() in _FORBIDDEN_FUNCTIONS:
            return False, _FORBIDDEN_FUNC_REASON.format(name=func.name.upper())

    return True, ""


def _prepare(sql: str, default_limit: int = 20) -> tuple[str, bool]:
    """摘尾分号 + 判定是否需要补 LIMIT（设计 §1.6，sql_tools 的内部接线函数）。

    返回 (safe_sql, had_explicit_limit)：
    - safe_sql：执行用原文——只摘尾分号（+首尾空白规整），不重新生成 SQL；
      极端场景（SQL 以行注释/块注释收尾）下直接拼接会把追加的 LIMIT 吞进注释，
      此时改用清注释副本作为拼接目标（had 判定永远来自 AST，与注释无关）；
    - had_explicit_limit：AST 判定的"最外层是否已有 LIMIT"（root.args.get("limit")，
      子查询的 LIMIT 不算、Union 根算），供"恰好 20 行可能被截断"提示沿用。

    调用契约：validate_readonly_sql(sql) 必须已经通过——本函数不重复校验；
    为防御直调，根节点取不到（空/多语句）时按"无 LIMIT"处理而非崩溃。
    """
    stmts = sqlglot.parse(sql, dialect="mysql")
    root = stmts[0] if len(stmts) == 1 else None
    had_limit = root is not None and root.args.get("limit") is not None

    # 执行用原文：摘尾分号 + 首尾空白（与旧实现输出风格一致）
    base = sql.strip()
    if base.endswith(";"):
        base = base[:-1].rstrip()

    if had_limit:
        return base, True

    # 行注释（"-- ..." 无换行收尾）或块注释收尾时，直接在原文上拼接会把
    # LIMIT 追加进注释里（如 "SELECT 1 -- 注释 LIMIT 20" 执行时注释吞掉 LIMIT）。
    # 用 _strip_comments 清注释副本作为拼接目标；清完后做一次可解析性自检——
    # 若清注释把字符串字面量切坏了（如 WHERE note = 'a--b' 被误当注释剥离，
    # 留下未闭合引号），保留原文拼接：此时 LIMIT 落在字面量之外，语义仍正确。
    if re.search(r"--[^\n]*$", base) or base.endswith("*/"):
        try:
            cleaned = _strip_comments(base).rstrip()
            sqlglot.parse(cleaned, dialect="mysql")
            base = cleaned
        except (ParseError, TokenError):
            pass  # 自检失败：清注释副本不可用，回退原文
    return f"{base} LIMIT {default_limit}", False


def enforce_limit(sql: str, default_limit: int = 20) -> str:
    """给最外层没有 LIMIT 的查询追加 ` LIMIT 20`，防止一次捞回全表。

    W5 AST 版：通过 _prepare 返回 (safe_sql, had_explicit_limit)，
    本函数保留为单返回值兼容封装（签名与 W1 一致，评测/旧调用不受影响）。

    行为变化即修复项（设计 §1.6）：
    - W1 的局限"子查询里的 LIMIT 令外层漏追加"已修复——AST 只看根节点的
      limit 参数，`SELECT * FROM (SELECT ... LIMIT 5) t` 的外层会被正确追加；
    - `/* LIMIT 5 */` 注释不再骗过检查——LIMIT 判定来自 AST，注释不参与。
    """
    safe_sql, _had_explicit_limit = _prepare(sql, default_limit)
    return safe_sql


# ---------------------------------------------------------------------------
# 逃生门（W5 设计 §1.2）：W1 正则实现整体迁入，TEXT2SQL_SQLGUARD=legacy 时
# 由 sql_tools 选用。新校验上线后若线上出现设计未预料的误杀，切回正则实现
# 可分钟级恢复服务。deprecated：W6 删除（连同 config 的 legacy 分支）。
# 以下实现与 W1/W2 的 sql_tools 原文逐行一致（仅模块归属变化）。
# ---------------------------------------------------------------------------

# 写操作关键字黑名单。用 \b 词边界匹配，大小写不敏感。
# 说明：\b 是"单词字符与非单词字符的边界"，下划线属于单词字符，
# 所以 created_at / updated_at 这类列名不会被 \bUPDATE\b 误伤；
# 但字符串字面量 'update' 里的 update 会被误拒——W1 就接受这种保守误杀。
# 排序注意：多词短语放最前——FOR UPDATE 里含单词 UPDATE，先匹配短语
# 才能给模型报出最具体的原因（"检测到 FOR UPDATE"而不是笼统的 UPDATE）。
# W2 新增 6 个 MySQL 特有词（W2 设计 §1.3）：
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
    legacy 校验、LIMIT 判断都必须基于剥完注释的文本，
    否则注释里的关键字/分号/LIMIT 会造成误判。
    """
    cleaned = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    cleaned = re.sub(r"--[^\n]*", " ", cleaned)
    return cleaned


def _legacy_validate_readonly_sql(sql: str) -> tuple[bool, str]:
    """[deprecated，W6 删] W1 正则版只读校验：逃生门专用。

    检查顺序：剥注释 → 空检查 → 多语句检查 → 首关键字检查 → 黑名单检查。
    已知误杀：字符串字面量里的关键字（W5 已由 AST 版消除，此处保留旧行为）。
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


def _legacy_enforce_limit(sql: str, default_limit: int = 20) -> str:
    """[deprecated，W6 删] W1 正则版 LIMIT 补齐：逃生门专用。

    已知局限（W5 AST 版已修复，此处保留旧行为）：无法区分 LIMIT 写在主查询
    还是子查询里——子查询里写了 LIMIT 时主查询会被误判为"已有 LIMIT"。
    """
    stripped = _strip_comments(sql).strip()
    # 先摘掉结尾的分号，避免拼出 "...; LIMIT 20" 这种语法错误
    if stripped.endswith(";"):
        stripped = stripped[:-1].rstrip()

    if re.search(r"\bLIMIT\s+\d+\b", stripped, flags=re.IGNORECASE):
        return stripped  # 已有 LIMIT，原样返回
    return f"{stripped} LIMIT {default_limit}"


def _legacy_prepare(sql: str, default_limit: int = 20) -> tuple[str, bool]:
    """[deprecated，W6 删] legacy 模式的 _prepare：正则判定 had_explicit_limit。

    与 W1 执行链逐字节等价：had 判定用"剥注释文本里是否出现 LIMIT <数字>"，
    拼接目标为旧 enforce_limit 的产物（剥注释 + 摘尾分号）。
    """
    stripped = _strip_comments(sql).strip()
    if stripped.endswith(";"):
        stripped = stripped[:-1].rstrip()
    had_explicit_limit = bool(re.search(r"\bLIMIT\s+\d+\b", stripped, flags=re.IGNORECASE))
    if had_explicit_limit:
        return stripped, True
    return f"{stripped} LIMIT {default_limit}", False
