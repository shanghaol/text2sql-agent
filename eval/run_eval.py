"""W3 评测 runner：加载评测集 → 逐题运行 Agent → 重放判定 → 报告与明细落盘。

**同构契约（W3 设计 §2.3 / W4 设计 §3.1 / §6）**：W3 是"main ↔ eval 逐点对照"，
W4 起升级为"三方向 prepare_turn 看齐"——CLI（agent/main.py）、FastAPI
（agent/server.py）与本 runner 的每轮组装统一走 agent/core/turn.py::prepare_turn，
检索 query 拼接、混合检索、prompt 组装、build_agent 不再在 runner 内联。
本文件对 prepare_turn 的依赖只有两处调用（单轮 / 多轮各一）+ agent.invoke；
核心组装行为变更由 prepare_turn 自身的同构闸门验收（W4 §6.2）兜住。

运行方式（在仓库根目录）：
    .venv/Scripts/python.exe -m eval.run_eval --tag baseline_full --top-n 8 --runs 3
    .venv/Scripts/python.exe -m eval.run_eval --tag no_rag_control --top-n 0 --runs 1
    .venv/Scripts/python.exe -m eval.run_eval --tag smoke --limit 2 --runs 1            # 冒烟
    .venv/Scripts/python.exe -m eval.run_eval --tag flaky_check --rerun EV-017,EV-029   # 复跑指定题
    .venv/Scripts/python.exe -m eval.run_eval --tag multiturn --multiturn               # 多轮 4 题（§2.7）
    .venv/Scripts/python.exe -m eval.run_eval --tag vs_control --compare eval/reports/detail_xxx.json
    .venv/Scripts/python.exe -m eval.run_eval --tag rrf_k_10 --rrf-k 10 --runs 3       # W6 消融 E 行
    .venv/Scripts/python.exe -m eval.run_eval --tag rerank_bge --rerank --runs 3       # W6 消融 F 行（需 SILICONFLOW_API_KEY）
    .venv/Scripts/python.exe -m eval.run_eval --tag rerank_quota --rerank --quota --runs 3   # W7 FQ 行（doc_type 覆盖配额）

产物（eval/reports/，进 git——报告是简历证据链）：
    report_<tag>_<ts>.md    Markdown 汇总（§3.3 固定结构，声明段五条写死在模板里）
    detail_<tag>_<ts>.json  逐题逐 run 全量明细（W6 消融复用的数据接口）
"""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from langchain_core.messages import HumanMessage
from sqlalchemy import text

