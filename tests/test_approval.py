"""W5 HITL 待审批注册表的全逻辑测试（W5 设计 §3.5）。

范围：agent/core/approval.py 的进程内单例——register/pop/find_by_thread/decisions_for：
- register：登记返回 approvalId；同 thread 重复登记抛 PendingApprovalExists；
  不同 thread 互不影响；
- pop：原子取出（锁内 pop）——双击/并发第二次必 NotFound；NotFound/Expired 的
  code 与 http_status 映射（409/410，server 层直接透传）；
- TTL：过期项在 pop → Expired、find_by_thread/register → 惰性清理（不真睡，
  monkeypatch PENDING_TTL_SECONDS）；
- find_by_thread：命中/未命中/过期清理；
- decisions_for：approve/reject 载荷契约（resume 必须是带 decisions 键的 dict，
  §3.3-3 实测）与非法值。

零环境依赖：不连 MySQL/Chroma、不需要 key、不起 FastAPI。
"""

import threading

import pytest

from agent.core import approval


@pytest.fixture(autouse=True)
def _clean_registry():
    """注册表是模块级单例：每个用例前后都清空，防跨用例泄漏。"""
    approval._reset_for_tests()
    yield
    approval._reset_for_tests()


def test_register_returns_unique_id_and_entry_is_findable():
    aid1 = approval.register("u1:t1", "SELECT 1", "问一", "上下文块")
    aid2 = approval.register("u1:t2", "SELECT 2", "问二", "上下文块")
    assert aid1 != aid2
    entry = approval.pop(aid1)
    assert entry["approvalId"] == aid1
    assert entry["thread_key"] == "u1:t1"
    assert entry["sql"] == "SELECT 1"
    assert entry["question"] == "问一"
    assert entry["context_block"] == "上下文块"
    assert isinstance(entry["created_at"], float)


def test_register_duplicate_thread_raises_pending_approval_exists():
    approval.register("u1:t1", "SELECT 1", "问", "ctx")
    with pytest.raises(approval.PendingApprovalExists) as ei:
        approval.register("u1:t1", "SELECT 2", "问", "ctx")
    assert ei.value.code == "pending_approval_exists"
    assert ei.value.http_status == 409


def test_register_after_pop_allows_new_pending_for_same_thread():
    aid = approval.register("u1:t1", "SELECT 1", "问", "ctx")
    approval.pop(aid)
    aid2 = approval.register("u1:t1", "SELECT 2", "问", "ctx")  # 消费后可再登记
    assert aid2 != aid


def test_pop_twice_second_is_not_found():
    aid = approval.register("u1:t1", "SELECT 1", "问", "ctx")
    assert approval.pop(aid)["approvalId"] == aid  # 第一次取出成功
    with pytest.raises(approval.ApprovalNotFound) as ei:
        approval.pop(aid)  # 双击批准的第二击
    assert ei.value.code == "approval_not_found"
    assert ei.value.http_status == 409


def test_pop_unknown_id_is_not_found():
    with pytest.raises(approval.ApprovalNotFound):
        approval.pop("nonexistent-id")


def test_pop_expired_raises_expired_and_clears_index(monkeypatch):
    monkeypatch.setattr(approval, "PENDING_TTL_SECONDS", 0.0)
    aid = approval.register("u1:t1", "SELECT 1", "问", "ctx")
    with pytest.raises(approval.ApprovalExpired) as ei:
        approval.pop(aid)
    assert ei.value.code == "approval_expired"
    assert ei.value.http_status == 410
    # 过期取出后反查索引同样被清：新登记不再冲突
    approval.register("u1:t1", "SELECT 2", "问", "ctx")


def test_find_by_thread_hit_and_miss():
    assert approval.find_by_thread("u1:t1") is None  # 未命中
    aid = approval.register("u1:t1", "SELECT 1", "问", "ctx")
    entry = approval.find_by_thread("u1:t1")
    assert entry is not None and entry["approvalId"] == aid
    assert approval.find_by_thread("u1:t2") is None  # 其它 thread 不受影响


