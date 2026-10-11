"""HITL 待审批注册表（W5 设计 §3.5）：进程内 dict + TTL 惰性清理，零外部依赖。

单 worker 前提（与 Chroma/fastembed/checkpointer/LLM 缓存一致，部署锁定 workers=1）：
- Python 重启丢 pending → 前端批准时 409 approval_not_found → 引导重新提问；
  与 InMemorySaver 的"重启即失"同等级已知限制，不为演示项目引入 Redis（§3.5）；
- 同 thread 已有待审批时 register 抛 PendingApprovalExists——正常流程里新提问已在
  server 层被 find_by_thread 409 短路，这里是并发双提问的防御兜底（§3.10）。

TTL=5 分钟（PENDING_TTL_SECONDS）：与"用户看完一条 SQL 做个决定"的节奏匹配；
过期项在 register/pop/find_by_thread 时惰性清理，不引入定时线程。

entry 结构（dict，与设计 §3.5 逐字段对应）：
    {approvalId, thread_key, sql, question, context_block, created_at}
context_block 是停车轮 format_context_block 的产物——resume 重建 agent 时原样复用，
保证系统提示词与停车轮**逐字节一致**（§3.5"恢复轮的组装口径"）。
"""

import threading
import time
import uuid as uuid_lib

# 过期时限（秒）。模块属性而非函数默认参数：pytest 用 monkeypatch 改它即可测过期，不用真睡。
PENDING_TTL_SECONDS = 300.0

# reject 决策注入给模型的中文引导（§3.4）。实测（2026-10-11）：工具收到
# ToolMessage "User rejected the tool call for `execute_readonly_sql` with reason: <本文>"
# 后，模型用中文询问用户意图，不会擅自改写 SQL 重试（那条路径同样要审批）。
REJECT_MESSAGE = (
    "用户拒绝了这条 SQL。请不要执行或改写它，直接询问用户希望如何调整"
    "（时间范围、指标口径、过滤条件）。"
)

# 多 action 轮里"未被用户过目"的 SQL 的拒绝引导（§8-⑰：安全兜底，绝不执行未确认 SQL）。
REJECT_UNCONFIRMED_MESSAGE = (
    "这条 SQL 没有经过用户确认（用户本轮只确认了第一条）。请不要执行它；"
    "如确需查询，请重新生成 SQL 后等待用户确认，或直接询问用户是否需要。"
)

_pending: dict[str, dict] = {}  # approvalId -> entry
_by_thread: dict[str, str] = {}  # thread_key -> approvalId（反查"该会话是否已有待审批"）
_lock = threading.Lock()


class ApprovalError(Exception):
    """审批注册表业务错误基类：code 对外业务码，http_status 是 server 层映射的状态码。"""

    code = "approval_not_found"
    http_status = 409


class PendingApprovalExists(ApprovalError):
    """同 thread 已有未决审批时又登记（server 层新提问 409 短路后的防御兜底）。"""

    code = "pending_approval_exists"
    http_status = 409


class ApprovalNotFound(ApprovalError):
    """approvalId 不存在（已消费 / 服务重启 / 伪造 id）。"""

    code = "approval_not_found"
    http_status = 409


class ApprovalExpired(ApprovalError):
    """approvalId 存在但超过 TTL（前端据此把审批卡置灰"已过期"）。"""

    code = "approval_expired"
    http_status = 410