from agent.config import DEEPSEEK_MODEL, RERANK_MODEL, RERANK_POOL
from agent.core.agent import get_retriever
from agent.core.turn import RetrievalOptions, prepare_turn
from agent.rag.rerank import SiliconFlowReranker
from agent.tools.sql_tools import get_engine
from eval.judge import (
    extract_execute_calls,
    extract_final_answer_text,
    judge_case,
    load_dataset,
    load_multiturn_dataset,
    p95,
    pick_sql,
    sum_usage,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASET = REPO_ROOT / "eval" / "dataset" / "eval_v1.jsonl"
MULTITURN_DATASET = REPO_ROOT / "eval" / "dataset" / "eval_v1_multiturn.jsonl"
REPORTS_DIR = REPO_ROOT / "eval" / "reports"

# 评测器重放的行数保护（§2.4）：不套 enforce_limit（判定看逻辑结果集），
# 超过上限截断并在明细里标记 oversized。
REPLAY_MAX_ROWS = 5000

# 检索参数默认值 = hybrid_retriever 的线上默认（不传消融参数时实际生效的值）。
# W6 §3.4 修硬编码谎报：报告与 detail 的 RRF k 不再直接引用本常量渲染，改经
# effective_retrieval_config() 从实际生效参数取值（--rrf-k 10 行不再谎报 60）。
RETRIEVAL_TOP_K_EACH = 20
RRF_K = 60

# §1.3 分层口径：EV-001~011 即现有 11 条 QA（qa_pairs.md 原题，"有同款示例的题"），
# EV-012 起为泛化题。id 为零填充三位序号，字符串比较即数值比较。
FEWSHOT_LAYER_LAST_ID = "EV-011"

# W5 自举语料（§4.7-3）：qa_pairs.md 里 QA-012 起的片 = 用户点赞自动收录（absorb_feedback.py），
# 001~011 是人工精挑语料。自举命中统计的口径 = injected_chunks ∩ {qa_pair:qa_NNN | NNN ≥ 012}。
AUTO_ABSORB_MIN_NUMBER = 12

_QUESTION_TYPE_ORDER = ("single_table", "multi_join", "aggregation", "time_window", "glossary_trap")

# 声明段固定内容（§3.3：防数字被误读，五条写死在模板里；⑤ 为判定规则 v2 增补）
_DECLARATIONS = """\
## 声明（固定五条，W3 设计 §3.3）
1. 本报告是**系统级数字**：评测对象为"混合检索注入 + Agent"完整链路，含 few-shot 自举
   （qa_pair 片参与检索，11 条评测题原题在示例库中），不得表述为"模型的 NL2SQL 能力"（§1.3）。
2. temperature=0 仍存在服务端非确定性；主行（runs>1）为多次运行结果，W6 定稿前会复测（§3.2）。
3. 延迟绝对值受评测时网络波动影响，仅横向（题型间）与纵向（W6 前后对比）相对可比（§2.6）。
4. token 成本按当次实测累计（逐条 AIMessage.usage_metadata 累加；cache_read 单列，
   已含在 input 内不重复计）；人民币估算按当期牌价手填旁注，不写死单价（§2.6）。
5. 双层指标分列：SQL 级执行准确率只认重放结果集一致（pass_via=sql），答案级回答准确率
   额外计入最终回答文本计算命中金标的题（pass_via=text）；两者差值 = Agent 文本计算弥补
   SQL 单查询表达局限的部分，W6 消融时分别跟踪（§2.4 判定规则 v2）。
"""


# ---------------------------------------------------------------------------
# preflight（§2.1）：简版自包含十几行，不 import main.py 私有函数（复制比耦合便宜）
# ---------------------------------------------------------------------------


def preflight() -> None:
    """API key 非空 → MySQL SELECT 1 → Chroma count>0；任一失败给中文指引后退出。"""
    from agent.config import DEEPSEEK_API_KEY

    if not DEEPSEEK_API_KEY:
        print("未找到 DeepSeek API Key，无法评测。请按以下步骤配置：")
        print("  1. 到 https://platform.deepseek.com 注册并创建 API Key；")
        print("  2. 在仓库根目录 .env 中填入：DEEPSEEK_API_KEY=sk-xxxxxxxx；")
        print("  3. 重新运行本程序。")
        sys.exit(1)

    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 - preflight 要把任何连接失败变成人话
        print("MySQL 连接失败，无法评测。请按以下步骤排查：")
        print("  1. 容器没起是最常见原因：确认 MySQL 容器已启动（docker ps 看一眼）；")
        print("  2. 确认 .env 里 MYSQL_PORT 与 MYSQL_RO_PASSWORD 与容器内一致。")
        print(f"  原始错误：{exc}")
        sys.exit(1)

    try:
        from agent.rag.chroma_store import get_collection

        count = get_collection().count()
    except Exception as exc:  # noqa: BLE001
        print(f"读取 Chroma 索引失败：{exc}")
        print("请先运行：python -m agent.rag.build_index")
        sys.exit(1)
    if count <= 0:
        print("Chroma 索引为空，请先运行：python -m agent.rag.build_index")
        sys.exit(1)


# ---------------------------------------------------------------------------
# W6 消融入口（设计 §3.4）：CLI 开关 → RetrievalOptions → prepare_turn 透传；
# 无任何消融开关时 opts 恒为 None——prepare_turn 默认分支与线上逐字节等价（同构红线）
# ---------------------------------------------------------------------------


def _ablation_flags_present(args: argparse.Namespace) -> bool:
    """是否有任一消融开关被启用（getattr 兼容旧调用方直接构造的 Namespace）。"""
    return bool(
        getattr(args, "rrf_k", None) is not None
        or getattr(args, "no_bm25", False)
        or getattr(args, "no_dense", False)
        or getattr(args, "no_fewshot", False)
        or getattr(args, "rerank", False)
        or getattr(args, "quota", False)
    )


def _build_retrieval_opts(args: argparse.Namespace) -> RetrievalOptions | None:
    """从 CLI 开关构建 RetrievalOptions；无开关返回 None（走线上默认路径）。

    --rerank 在此构建 SiliconFlowReranker 实例（设计 §4.1/§4.4：仅评测侧接线，
    线上永不构造）——key 缺失时构造即抛 ValueError，main() 捕获后给中文指引退出
    （启动期失败，而不是跑了一半才在第一题上炸）。
    --quota 与 --no-fewshot 的互斥校验在 _parse_args（argparse 启动期）完成。
    """
    if not _ablation_flags_present(args):
        return None
    reranker = None
    rerank_pool = 0
    if args.rerank:
        reranker = SiliconFlowReranker()  # 无 key 抛 ValueError（中文指引）
        rerank_pool = RERANK_POOL  # 16 = 2×top_n 起点（设计 §4.2）
    return RetrievalOptions(
        rrf_k=args.rrf_k,
        doc_types=["schema", "glossary"] if args.no_fewshot else None,
        enable_bm25=not args.no_bm25,
        enable_dense=not args.no_dense,
        rerank_pool=rerank_pool,
        reranker=reranker,
        quota=args.quota,
    )


def effective_retrieval_config(args: argparse.Namespace) -> dict:
    """实际生效的全部检索参数（W6 §3.4 修硬编码谎报的唯一事实源）。

    detail config 与报告的"检索参数"行都从这里取——--rrf-k 10 的行如实记
    rrf_k=10，不再引用模块常量谎报 60。默认（无开关）时返回值与线上默认一致。
    """
    rerank_enabled = bool(getattr(args, "rerank", False))
    return {
        "top_k_each": RETRIEVAL_TOP_K_EACH,  # runner 无此开关，恒为线上默认 20
        "rrf_k": args.rrf_k if getattr(args, "rrf_k", None) is not None else RRF_K,
        "enable_bm25": not getattr(args, "no_bm25", False),
        "enable_dense": not getattr(args, "no_dense", False),
        "doc_types": ["schema", "glossary"] if getattr(args, "no_fewshot", False) else None,
        "rerank": rerank_enabled,
        "rerank_pool": RERANK_POOL if rerank_enabled else 0,
        "rerank_model": RERANK_MODEL if rerank_enabled else None,
        "quota": bool(getattr(args, "quota", False)),
    }


def _describe_retrieval(retrieval_cfg: dict) -> str:
    """消融参数生效时的单行日志描述（默认行不需要，报告行另走 _render_report）。"""
    bits = [
        f"rrf_k={retrieval_cfg['rrf_k']}",
        f"BM25路={'开' if retrieval_cfg['enable_bm25'] else '关'}",
        f"向量路={'开' if retrieval_cfg['enable_dense'] else '关'}",
    ]
    if retrieval_cfg["doc_types"] is not None:
        bits.append(f"doc_types={'+'.join(retrieval_cfg['doc_types'])}（去 few-shot）")
    if retrieval_cfg["rerank"]:
        bits.append(f"rerank={retrieval_cfg['rerank_model']}（pool={retrieval_cfg['rerank_pool']}）")
    if retrieval_cfg["quota"]:
        bits.append("quota=开（doc_type 覆盖保底，W7 §1.3）")
    return "，".join(bits)


def _corpus_commit() -> str:
    """git HEAD 短哈希（设计 §3.5 语料冻结证据链）：detail config 的 corpus_commit。

    非 git 环境 / git 不可用时返回 "unknown"（不阻断评测——freeze 断言在
    summarize_ablation 侧做，那里不一致才拒绝出表）。
    """
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
            timeout=10,
            check=False,  # 非 0 返回码走 returncode 分支降级 unknown，不抛
        )
    except Exception:  # noqa: BLE001 - git 缺失/超时不该炸评测，降级 unknown
        return "unknown"
    if proc.returncode == 0 and proc.stdout.strip():
        return proc.stdout.strip()
    return "unknown"


def _build_detail_config(
    args: argparse.Namespace,
    dataset_path: Path,
    started_at: str,
    retrieval_cfg: dict,
    corpus_counts: dict[str, int],
) -> dict:
    """detail JSON 的 config 节（W6 §3.4/§3.5：实际生效检索参数 + 语料冻结证据链）。

    retrieval（实际生效值，唯一事实源 effective_retrieval_config）与 corpus
    （commit 短哈希 + 片数）是 summarize_ablation 启动断言的消费面——
    行间任一不一致，该行作废重跑（§3.5-3）。
    """
    return {
        "dataset": str(dataset_path),
        "multiturn": args.multiturn,
        "top_n": args.top_n,
        "runs": args.runs,
        "model": DEEPSEEK_MODEL,
        "temperature": 0,
        "snapshot_anchor": "2026-09-30",
        "retrieval": retrieval_cfg,
        "corpus": {
            "commit": _corpus_commit(),
            "counts": corpus_counts,
            "total": sum(corpus_counts.values()),
        },
        "replay_max_rows": REPLAY_MAX_ROWS,
    }


# ---------------------------------------------------------------------------
# SQL 重放器（§2.4）：评测器自己执行金标与模型 SQL 拿原生结果，不解析 ToolMessage 文本
# ---------------------------------------------------------------------------


class Replayer:
    """get_engine() 直连重放：同一只读账号 + 会话只读；不套 enforce_limit
    （判定看逻辑结果集），仅做行数保护（REPLAY_MAX_ROWS，超出截断并标记 oversized）。
    同一 SQL 的重放结果进程内缓存：pick_sql 三级规则会反复试探，缓存避免重复打库。
    """

    def __init__(self) -> None:
        self._cache: dict[str, list[tuple] | None] = {}
        self.oversized: set[str] = set()  # 命中行数保护被截断的 SQL

    def __call__(self, sql: str) -> list[tuple] | None:
        if sql not in self._cache:
            self._cache[sql] = self._execute(sql)
        return self._cache[sql]

    def _execute(self, sql: str) -> list[tuple] | None:
        try:
            with get_engine().connect() as conn:
                fetched = conn.execute(text(sql)).fetchmany(REPLAY_MAX_ROWS + 1)
        except Exception:  # noqa: BLE001 - 重放失败 = 判定原料缺失，按 None 交给三级规则/调用方
            return None
        if len(fetched) > REPLAY_MAX_ROWS:
            self.oversized.add(sql)
            fetched = fetched[:REPLAY_MAX_ROWS]
        return [tuple(row) for row in fetched]