def test_find_by_thread_lazily_purges_expired(monkeypatch):
    monkeypatch.setattr(approval, "PENDING_TTL_SECONDS", 0.0)
    approval.register("u1:t1", "SELECT 1", "问", "ctx")
    assert approval.find_by_thread("u1:t1") is None  # 过期 → None（不阻塞新提问，§3.10）
    monkeypatch.setattr(approval, "PENDING_TTL_SECONDS", 300.0)  # 恢复 TTL 再登记：新项有效
    aid = approval.register("u1:t1", "SELECT 2", "问", "ctx")  # 清理后可重新登记
    assert approval.find_by_thread("u1:t1")["approvalId"] == aid


def test_register_purges_expired_then_accepts_same_thread(monkeypatch):
    """register 通道的惰性清理：过期项不再触发 PendingApprovalExists。"""
    monkeypatch.setattr(approval, "PENDING_TTL_SECONDS", 0.0)
    approval.register("u1:t1", "SELECT 1", "问", "ctx")
    aid = approval.register("u1:t1", "SELECT 2", "问", "ctx")
    assert aid


def test_concurrent_pop_single_winner():
    """锁内原子取出：并发 pop 同一 id 恰有一个成功（双击/并发幂等保护）。"""
    aid = approval.register("u1:t1", "SELECT 1", "问", "ctx")
    results: list[str] = []
    errors: list[Exception] = []
    barrier = threading.Barrier(4)

    def worker():
        barrier.wait()
        try:
            approval.pop(aid)
            results.append("ok")
        except approval.ApprovalError as e:
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results == ["ok"]
    assert len(errors) == 3
    assert all(isinstance(e, approval.ApprovalNotFound) for e in errors)


def test_decisions_for_approve_and_reject():
    approve = approval.decisions_for("approve")
    assert approve == [{"type": "approve"}]
    reject = approval.decisions_for("reject")
    assert len(reject) == 1
    assert reject[0]["type"] == "reject"
    assert reject[0]["message"] == approval.REJECT_MESSAGE
    # resume 载荷契约（§3.3-3 实测）：外层必须是带 decisions 键的 dict，元素 type 必填
    assert all(isinstance(d, dict) and d.get("type") for d in approve + reject)


def test_decisions_for_invalid_raises():
    with pytest.raises(ValueError):
        approval.decisions_for("edit")  # W5 不给 edit 决策（§3.3）
    with pytest.raises(ValueError):
        approval.decisions_for("")


def test_decisions_for_aligns_with_action_count():
    """§8-⑰ 实测：decisions 数必须与 action_requests 等长（不等长 resume 抛 ValueError）。"""
    # 单 action（常态）
    assert approval.decisions_for("approve", 1) == [{"type": "approve"}]
    assert approval.decisions_for("reject", 1) == [{"type": "reject", "message": approval.REJECT_MESSAGE}]
    # 多 action：reject → 整批拒；approve → 批第 1 条 + 其余"未过目"安全兜底拒绝
    multi_reject = approval.decisions_for("reject", 3)
    assert len(multi_reject) == 3 and all(d["type"] == "reject" for d in multi_reject)
    multi_approve = approval.decisions_for("approve", 2)
    assert len(multi_approve) == 2
    assert multi_approve[0] == {"type": "approve"}
    assert multi_approve[1]["type"] == "reject"
    assert multi_approve[1]["message"] == approval.REJECT_UNCONFIRMED_MESSAGE


def test_register_records_action_count():
    aid = approval.register("u1:t1", "SELECT 1", "问", "ctx", action_count=2)
    entry = approval.pop(aid)
    assert entry["action_count"] == 2
    assert approval.register("u1:t2", "SELECT 1", "问", "ctx") is not None  # 缺省 = 1