def register(thread_key: str, sql: str, question: str, context_block: str, action_count: int = 1) -> str:
    """登记一条待审批 SQL，返回 approvalId。

    同 thread 已有未过期待审批 → PendingApprovalExists。锁内先惰性清理过期项再查重，
    "TTL 过期后重新提问"因此不受旧项阻塞（§3.10）。
    action_count：本轮 interrupt 的 action_requests 数（§8-⑰ 多 SQL——resume 时 decisions
    须与之等长对齐；当前实现登记数仅供 decisions_for 做安全兜底填充）。
    """
    now = time.perf_counter()  # 单调钟：TTL 只比 elapsed，不受系统对时/回拨影响
    with _lock:
        _purge_expired_locked(now)
        existing = _by_thread.get(thread_key)
        if existing is not None:
            raise PendingApprovalExists(f"thread 已有待审批 approvalId={existing}")
        approval_id = uuid_lib.uuid4().hex
        _pending[approval_id] = {
            "approvalId": approval_id,
            "thread_key": thread_key,
            "sql": sql,
            "question": question,
            "context_block": context_block,
            "action_count": max(int(action_count), 1),
            "created_at": now,  # perf_counter 单调钟时间戳（仅供 TTL 计算，非墙钟）
        }
        _by_thread[thread_key] = approval_id
        return approval_id


def pop(approval_id: str) -> dict:
    """原子取出待审批项：锁内 pop，双击/并发第二次必 NotFound（§3.10 幂等保护）。

    不存在 → ApprovalNotFound；存在但超过 TTL → ApprovalExpired（同时清掉反查索引）。
    """
    now = time.perf_counter()
    with _lock:
        entry = _pending.pop(approval_id, None)
        if entry is None:
            raise ApprovalNotFound(f"approvalId 不存在或已被消费: {approval_id}")
        if _by_thread.get(entry["thread_key"]) == approval_id:
            del _by_thread[entry["thread_key"]]
        if now - entry["created_at"] > PENDING_TTL_SECONDS:
            raise ApprovalExpired(f"approvalId 已超过 TTL {PENDING_TTL_SECONDS}s: {approval_id}")
        return entry


def find_by_thread(thread_key: str) -> dict | None:
    """该 thread 的未决审批项（新提问到达时的 409 检查）；无或已过期 → None。

    过期项在此惰性清理（§3.5）——被过期项占住的 thread 不阻塞新提问。
    """
    now = time.perf_counter()
    with _lock:
        _purge_expired_locked(now)
        approval_id = _by_thread.get(thread_key)
        if approval_id is None:
            return None
        return _pending.get(approval_id)


def decisions_for(decision: str, action_count: int = 1) -> list[dict]:
    """业务决策 → resume 载荷的 decisions 列表（§3.3-3：外层必须是带 decisions 键的 dict）。

    decisions 长度与 action_requests 等长（实测：不等长 resume 直接 ValueError，§8-⑰）。
    多 SQL 轮的兜底口径（安全不变量：**绝不执行未经用户过目的 SQL**）：
    - reject → 整批拒绝（用户拒绝了这轮 SQL）；
    - approve → 批准第一条（前端当前只展示/确认第一条，逐条批准 UI 留 §8-⑰ 收尾），
      其余以 reject 兜底——模型收到回执后会重新发起新 SQL（触发下一轮 interrupt → 新审批卡）
      或如实询问用户。
    """
    count = max(int(action_count), 1)
    if decision == "approve":
        rest = [{"type": "reject", "message": REJECT_UNCONFIRMED_MESSAGE} for _ in range(count - 1)]
        return [{"type": "approve"}, *rest]
    if decision == "reject":
        return [{"type": "reject", "message": REJECT_MESSAGE} for _ in range(count)]
    raise ValueError(f"decision 仅支持 approve/reject，收到: {decision!r}")


def _purge_expired_locked(now: float) -> None:
    """惰性清理过期项（调用方必须已持有 _lock）。"""
    expired = [aid for aid, entry in _pending.items() if now - entry["created_at"] > PENDING_TTL_SECONDS]
    for aid in expired:
        entry = _pending.pop(aid)
        if _by_thread.get(entry["thread_key"]) == aid:
            del _by_thread[entry["thread_key"]]


def _reset_for_tests() -> None:
    """仅测试使用：清空注册表（进程内单例状态不得跨用例泄漏）。"""
    with _lock:
        _pending.clear()
        _by_thread.clear()