# ---------------------------------------------------------------------------
# 逐题评测（组装走 prepare_turn，W4 §3.1 三方看齐；对照锚点承 W3 §2.3）
# ---------------------------------------------------------------------------


def evaluate_single_case(
    case: dict, run_idx: int, top_n: int, replayer: Replayer, retrieval_opts: RetrievalOptions | None = None
) -> dict:
    """跑一题单轮评测，返回 §3.3 明细记录。

    与线上 main.py 的逐点同构对照（W3 设计 §2.3 表格；W4 起组装三步收进
    prepare_turn，锚点标注它接管了哪些点）：
    [1 thread]  线上=启动生成一个 thread、会话内共享；评测=每题每 run 唯一——
                有意的更细隔离粒度，防跨题污染（前一题的口径/SQL/结果残留）。
    [2 query]   线上=previous_human_message + 输入；评测 thread 恒为新会话，
                previous_human_message 返回空串 → query == question 本身（与线上首轮一致）。
                —— prepare_turn 内部逐字符同款实现（W4 §3.1）。
    [3 search]  prepare_turn → get_retriever().search(query, top_n)，doc_types 缺省全类型
                ——同一函数同一参数路径；top_n=8 时与线上逐字节一致（检索全链路确定性）。
                W6 消融行经 retrieval_opts 走参数化分支（设计 §3.4），None 时仍逐字节等价。
    [4 prompt]  prepare_turn → build_agent(build_system_prompt(format_context_block(chunks)))
                ——原样 import 零差异，收进 prepare_turn 内部（W4 §3.1）。
    [5 调用]    invoke 替代 stream——同一 LangGraph 执行、消息状态语义相同，评测取全量消息。
    [6 计时]    perf_counter 从 prepare_turn 之前起、invoke 返回止（检索计入用户体感，
                口径与 W3"search 之前起"一致——组装段就是原 search+prompt 段）。
    [7 兜底]    检索空 → build_system_prompt 内部换兜底文案（top_n=0 走线上真实降级路径）。
    """
    case_id = case["id"]
    thread_id = f"eval-{case_id}-r{run_idx}"  # [1]

    start = time.perf_counter()  # [6] 计时起点：组装（检索）之前
    pt = prepare_turn(case["question"], thread_id, top_n=top_n, retrieval_opts=retrieval_opts)  # [2][3][4] 三方看齐 prepare_turn
    result = pt.agent.invoke({"messages": [HumanMessage(content=case["question"])]}, config=pt.config)  # [5]
    latency_s = time.perf_counter() - start  # [6] 计时终点：invoke 返回

    messages = result["messages"]
    return _finalize_record(
        case=case,
        run_idx=run_idx,
        replayer=replayer,
        sql_calls=extract_execute_calls(messages),
        injected_ids=[chunk.id for chunk in pt.injected_chunks],  # 来源不变：仍是检索产物
        latency_s=latency_s,
        tokens=sum_usage(messages),
        # §2.4 v2 答案级回退的取材面：最后一条 AIMessage 的文本（明细落盘审计用）
        final_answer_text=extract_final_answer_text(messages),
    )


def evaluate_multiturn_case(
    case: dict, run_idx: int, top_n: int, replayer: Replayer, retrieval_opts: RetrievalOptions | None = None
) -> dict:
    """跑一个多轮 case（§2.7）：逐轮 invoke 共享 thread，判定只看最后一轮。

    与单轮的差异（每点仍与线上多轮同构，§2.3 表 + §2.7 增补；W4 起每轮组装
    走 prepare_turn——多轮检索 query 拼接在其内部逐字符同款实现）：
    - thread_id 同 case 内共享（多轮记忆生效——这正是被测对象，W2 §3.6 的量化场景）；
    - 每轮检索 query = previous_human_message + 当前轮输入（prepare_turn 内部拼接，
      与 main.py 逐字符同款）；
    - 每轮全量记录明细（query / 注入 / 工具调用 / 延迟 / token）；
    - 金标 = final_gold_sql，只判最后一轮产出的 SQL；中间轮是上下文提供者不单独判分；
    - Recall 取最后一轮的注入（q1 注入是"为 q2 铺垫"的副产物，计入会稀释指标）。
    """
    thread_id = f"eval-{case['id']}-r{run_idx}"  # case 内共享、跨 case/run 唯一

    turn_records: list[dict] = []
    total_latency = 0.0
    final_calls: list[dict] = []
    final_injected: list[str] = []
    messages: list = []
    for turn_idx, user_input in enumerate(case["turns"], 1):
        start = time.perf_counter()
        # 组装 + 调用：prepare_turn 覆盖 W3 §2.3 的 [2][3][4]，invoke 即 [5]
        pt = prepare_turn(user_input, thread_id, top_n=top_n, retrieval_opts=retrieval_opts)
        result = pt.agent.invoke({"messages": [HumanMessage(content=user_input)]}, config=pt.config)
        latency = time.perf_counter() - start
        total_latency += latency

        messages = result["messages"]
        calls = extract_execute_calls(messages)
        injected_ids = [chunk.id for chunk in pt.injected_chunks]
        turn_records.append(
            {
                "turn_idx": turn_idx,
                "question": user_input,
                "retrieval_query": pt.retrieval_query,
                "injected_chunks": injected_ids,
                "sql_calls": calls,
                "latency_s": round(latency, 3),
                "tokens": sum_usage(messages),
            }
        )
        final_calls, final_injected = calls, injected_ids

    tokens = {
        key: sum(turn["tokens"][key] for turn in turn_records)
        for key in ("input", "output", "total", "cache_read")
    }
    # 判定只吃最后一轮的 SQL 与最终回答（§2.7 / §2.4 v2）；
    # 最终轮无可用 SQL → 先试答案级回退，不适用才 error no_usable_sql（三级规则 + v2 规则一）
    return _finalize_record(
        case=case,
        run_idx=run_idx,
        replayer=replayer,
        sql_calls=final_calls,
        injected_ids=final_injected,
        latency_s=total_latency,
        tokens=tokens,
        turn_records=turn_records,
        final_answer_text=extract_final_answer_text(messages),  # messages 即最后一轮的消息链
    )


