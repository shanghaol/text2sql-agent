"""rerank 的 doc_type 覆盖配额（W7 设计 §1.3）——纯函数模块，零第三方依赖。

语义（设计 §1.3 一句话）：rerank 允许重排**顺序**，不允许降低**类型覆盖**——
相对基线（RRF top-8 出现过的 doc_type 集合）而言。这是对 rerank 负结果的
最小干预修法：排名空间（rank-space）规则，对分数尺度不敏感、可单测、可解释。

两段式规则（设计 §1.3 伪代码逐行照实现）：

    输入：rerank_pool 池（RRF 序）、池内片的 rerank 全序（rerank 分写在
          RetrievedChunk.rrf_score 字段位置）、覆盖集 C、top_n
    1. 覆盖集 C = { doc_type(t) | 片 t ∈ RRF top-8 }   # 基线行为锚，coverage_types()
    2. 预留段：对每个 c ∈ C 且池内存在 c 类片：取 c 类 rerank 分最高的 1 片强制入选
       （池内无 c 类片则跳过并记日志——配额救不了池外的片，这是诚实边界）
    3. 填充段：剩余槽位按 rerank 全序从池内其余片补满（跳过已入选片）
    4. 输出按 rerank 分全局降序排列（保持确定性；rerank 分仍写在 rrf_score 字段位置）

覆盖集锚 RRF top-8 而不是池 16（设计 §1.3）：最小干预语义——"只修复 rerank 造成的
回归，不新增基线没有的注入策略"；floor=1 对每个覆盖类型生效，50 题期望分布下
floor 总和 2 ≤ top_n 8、schema 槽位下界 6 ≥ 单题最大 schema 期望 5，配额与需求
无冲突（该数学验证的断言在 tests/test_quota.py，直接读 eval_v1.jsonl 统计）。

确定性：全程只依赖 RRF 序 + rerank 序 + 池内 index 决胜（同 rerank 分按池内
index 升序——与 rrf_fuse/rerank.py 的决胜哲学一致），无随机、无分数阈值——
同样输入必然同样输出，离线扫描与线上行为可逐位对账。覆盖集是 set，其迭代序
跨进程不稳定（PYTHONHASHSEED），凡遍历一律 sorted() 后进行。

本模块对 RetrievedChunk 只做类型标注（TYPE_CHECKING），不 import
hybrid_retriever 本体（它拖 jieba/rank_bm25）——单测裸机可跑（duck-type 假片
即可），quota_sweep.py（W7-T2 离线标定）与 hybrid_retriever.search() 共用同一
实现，杜绝"标定规则 ≠ 线上规则"的漂移（设计 §1.5）。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # 仅为类型标注，不引入运行时依赖
    from agent.rag.hybrid_retriever import RetrievedChunk

logger = logging.getLogger(__name__)


def coverage_types(rrf_top_ids: list[str], metas: dict[str, dict]) -> set[str]:
    """从 RRF top-8 计算覆盖集 C（设计 §1.3 第 1 步，基线行为锚）。

    参数：
        rrf_top_ids: RRF 融合序的前 top_n 个 chunk id（基线在无 rerank 时
                     会注入的片——配额的"基线行为锚"）；
        metas: chunk id → 该片 metadata（含 doc_type 字段）。

    id 不在 metas 里、或 metadata 无 doc_type / doc_type 为空的片不计入
    （与检索层 doc_type 缺省 "" 的语义一致：空类型无法构成可保底的目标）。
    """
    coverage: set[str] = set()
    for doc_id in rrf_top_ids:
        meta = metas.get(doc_id) or {}
        doc_type = meta.get("doc_type") or ""
        if doc_type:
            coverage.add(doc_type)
    return coverage


def apply_coverage_quota(
    reranked: list[RetrievedChunk],
    rrf_order: list[str],
    coverage: set[str],
    top_n: int,
) -> list[RetrievedChunk]:
    """对池内片的 rerank 全序执行两段式覆盖配额选片（设计 §1.3 第 2-4 步）。

    参数：
        reranked: 池内全部片的 rerank 全序，rerank 分写在 rrf_score 字段位置
                  （调用方负责把 rerank 分替换进去；本函数以
                  (-rerank 分, 池内 index) 再排一次，不依赖调用方是否已决胜）；
        rrf_order: 池内片的 RRF 全序（rrf_fuse 产物，池 = RRF 前 rerank_pool 名）
                   ——提供池内 index（决胜键）与"池内"成员资格的权威定义；
        coverage: 覆盖集 C（coverage_types 的产物）；
        top_n: 最终注入条数。

    返回：≤top_n 片，按 rerank 分全局降序（同分按池内 index 升序）。

    契约与失败语义：reranked 里每个片的 id 必须在 rrf_order 中（池的成员资格
    由 rrf_order 定义，不在其中说明调用方组装错了——直接 KeyError 大声失败，
    与本仓库"不猜不兜底"惯例一致）；rerank 分相同时按池内 index 升序决胜。
    """
    if top_n <= 0 or not reranked:
        return []
    pool_index = {doc_id: i for i, doc_id in enumerate(rrf_order)}
    # rerank 全序：rerank 分降序，同分按池内 index 升序（设计 §1.3 决胜规则）
    ordered = sorted(reranked, key=lambda chunk: (-chunk.rrf_score, pool_index[chunk.id]))

    selected: list[RetrievedChunk] = []
    selected_ids: set[str] = set()

    # —— 预留段（第 2 步）：每个覆盖类型强制保底 1 片（c 类 rerank 分最高者）——
    for doc_type in sorted(coverage):  # set 迭代序跨进程不稳定，sorted() 保证确定性
        best = next(
            (chunk for chunk in ordered if chunk.doc_type == doc_type and chunk.id not in selected_ids),
            None,
        )
        if best is None:
            # 池内无该类型片：配额救不了池外的片（扩池/改切分是另案），诚实边界
            logger.warning(
                "配额预留跳过：类型 %s 在候选池 %d 片内不存在（池外片救不回，设计 §1.3 诚实边界）",
                doc_type,
                len(ordered),
            )
            continue
        selected.append(best)
        selected_ids.add(best.id)

    # —— 填充段（第 3 步）：剩余槽位按 rerank 全序补满，跳过已入选片 ——
    for chunk in ordered:
        if len(selected) >= top_n:
            break
        if chunk.id in selected_ids:
            continue
        selected.append(chunk)
        selected_ids.add(chunk.id)

    # —— 输出（第 4 步）：按 rerank 分全局降序（预留片可能不属于 top_n 分位，
    #     预留段的入选序也不保证全局有序，统一重排恢复"按分注入"的形态）——
    return sorted(selected, key=lambda chunk: (-chunk.rrf_score, pool_index[chunk.id]))[:top_n]


def apply_cap_quota(
    reranked: list[RetrievedChunk],
    rrf_order: list[str],
    top_n: int,
    caps: dict[str, int] | None = None,
) -> list[RetrievedChunk]:
    """cap_only 选片（W7 离线标定的胜者变体）：按 rerank 全序遍历，类型配额封顶。

    与 apply_coverage_quota 的区别：无覆盖集保底（不做预留段），只对类型设上限——
    schema 过量时被 cap 挤出，非 schema（glossary/qa_pair）获得实际配额。
    语义来源：W6 负结果归因实验——判别"schema 挤出 glossary"与"示例稀释"两通道
    谁是 SQL 伤害主因（experiment-log W7 预注册；离线扫描 86.8%，J1+J2 双过）。

    参数：
        reranked: 池内全部片的 rerank 全序，rerank 分写在 rrf_score 字段位置
                  （与 apply_coverage_quota 同约定：本函数以 (-rerank 分, 池内 index)
                  再排一次，不依赖调用方是否已决胜）；
        rrf_order: 池内片的 RRF 全序（提供池内 index 决胜键）；
        top_n: 最终注入条数（遍历在此处停止——被封顶类型跳过不占槽）；
        caps: 类型 → 上限。默认 {"schema": 6, "glossary": 2, "qa_pair": 2}
              （离线标定的 V4 精确语义：单题 schema 期望 ≤5、glossary/qa 期望各 ≤1，
              cap 6/2/2 不误伤合法需求；运行时必须与离线扫描逐位一致——
              2026-11-03 一致性核对曾因"非 schema 不封顶"漂移被抓，教训在案）。

    返回：≤top_n 片，按 rerank 分全局降序（同分按池内 index 升序）。
    确定性：只依赖 rerank 全序 + 池内 index，无随机、无分数阈值。
    """
    if top_n <= 0 or not reranked:
        return []
    caps = caps or {"schema": 6, "glossary": 2, "qa_pair": 2}
    pool_index = {doc_id: i for i, doc_id in enumerate(rrf_order)}
    ordered = sorted(reranked, key=lambda chunk: (-chunk.rrf_score, pool_index[chunk.id]))

    selected: list[RetrievedChunk] = []
    counts: dict[str, int] = {}
    for chunk in ordered:
        if len(selected) >= top_n:
            break
        cap = caps.get(chunk.doc_type)
        used = counts.get(chunk.doc_type, 0)
        if cap is not None and used >= cap:
            continue
        selected.append(chunk)
        counts[chunk.doc_type] = used + 1
    return selected