def _finalize_record(
    case: dict,
    run_idx: int,
    replayer: Replayer,
    sql_calls: list[dict],
    injected_ids: list[str],
    latency_s: float,
    tokens: dict,
    turn_records: list[dict] | None = None,
    final_answer_text: str | None = None,
) -> dict:
    """从一轮（多轮即最后一轮）的调用序列走到判定结果，组装 §3.3 明细记录。

    §2.4 判定规则 v2 的明细字段扩展：final_answer_text（最后一条 AIMessage 全文，审计用）、
    pass_via（"sql" / "text" / null）、text_matched（文本回退命中的候选值，字符串形式）。
    """
    is_multiturn = "turns" in case
    gold_sql = case["final_gold_sql"] if is_multiturn else case["gold_sql"]
    record: dict = {
        "id": case["id"],
        "run_idx": run_idx,
        "question_type": case.get("question_type"),
        # W6 §2.3 分层字段随记录落盘（additive）：hit/near/far 三分层的报告与
        # 消融交叉表消费；旧数据集/多轮无该字段时为 None（报告按 id 规则兜底归层）
        "generalize_layer": case.get("generalize_layer"),
        "injected_chunks": injected_ids,
        "expected_chunks": list(case["expected_chunks"]),
        "recall_hits": [c for c in case["expected_chunks"] if c in set(injected_ids)],
        "sql_calls": sql_calls,
        "sql_used": None,
        "sql_source": None,
        "gold_row_count": None,
        "model_row_count": None,
        "judge": "error",
        "pass_via": None,
        "text_matched": None,
        "final_answer_text": final_answer_text,
        "failure_mode": None,
        "error_msg": None,
        "latency_s": round(latency_s, 3),
        "tokens": tokens,
        "oversized": False,
        "ended_with_failed_call": False,
    }
    if turn_records is not None:
        record["turns"] = turn_records

    # 金标重放失败 → 本题无法判定（error/replay_error；T1 verify_gold 核对应已拦住）
    gold_rows = replayer(gold_sql)
    if gold_rows is None:
        record["error_msg"] = "replay_error"
        return record

    # SQL 提取三级规则（§2.2）：最后成功 → 回退更早成功 → no_usable_sql
    used_sql, sql_source = pick_sql(sql_calls, replayer)
    if used_sql is None:
        # §2.4 v2 规则一：无可用 SQL 先试答案级回退（文本命中单行单列金标 → pass_via=text），
        # 不适用才 error；gold_row_count 已知，一并记录
        verdict = judge_case(
            gold_rows, None, error_msg="no_usable_sql", final_answer_text=final_answer_text
        )
        record.update(
            {
                "gold_row_count": len(gold_rows),
                "judge": verdict["judge"],
                "pass_via": verdict["pass_via"],
                "text_matched": _text_matched_str(verdict["text_matched"]),
                "failure_mode": verdict["failure_mode"],
                "error_msg": verdict["error_msg"],
            }
        )
        return record
    model_rows = replayer(used_sql)  # pick_sql 已保证非 None（重放有缓存）

    verdict = judge_case(
        gold_rows,
        model_rows,
        ordered=case["ordered"],
        standard_wrong=case["standard_wrong"],
        final_answer_text=final_answer_text,
    )
    record.update(
        {
            "sql_used": used_sql,
            "sql_source": sql_source,
            "gold_row_count": len(gold_rows),
            "model_row_count": len(model_rows),
            "judge": verdict["judge"],
            "pass_via": verdict["pass_via"],
            "text_matched": _text_matched_str(verdict["text_matched"]),
            "failure_mode": verdict["failure_mode"],
            # §2.2：以失败调用收尾（自我修正失败）单独留痕统计
            "ended_with_failed_call": sql_source == "fallback_to_earlier",
            "oversized": used_sql in replayer.oversized or gold_sql in replayer.oversized,
        }
    )
    return record


def _text_matched_str(text_matched) -> str | None:
    """text_matched（Decimal 候选值）的明细存储形式：str 化以便 JSON 序列化。"""
    return str(text_matched) if text_matched is not None else None


def _invoke_exception_record(case: dict, run_idx: int, exc: Exception) -> dict:
    """题级 try/except 的兜底记录（§6 风险 4：单题异常不中断整场）。"""
    return {
        "id": case["id"],
        "run_idx": run_idx,
        "question_type": case.get("question_type"),
        "generalize_layer": case.get("generalize_layer"),
        "injected_chunks": [],
        "expected_chunks": list(case["expected_chunks"]),
        "recall_hits": [],
        "sql_calls": [],
        "sql_used": None,
        "sql_source": None,
        "gold_row_count": None,
        "model_row_count": None,
        "judge": "error",
        "pass_via": None,
        "text_matched": None,
        "final_answer_text": None,
        "failure_mode": None,
        "error_msg": "invoke_exception",
        "error_detail": str(exc)[:500],
        "latency_s": None,
        "tokens": {"input": 0, "output": 0, "total": 0, "cache_read": 0},
        "oversized": False,
        "ended_with_failed_call": False,
    }


# ---------------------------------------------------------------------------
# 指标汇总与报告渲染（§2.5 / §2.6 / §3.3）
# ---------------------------------------------------------------------------


def _split_accuracy(records: list[dict]) -> tuple[int, int, int, float]:
    """回答准确率·答案级（errors 计入失败，不藏分）：返回 (pass, fail, error, 准确率)。

    §2.4 判定规则 v2：judge=="pass" 含 SQL 级（pass_via=sql）与文本计算兜底
    （pass_via=text）两种途径；SQL 级严格口径见 _pass_via_counts / _sql_acc。
    """
    passed = sum(1 for r in records if r["judge"] == "pass")
    failed = sum(1 for r in records if r["judge"] == "fail")
    errored = sum(1 for r in records if r["judge"] == "error")
    total = len(records)
    return passed, failed, errored, (passed / total if total else 0.0)


def _pass_via_counts(records: list[dict]) -> tuple[int, int]:
    """pass_via 构成：返回 (sql 判过数, text 判过数)（§2.4 v2 双层指标）。

    兼容 v1 旧明细（--compare 读入的对照行 detail 可能没有 pass_via 字段）：
    judge=pass 且无该字段的一律按 SQL 级计数（v1 判定即 SQL 级）。
    """
    sql = text = 0
    for record in records:
        if record["judge"] != "pass":
            continue
        if record.get("pass_via") == "text":
            text += 1
        else:  # "sql" 或 v1 明细缺字段
            sql += 1
    return sql, text


def _sql_acc(records: list[dict]) -> float:
    """执行准确率·SQL 级：pass_via=sql 的题数 ÷ 总数（严格口径，简历主数字）。"""
    sql_pass, _ = _pass_via_counts(records)
    return sql_pass / len(records) if records else 0.0


def _fmt_pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def _recall_of(records: list[dict]) -> tuple[int, int, float]:
    """逐片 Recall = Σ|∩| / Σ|expected|（§2.5）。"""
    hits = sum(len(r["recall_hits"]) for r in records)
    expected = sum(len(r["expected_chunks"]) for r in records)
    return hits, expected, (hits / expected if expected else 0.0)


def _doc_type_of(chunk_id: str) -> str:
    """chunk id 形如 schema:orders / glossary:gmv / qa_pair:qa_003，前缀即 doc_type。"""
    return chunk_id.split(":", 1)[0]


def _is_fewshot_layer(record: dict) -> bool:
    """§1.3 分层：EV-001~011 是"有同款示例的题"，EV-012 起是"靠泛化的题"。"""
    return record["id"].startswith("EV-") and record["id"] <= FEWSHOT_LAYER_LAST_ID


# W6 §2.3 三分层：示例层（hit）由 id 前缀规则判定（EV-001~011 恒为 hit，旧明细
# 无 generalize_layer 字段也能归层）；近示例（near）/ 远示例（far）是命题时静态
# 标注的字段，无法从 id 推导——缺字段且过 EV-011 的记录归"未分层"兜底行。
_LAYER_ROWS: tuple[tuple[str, str], ...] = (
    ("hit", "示例层（hit：EV-001~011 有同款示例，§1.3）"),
    ("near", "近示例（near：泛化题，有同口径/同构 QA 可改窗口改维度套用，W6 §2.3）"),
    ("far", "远示例（far：泛化题，须 schema+glossary 组合推理，W6 §2.3）"),
)


def _layer_of(record: dict) -> str | None:
    """记录 → hit/near/far 三分层；EV-012 起且缺 generalize_layer 字段的旧记录返回 None。"""
    if _is_fewshot_layer(record):
        return "hit"
    return record.get("generalize_layer")


def _layer_groups(records: list[dict]) -> list[tuple[str, list[dict]]]:
    """W6 §2.3 分层表的三分组（hit/near/far）+ 旧数据集未分层兜底行（仅在存在
    EV-012+ 且无 generalize_layer 字段的记录时出现——30 条旧明细直渲不破）。"""
    groups = [
        (label, [r for r in records if _layer_of(r) == layer]) for layer, label in _LAYER_ROWS
    ]
    legacy = [r for r in records if _layer_of(r) is None]
    if legacy:
        groups.append(("泛化题（未分层：旧数据集无 generalize_layer 字段）", legacy))
    return groups


def _is_absorbed_qa_chunk(chunk_id: str) -> bool:
    """是否自举语料片：qa_pair:qa_NNN 且 NNN ≥ 012（W5 §4.6 自举从 012 起编号）。"""
    if not chunk_id.startswith("qa_pair:qa_"):
        return False
    suffix = chunk_id.removeprefix("qa_pair:qa_")
    return suffix.isdigit() and len(suffix) == 3 and int(suffix) >= AUTO_ABSORB_MIN_NUMBER


def absorbed_chunk_hits(records: list[dict]) -> dict[str, list[str]]:
    """自举语料命中统计（W5 §4.7-3，additive）：case id → 注入的自举片列表。

    口径 = injected_chunks ∩ {qa_pair:qa_NNN | NNN ≥ 012}；只返回非空 case。
    """
    hits: dict[str, list[str]] = {}
    for record in records:
        absorbed = sorted({c for c in record["injected_chunks"] if _is_absorbed_qa_chunk(c)})
        if absorbed:
            hits[record["id"]] = absorbed
    return hits


def corpus_type_counts(retriever) -> dict[str, int]:
    """从检索器缓存的 collection 元数据实拉统计语料片数（W5 §4.7-2 报告新增行）。

    retriever.metas 是 rebuild() 时 collection.get() 的全量快照——语料版本
    与本次评测实际注入同源，不额外打一次 Chroma。
    """
    counts: Counter = Counter(meta.get("doc_type", "") for meta in retriever.metas)
    return {doc_type: counts.get(doc_type, 0) for doc_type in ("schema", "glossary", "qa_pair")}


def _accuracy_table(records: list[dict], groups: list[tuple[str, list[dict]]]) -> list[str]:
    """分组准确率表（§3.3 v2）：SQL 级 pass/fail/error 计数 + SQL 级与答案级双列准确率。"""
    lines = [
        "| 分组 | SQL 判过 | 文本判过 | fail | error | SQL 级准确率 | 答案级准确率 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for label, group in groups:
        total = len(group)
        sql_pass, text_pass = _pass_via_counts(group)
        failed = sum(1 for r in group if r["judge"] == "fail")
        errored = sum(1 for r in group if r["judge"] == "error")
        sql_acc = sql_pass / total if total else 0.0
        answer_acc = (sql_pass + text_pass) / total if total else 0.0
        lines.append(
            f"| {label} | {sql_pass} | {text_pass} | {failed} | {errored} | {_fmt_pct(sql_acc)} | {_fmt_pct(answer_acc)} |"
        )
    return lines


def _render_report(
    args: argparse.Namespace,
    dataset_path: Path,
    records: list[dict],
    started_at: str,
    corpus_counts: dict[str, int] | None = None,
) -> str:
    """§3.3 固定结构的 Markdown 汇总报告。

    corpus_counts（W5 §4.7-2，additive）：语料片数统计，None 时不输出该行
    （直调本函数的旧单测/兼容路径行为不变）；main() 从 retriever 实拉传入。
    """
    n_records = len(records)
    _, failed, errored, acc = _split_accuracy(records)
    is_multiturn = bool(args.multiturn)

    lines: list[str] = []
    lines.append(f"# W3 基线评测报告：{args.tag}（{started_at}）")
    lines.append("")
    lines.append("## 运行配置")
    try:
        dataset_display = str(dataset_path.relative_to(REPO_ROOT))
    except ValueError:
        dataset_display = str(dataset_path)
    mode_label = "多轮（判定只看最后一轮，§2.7）" if is_multiturn else "单轮"
    lines.append(f"- 数据集：`{dataset_display}`（{len(records) // max(args.runs, 1)} 条，{mode_label}）")
    lines.append(f"- top_n={args.top_n} ｜ runs={args.runs} ｜ 本次共 {n_records} 次题级运行")
    lines.append(f"- 模型：{DEEPSEEK_MODEL}（temperature=0）｜ 快照锚点：2026-09-30")
    # W6 §3.4：从实际生效参数取值渲染（--rrf-k 10 行不再谎报 60）；默认行文案与 W3 逐字节一致
    retrieval_cfg = effective_retrieval_config(args)
    if _ablation_flags_present(args):
        lines.append(f"- 检索参数：{_describe_retrieval(retrieval_cfg)}（消融行，实际生效值，W6 §3.4）")
    else:
        lines.append(
            f"- 检索参数：top_k_each={RETRIEVAL_TOP_K_EACH}，RRF k={RRF_K}（与线上 hybrid_retriever 默认一致，§2.3 同构）"
        )
    if corpus_counts is not None:
        # W5 §4.7-2：语料片数进报告——语料从 28 片变 28+k 片后 top-8 竞争加剧，
        # Recall 数字的变化是"检索环境改变"而非退化；此行让任意两次报告可对齐语料版本
        total = sum(corpus_counts.values())
        counts_text = " / ".join(
            f"{doc_type} {corpus_counts.get(doc_type, 0)}" for doc_type in ("schema", "glossary", "qa_pair")
        )
        lines.append(f"- 语料：{counts_text}（共 {total} 片，索引自 data/rag_corpus 删除重建）")
    lines.append("")

    # —— 四指标汇总 · 执行准确率（SQL 级）——
    sql_pass, text_pass = _pass_via_counts(records)
    sql_acc = _sql_acc(records)
    lines.append("## 四指标汇总")
    lines.append("")
    lines.append("### 执行准确率（SQL 级：比较重放结果集，不看 SQL 文本）")
    lines.append(
        f"- overall：**{_fmt_pct(sql_acc)}**（SQL 判过 {sql_pass} ｜ 文本判过 {text_pass}"
        f"（计入答案级，见下节）｜ fail {failed} ｜ error {errored}，共 {n_records} 次）"
    )
    if args.runs > 1:
        per_run = []
        for run_idx in range(1, args.runs + 1):
            run_records = [r for r in records if r["run_idx"] == run_idx]
            per_run.append(f"r{run_idx}={_fmt_pct(_sql_acc(run_records))}")
        lines.append(f"- 各 run：{' ｜ '.join(per_run)}（三次结果不一致的题见 flaky 节，§3.2 不机械取众数）")
    if is_multiturn:
        lines.append("- 多轮题不设题型/分层维度（MT schema 无 question_type，§1.7 独立报告节）")
    else:
        type_groups = [
            (qtype, [r for r in records if r["question_type"] == qtype]) for qtype in _QUESTION_TYPE_ORDER
        ]
        lines.extend(_accuracy_table(records, type_groups))
        # W6 §2.3：分层表从二分（示例/泛化）升级为三分（hit/near/far）——
        # G 行（去 few-shot）消融后按层拆 Δ，量化"自举的作用半径"
        lines.extend(_accuracy_table(records, _layer_groups(records)))
    lines.append("")

    # —— 四指标汇总 · 回答准确率（答案级，§2.4 判定规则 v2）——
    lines.append("### 回答准确率（答案级，含文本计算）")
    lines.append(
        f"- overall：**{_fmt_pct(acc)}**（pass_via：sql {sql_pass} + text {text_pass}，共 {n_records} 次）"
    )
    lines.append(
        f"- 差值说明：答案级 − SQL 级 = {_fmt_pct(acc - sql_acc)}"
        f"（文本判过 {text_pass} 题）= Agent 文本计算弥补单条 SQL 表达局限的部分，"
        "W6 消融时分别跟踪（§2.4 v2 / 声明⑤）"
    )
    lines.append("")

    # —— 四指标汇总 · Recall@{top_n}（W6 §3.4 标题动态化：D1/D2 行的指标名才诚实）——
    lines.append(f"### Recall@{args.top_n}")
    if args.top_n <= 0:
        lines.append(
            "- 对照行（top_n=0）注入恒空，Recall=0 是设定使然，本节不列数字；报告只做执行准确率对比（§2.5）"
        )
    else:
        hits, expected, recall = _recall_of(records)
        full_hits = sum(
            1 for r in records if set(r["expected_chunks"]) <= set(r["injected_chunks"])
        )
        lines.append(
            f"- 整体逐片 Recall：**{_fmt_pct(recall)}**（Σ|∩|={hits} / Σ|expected|={expected}）"
        )
        lines.append(
            f"- 题级全命中率（expected 全部命中）：{_fmt_pct(full_hits / n_records if n_records else 0.0)}"
            f"（{full_hits}/{n_records} 题）"
        )
        doc_types = ("schema", "glossary", "qa_pair")
        per_doc = []
        for doc_type in doc_types:
            subset = [
                (
                    [c for c in r["expected_chunks"] if _doc_type_of(c) == doc_type],
                    [c for c in r["recall_hits"] if _doc_type_of(c) == doc_type],
                )
                for r in records
            ]
            exp_n = sum(len(e) for e, _ in subset)
            hit_n = sum(len(h) for _, h in subset)
            per_doc.append(f"{doc_type} {_fmt_pct(hit_n / exp_n if exp_n else 0.0)}")
        lines.append(f"- 分 doc_type：{' ｜ '.join(per_doc)}")
        if not is_multiturn:
            per_type = []
            for qtype in _QUESTION_TYPE_ORDER:
                h, e, _ = _recall_of([r for r in records if r["question_type"] == qtype])
                per_type.append(f"{qtype} {_fmt_pct(h / e if e else 0.0)}")
            lines.append(f"- 分题型：{' ｜ '.join(per_type)}")
        else:
            lines.append("- 末轮注入口径（§2.7：取最后一轮的注入，q1 铺垫注入不计入）")
    lines.append("")

    # —— 四指标汇总 · 延迟 ——
    latencies = [r["latency_s"] for r in records if isinstance(r["latency_s"], (int, float))]
    lines.append("### 延迟（wall-clock：search 前起、invoke 返回止，含全部工具往返）")
    if latencies:
        lines.append(
            f"- mean {sum(latencies) / len(latencies):.1f}s ｜ median {statistics.median(latencies):.1f}s"
            f" ｜ P95 {p95(latencies):.1f}s ｜ max {max(latencies):.1f}s"
        )
        lines.append(
            "- P95 口径：statistics.quantiles(n=20, method='inclusive')[18]（线性插值，§8-5 拍板）"
        )
        if not is_multiturn:
            per_type_p95 = []
            for qtype in _QUESTION_TYPE_ORDER:
                values = [
                    r["latency_s"]
                    for r in records
                    if r["question_type"] == qtype and isinstance(r["latency_s"], (int, float))
                ]
                if values:
                    per_type_p95.append(f"{qtype} {p95(values):.1f}s")
            lines.append(f"- 分题型 P95：{' ｜ '.join(per_type_p95)}")
        lines.append("- 延迟绝对值受网络波动影响，仅相对可比（见声明③）")
    else:
        lines.append("- 无可用延迟样本（全部题级异常）")
    lines.append("")

    # —— 四指标汇总 · token ——
    totals = {
        key: sum(r["tokens"][key] for r in records) for key in ("input", "output", "total", "cache_read")
    }
    lines.append("### token 成本（累加全部 AIMessage.usage_metadata，计费口径 §2.6）")
    if n_records:
        lines.append(
            f"- 单题均值：input {totals['input'] / n_records:.0f} ｜ output {totals['output'] / n_records:.0f}"
            f" ｜ total {totals['total'] / n_records:.0f}（cache_read {totals['cache_read'] / n_records:.0f} 单列，不重复计）"
        )
    lines.append(
        f"- 总量：input {totals['input']} ｜ output {totals['output']} ｜ total {totals['total']}"
        f"（cache_read {totals['cache_read']}）；人民币估算按当期牌价手填旁注"
    )
    lines.append("")

    # —— 对照行对比表（§3.3）——
    lines.append("## 对照行对比表")
    if getattr(args, "compare", None):
        compare_data = json.loads(Path(args.compare).read_text(encoding="utf-8"))
        compare_records = compare_data.get("cases", [])
        compare_tag = compare_data.get("tag", Path(args.compare).stem)
        lines.append(f"| 题型 | 本行（{args.tag}） | 对照（{compare_tag}） | 差值（百分点） |")
        lines.append("| --- | --- | --- | --- |")
        mine_overall = _split_accuracy(records)[3]
        theirs_overall = _split_accuracy(compare_records)[3]
        mine_sql = _sql_acc(records)
        theirs_sql = _sql_acc(compare_records)
        lines.append(
            f"| overall（答案级） | {_fmt_pct(mine_overall)} | {_fmt_pct(theirs_overall)} | {(mine_overall - theirs_overall) * 100:+.1f} |"
        )
        lines.append(
            f"| overall（SQL 级） | {_fmt_pct(mine_sql)} | {_fmt_pct(theirs_sql)} | {(mine_sql - theirs_sql) * 100:+.1f} |"
        )
        if not is_multiturn:
            for qtype in _QUESTION_TYPE_ORDER:
                mine = _sql_acc([r for r in records if r["question_type"] == qtype])
                theirs = _sql_acc([r for r in compare_records if r["question_type"] == qtype])
                lines.append(f"| {qtype} | {_fmt_pct(mine)} | {_fmt_pct(theirs)} | {(mine - theirs) * 100:+.1f} |")
    else:
        lines.append("（本次运行未提供 --compare 明细文件；对照行对比由 T3 汇主行与对照行两份 detail JSON 后生成。）")
    lines.append("")

    # —— 错答模式统计（§4.3）——
    lines.append("## 错答模式统计")
    mode_counter: Counter[str] = Counter()
    mode_samples: dict[str, list[str]] = {}
    for record in records:
        if record["judge"] == "pass":
            continue
        mode = (
            record["failure_mode"]
            if record["judge"] == "fail"
            else f"error:{record.get('error_msg') or 'unknown'}"
        )
        mode_counter[mode] += 1
        samples = mode_samples.setdefault(mode, [])
        if record["id"] not in samples:
            samples.append(record["id"])
    if mode_counter:
        lines.append("| failure_mode | 次数 | 典型样本（case id） |")
        lines.append("| --- | --- | --- |")
        for mode, count in mode_counter.most_common():
            lines.append(f"| {mode} | {count} | {'、'.join(mode_samples[mode][:3])} |")
        lines.append("")
        lines.append(
            "trap:<id> 与 valid_alt_gloss 分开统计（§4.3）：前者是真陷阱，后者是合法口径但答非所问；"
            "other 为错出新花样，人工看明细 JSON 归类后可回填登记表。"
        )
    else:
        lines.append("（本次运行无 fail / error 记录。）")
    lines.append("")

    # —— flaky 题记录（§3.2）——
    lines.append("## flaky 题记录")
    by_id: dict[str, list[str]] = {}
    for record in records:
        by_id.setdefault(record["id"], []).append(record["judge"])
    flaky = {cid: judges for cid, judges in by_id.items() if len(set(judges)) > 1}
    if flaky:
        lines.append("| id | 各 run 判定 |")
        lines.append("| --- | --- |")
        for cid, judges in sorted(flaky.items()):
            lines.append(f"| {cid} | {' → '.join(judges)} |")
        lines.append("")
        lines.append("flaky 题不机械取众数：人工复盘明细 JSON（三次的 SQL 差在哪），结论记入 experiment-log（§3.2）。")
    else:
        runs_hint = "（runs>1 时才可能出现翻转）" if args.runs > 1 else "（本次 runs=1，无从翻转）"
        lines.append(f"（无 {runs_hint}）")
    lines.append("")

    # —— 多轮评测节（§3.3 固定结构 + §2.7）——
    lines.append("## 多轮评测节")
    if is_multiturn:
        mt_passed, mt_failed, mt_errored, mt_acc = _split_accuracy(records)
        lines.append(
            f"- 多轮准确率（只判最后一轮）：**{_fmt_pct(mt_acc)}**（pass {mt_passed} / fail {mt_failed} / error {mt_errored}）"
        )
        lines.append("| id | 轮数 | 判定 | failure_mode | 末轮 expected | 末轮命中 |")
        lines.append("| --- | --- | --- | --- | --- | --- |")
        for record in records:
            turns = record.get("turns")  # invoke_exception 兜底记录没有轮级明细
            if turns is None:
                lines.append(
                    f"| {record['id']} | - | {record['judge']} | {record.get('error_msg') or '-'}"
                    f" | {len(record['expected_chunks'])} 片 | 无（题级异常，无轮级明细） |"
                )
                continue
            hits = "、".join(record["recall_hits"]) or "无"
            lines.append(
                f"| {record['id']} | {len(turns)} | {record['judge']} | {record['failure_mode'] or '-'}"
                f" | {len(record['expected_chunks'])} 片 | {hits} |"
            )
        lines.append("")
        lines.append("### query 拼接行为观察（每轮 query = previous_human_message + 当前输入，§2.7）")
        changed = 0
        observed = 0
        for record in records:
            turns = record.get("turns")
            if turns is None:
                lines.append(f"- **{record['id']}**：题级异常（invoke_exception），无轮级观察数据")
                continue
            observed += 1
            lines.append(f"- **{record['id']}**")
            for turn in turns:
                query_display = turn["retrieval_query"].replace("\n", " / ")
                lines.append(
                    f"  - 轮 {turn['turn_idx']}：query=`{query_display}` → 注入 {len(turn['injected_chunks'])} 片"
                    f"[{'、'.join(turn['injected_chunks']) or '无'}]，工具调用 {len(turn['sql_calls'])} 次"
                )
            if set(turns[-1]["injected_chunks"]) != set(turns[0]["injected_chunks"]):
                changed += 1
        if observed:
            lines.append(
                f"- 观察：{changed}/{observed} 个 case 的最后一轮注入集合与首轮不同"
                "（query 拼接改变了检索结果——多轮检索歧义量的量化，W2 §3.6 已知妥协的实测数据）"
            )
    else:
        lines.append("（本次未运行多轮评测；多轮题用 `--multiturn` 跑 eval_v1_multiturn.jsonl，独立报告节 §1.7/§2.7。）")
    lines.append("")

    # —— 逐题结果表（md 汇总含逐题结果，明细 JSON 可不单独提交，§7-3）——
    lines.append("## 逐题结果")
    lines.append("| run | id | 判定 | pass_via | failure_mode | sql_source | 行数(金/模) | 延迟s | tokens(total) |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for record in records:
        lines.append(
            f"| r{record['run_idx']} | {record['id']} | {record['judge']} | {record.get('pass_via') or '-'}"
            f" | {record['failure_mode'] or record.get('error_msg') or '-'}"
            f" | {record['sql_source'] or '-'} | {record['gold_row_count'] if record['gold_row_count'] is not None else '-'}/"
            f"{record['model_row_count'] if record['model_row_count'] is not None else '-'}"
            f" | {record['latency_s'] if record['latency_s'] is not None else '-'} | {record['tokens']['total']} |"
        )
    lines.append("")

    # —— 自举语料命中（W5 §4.7-3，additive）——
    # 仅当非空才输出：W3 时代（语料 001~011）的报告保持逐字节可复现
    absorbed = absorbed_chunk_hits(records)
    if absorbed:
        lines.append("## 自举语料命中")
        case_text = "、".join(absorbed)
        chunks = sorted({chunk for chunk_list in absorbed.values() for chunk in chunk_list})
        lines.append(
            f"- 命中自举语料的题：{case_text}（注入 qa_012+ 片：{'、'.join(chunks)}）"
        )
        lines.append(
            "- 口径：injected_chunks ∩ {qa_pair:qa_NNN | NNN ≥ 012}（012 起为用户点赞自举收录，W5 §4.7-3）"
        )
        lines.append("")

    lines.append(_DECLARATIONS)
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# 命令行入口
# ---------------------------------------------------------------------------


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m eval.run_eval",
        description="W3 评测 runner：与线上 main.py 同构地逐题运行 Agent，重放金标与模型 SQL 后判定，产出报告与明细。",
    )
    parser.add_argument("--dataset", default=None, help="评测集 JSONL 路径（默认 eval/dataset/eval_v1.jsonl；--multiturn 时默认 eval_v1_multiturn.jsonl）")
    parser.add_argument("--top-n", dest="top_n", type=int, default=8, help="检索注入条数，直接作为 retriever.search(top_n=...) 传入（默认 8；对照行用 0）")
    parser.add_argument("--runs", type=int, default=1, help="每题运行次数（主行基线用 3，§3.2）")
    parser.add_argument("--tag", required=True, help="本次运行标签（报告/明细文件名与对照表用，如 baseline_full / smoke）")
    parser.add_argument("--limit", type=int, default=None, help="只跑前 N 条（冒烟用，如 --limit 2）")
    parser.add_argument("--rerun", default=None, help="复跑指定题，逗号分隔 id（如 EV-017,EV-029）")
    parser.add_argument("--multiturn", action="store_true", help="加载多轮评测集（eval_v1_multiturn.jsonl，§2.7：逐轮 invoke 共享 thread，判定只看最后一轮）")
    parser.add_argument("--compare", default=None, help="对照行的 detail JSON 路径，报告据此生成对照行对比表（§3.3）")
    # —— W6 消融入口（设计 §3.4，全部默认关闭；无任一开关时组装与线上逐字节等价）——
    parser.add_argument("--rrf-k", dest="rrf_k", type=int, default=None, help="RRF 融合常数 k（默认 60=线上默认；消融 E 行用 10。传入后按该 k 另建检索器实例并缓存）")
    parser.add_argument("--no-bm25", dest="no_bm25", action="store_true", help="关 BM25 路=纯向量检索（消融 B 行：精确 token 命中的损失）")
    parser.add_argument("--no-dense", dest="no_dense", action="store_true", help="关向量路=纯 BM25 检索（消融 C 行：语义改写鲁棒性的损失）")
    parser.add_argument("--no-fewshot", dest="no_fewshot", action="store_true", help="过滤 qa_pair 片（doc_types=schema+glossary，消融 G 行：去 few-shot 自举）")
    parser.add_argument("--rerank", action="store_true", help=f"启用 rerank（SiliconFlow {RERANK_MODEL}，候选池 {RERANK_POOL}=2×top_n 起点，消融 F 行；需 .env 配 SILICONFLOW_API_KEY，仅评测侧）")
    parser.add_argument("--quota", action="store_true", help="启用 doc_type 覆盖配额（W7 设计 §1.3，FQ 行：--rerank --quota）——重排允许改变顺序、不允许降低类型覆盖（相对 RRF top-8 基线锚）；需与 --rerank 同用")
    args = parser.parse_args()
    # W7 §1.5 启动期互斥校验：few-shot（qa_pair 片）被 --no-fewshot 过滤后，
    # quota 还要为 qa_pair 保底覆盖是语义矛盾——直接报错退出，不跑一半才发现。
    if args.quota and args.no_fewshot:
        parser.error(
            "--quota 与 --no-fewshot 不能同时使用：quota 会为 qa_pair（few-shot 示例片）"
            "保底类型覆盖，而 --no-fewshot 已把 qa_pair 片全部过滤——"
            "示例片被过滤了还保 qa 覆盖是语义矛盾（W7 设计 §1.5），请只保留其一"
        )
    return args


def _local_now() -> datetime:
    """本地时区的当前墙钟（报告头与文件名时间戳）：先取 UTC 再转本地，
    得到带时区的 aware datetime（报告时间按本机时区显示）。"""
    return datetime.now(tz=timezone.utc).astimezone()


def main() -> None:
    args = _parse_args()
    if args.dataset:
        dataset_path = Path(args.dataset)
    elif args.multiturn:
        dataset_path = MULTITURN_DATASET
    else:
        dataset_path = DEFAULT_DATASET

    preflight()

    # W6 消融入口（§3.4）：启动期构建 opts（--rerank 缺 key 在这里失败，不跑一半才炸）
    try:
        retrieval_opts = _build_retrieval_opts(args)
    except ValueError as exc:  # SiliconFlowReranker 构造失败的中文指引
        print(str(exc))
        sys.exit(1)
    retrieval_cfg = effective_retrieval_config(args)

    retriever = get_retriever()
    # §8-7：加载时用真实 chunk id 集合复检 expected_chunks（T1 已核对，运行时再拦一道）
    valid_chunk_ids = set(retriever.ids)
    if args.multiturn:
        cases = load_multiturn_dataset(dataset_path, valid_chunk_ids=valid_chunk_ids)
    else:
        cases = load_dataset(dataset_path, valid_chunk_ids=valid_chunk_ids)

    if args.rerun:
        wanted = [item.strip() for item in args.rerun.split(",") if item.strip()]
        known = {case["id"] for case in cases}
        unknown = [item for item in wanted if item not in known]
        if unknown:
            print(f"--rerun 的题号不在数据集里：{', '.join(unknown)}（数据集共 {len(known)} 条）")
            sys.exit(1)
        cases = [case for case in cases if case["id"] in set(wanted)]
    if args.limit is not None:
        cases = cases[: args.limit]

    started_at = _local_now().strftime("%Y-%m-%d %H:%M:%S")
    timestamp = _local_now().strftime("%Y%m%d_%H%M%S")
    mode_label = "多轮" if args.multiturn else "单轮"
    print(
        f"评测开始：{mode_label} ｜ 数据集 {dataset_path.name}（{len(cases)} 条）"
        f" ｜ top_n={args.top_n} ｜ runs={args.runs} ｜ tag={args.tag}"
    )
    if retrieval_opts is not None:
        print(f"消融参数生效：{_describe_retrieval(retrieval_cfg)}（W6 §3.4）")

    replayer = Replayer()
    records: list[dict] = []
    for run_idx in range(1, args.runs + 1):
        for case in cases:
            try:
                if args.multiturn:
                    record = evaluate_multiturn_case(case, run_idx, args.top_n, replayer, retrieval_opts)
                else:
                    record = evaluate_single_case(case, run_idx, args.top_n, replayer, retrieval_opts)
            except Exception as exc:  # noqa: BLE001 - 题级 try/except 不中断整场（§6 风险 4）
                record = _invoke_exception_record(case, run_idx, exc)
            records.append(record)
            mode_text = record["failure_mode"] or record.get("error_msg") or "-"
            latency_text = record["latency_s"] if record["latency_s"] is not None else "-"
            print(
                f"[r{run_idx}][{record['id']}] thread=eval-{record['id']}-r{run_idx}"
                f" 注入{len(record['injected_chunks'])}片 → {record['judge']}（{mode_text}）"
                f" {latency_text}s tokens={record['tokens']['total']}",
                flush=True,
            )

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    detail_path = REPORTS_DIR / f"detail_{args.tag}_{timestamp}.json"
    report_path = REPORTS_DIR / f"report_{args.tag}_{timestamp}.md"

    corpus_counts = corpus_type_counts(retriever)
    detail = {
        "tag": args.tag,
        "started_at": started_at,
        "config": _build_detail_config(args, dataset_path, started_at, retrieval_cfg, corpus_counts),
        "cases": records,
    }
    detail_path.write_text(json.dumps(detail, ensure_ascii=False, indent=2), encoding="utf-8")
    report_path.write_text(
        _render_report(args, dataset_path, records, started_at, corpus_counts=corpus_counts),
        encoding="utf-8",
    )

    _, failed, errored, acc = _split_accuracy(records)
    sql_pass, text_pass = _pass_via_counts(records)
    print(
        f"完成：SQL 级执行准确率 {_fmt_pct(_sql_acc(records))}（sql {sql_pass} / fail {failed} / error {errored}）"
        f" ｜ 答案级回答准确率 {_fmt_pct(acc)}（+ 文本判过 {text_pass}，§2.4 v2）"
    )
    print(f"报告：{report_path}")
    print(f"明细：{detail_path}")


if __name__ == "__main__":
    main()
