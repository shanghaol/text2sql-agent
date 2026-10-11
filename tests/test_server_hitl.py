"""W5 HITL 的 server 层测试（W5 设计 §3.4/§3.5）：interrupt 探测、pending_approval 事件、
审批恢复端点与 409/410/422 路径、ToolMessage 泄漏修复。

默认套件零环境依赖：不连 MySQL/Chroma、不需要 key（monkeypatch DEEPSEEK_API_KEY）、
不发起网络（agent 用假替身）。导入 agent.server 不触发 preflight（挂 lifespan）。

关键实测口径（tests/manual/w5_hitl_rebuild_resume_probe.py，2026-10-11）：
- 中断时 stream_mode="messages" 的流自然结束（无 done），载荷在
  get_state().tasks[0].interrupts[0].value 的 action_requests[0].args["sql"]；
- resume 必须是 Command(resume={"decisions": [...]})（带 decisions 键的 dict）；
- messages 流里工具节点写入的 ToolMessage 也会出现（正常轮与 resume 轮都是）——
  mapper 必须过滤，否则工具结果/英文拒绝回执会泄漏进 delta（W4 潜伏缺陷）。
"""

import json
import uuid
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessageChunk, ToolMessage
from langgraph.types import Command

from agent import server
from agent.core import approval

# ---------------------------------------------------------------------------
# 测试替身
# ---------------------------------------------------------------------------


def _text_chunk(text: str) -> AIMessageChunk:
    return AIMessageChunk(content=text)


def _usage_chunk(text: str, input_tokens: int, output_tokens: int) -> AIMessageChunk:
    return AIMessageChunk(
        content=text,
        usage_metadata={"input_tokens": input_tokens, "output_tokens": output_tokens, "total_tokens": input_tokens + output_tokens},
    )


def _interrupt_value(sql: str = "SELECT 1", tool: str = "execute_readonly_sql") -> dict:
    return {
        "action_requests": [{"name": tool, "args": {"sql": sql}, "description": f"即将执行 SQL：{sql}"}],
        "review_configs": [{"action_name": tool, "allowed_decisions": ["approve", "reject"]}],
    }


class InterruptFakeAgent:
    """stream 正常吐完 chunks 后自然结束（HITL 停车语义：无异常、无 done）；
    get_state 返回带 interrupt 的假状态。记录 stream 收到的 input 供断言。"""

    def __init__(self, chunks=(), interrupt: dict | None = None):
        self._chunks = list(chunks)
        self._interrupt = interrupt
        self.stream_inputs: list = []

    def stream(self, inputs, config=None, stream_mode=None):
        self.stream_inputs.append(inputs)
        for chunk in self._chunks:
            yield chunk, {"langgraph_node": "model"}

    def get_state(self, config):
        if self._interrupt is None:
            return SimpleNamespace(tasks=())
        return SimpleNamespace(tasks=[SimpleNamespace(interrupts=[SimpleNamespace(value=self._interrupt)])])


class PlainFakeAgent:
    """正常收尾（无 interrupt）：get_state 无 tasks。"""

    def __init__(self, chunks=()):
        self._chunks = list(chunks)

    def stream(self, inputs, config=None, stream_mode=None):
        for chunk in self._chunks:
            yield chunk, {"langgraph_node": "model"}

    def get_state(self, config):
        return SimpleNamespace(tasks=())


class FakePt:
    """PreparedTurn 的最小替身（生成器读 agent / config / injected_chunks）。"""

    def __init__(self, agent, thread_id="u42:8f3a", injected_chunks=()):
        self.agent = agent
        self.config = {"configurable": {"thread_id": thread_id}}
        self.injected_chunks = list(injected_chunks)


def _collect(generator) -> list[tuple[str, dict]]:
    frames = []
    for frame in generator:
        lines = frame.strip().split("\n")
        frames.append((lines[0].removeprefix("event: "), json.loads(lines[1].removeprefix("data: "))))
    return frames


@pytest.fixture(autouse=True)
def _clean_registry_and_globals():
    approval._reset_for_tests()
    yield
    approval._reset_for_tests()
    from agent import config as cfg

    server.INTERNAL_TOKEN = cfg.INTERNAL_TOKEN
    server.DEEPSEEK_API_KEY = cfg.DEEPSEEK_API_KEY


# ---------------------------------------------------------------------------
# StreamEventMapper：ToolMessage 过滤（W4 潜伏缺陷修复的回归锁）
# ---------------------------------------------------------------------------


def test_mapper_skips_tool_message_content():
    """工具节点的 ToolMessage（查询结果原文）不得成为 delta——messages 流实测会携带它。"""
    mapper = server.StreamEventMapper()
    tool_result = ToolMessage(content="共 1 行\ngmv\n16029955.34", name="execute_readonly_sql", tool_call_id="c1")
    assert mapper.feed(tool_result) == []
    assert mapper.feed(_text_chunk("上月 GMV 为 1603 万元")) == [("delta", {"text": "上月 GMV 为 1603 万元"})]


def test_mapper_tool_message_does_not_pollute_last_generated_sql():
    mapper = server.StreamEventMapper()
    mapper.feed(ToolMessage(content="rows...", name="execute_readonly_sql", tool_call_id="c1"))
    assert mapper.last_generated_sql is None


# ---------------------------------------------------------------------------
# _extract_interrupt_payload
# ---------------------------------------------------------------------------


def test_extract_interrupt_payload_hit():
    payload = _interrupt_value(sql="SELECT 42")
    agent = InterruptFakeAgent(interrupt=payload)
    assert server._extract_interrupt_payload(agent, {"configurable": {"thread_id": "t"}}) is payload


def test_extract_interrupt_payload_miss_cases():
    assert server._extract_interrupt_payload(PlainFakeAgent(), {"configurable": {"thread_id": "t"}}) is None
    # tasks 里没有 interrupts / 载荷没有 action_requests → 均视为无中断
    empty = SimpleNamespace(tasks=[SimpleNamespace(interrupts=[])])
    agent = SimpleNamespace(get_state=lambda config: empty)
    assert server._extract_interrupt_payload(agent, {}) is None
    no_action = SimpleNamespace(tasks=[SimpleNamespace(interrupts=[SimpleNamespace(value={"other": 1})])])
    agent2 = SimpleNamespace(get_state=lambda config: no_action)
    assert server._extract_interrupt_payload(agent2, {}) is None
    # get_state 抛异常 → None（探测绝不打断正常轮）
    class Boom:
        def get_state(self, config):
            raise RuntimeError("boom")

    assert server._extract_interrupt_payload(Boom(), {}) is None


# ---------------------------------------------------------------------------
# _chat_event_stream：pending 分支（§3.4）
# ---------------------------------------------------------------------------


def test_event_stream_interrupt_yields_pending_approval_without_done():
    agent = InterruptFakeAgent(
        [_usage_chunk("让我查一下", 100, 10)],
        interrupt=_interrupt_value(sql="SELECT SUM(amount) FROM payments"),
    )
    pt = FakePt(agent, injected_chunks=[SimpleNamespace(doc_type="schema", title="payments", text="列说明")])
    frames = _collect(server._chat_event_stream(pt, "上个月GMV", 0.0, "1a2b3c4d"))

    names = [name for name, _ in frames]
    assert names[0] == "meta"
    assert "done" not in names  # §3.4：pending 轮不发 done
    assert "error" not in names
    pending = [f for f in frames if f[0] == "pending_approval"]
    assert len(pending) == 1
    data = pending[0][1]
    assert data["sql"] == "SELECT SUM(amount) FROM payments"
    assert data["tokens"] == {"input": 100, "output": 10}  # 停车前已消耗的 token（additive 字段）
    assert isinstance(data["latencyMs"], int)
    assert data["approvalId"]

    # 注册表落地：find_by_thread 命中，context_block 与停车轮 format_context_block 一致
    entry = approval.find_by_thread("u42:8f3a")
    assert entry is not None and entry["approvalId"] == data["approvalId"]
    assert entry["sql"] == data["sql"]
    assert entry["question"] == "上个月GMV"
    assert "--- [1] 表结构 · payments ---" in entry["context_block"]


def test_event_stream_normal_completion_still_emits_done():
    """无 interrupt 的轮次照常发 done（W4 行为回归：探测零影响）。"""
    agent = PlainFakeAgent([_usage_chunk("答案是 1", 5, 2)])
    frames = _collect(server._chat_event_stream(FakePt(agent), "问题", 0.0, "env"))
    assert frames[-1][0] == "done"
    assert frames[-1][1]["tokens"] == {"input": 5, "output": 2}


def test_event_stream_pending_exists_race_emits_error_event():
    """并发双提问兜底：register 抛 PendingApprovalExists → error 事件（不崩流）。"""
    approval.register("u42:8f3a", "旧 SQL", "旧问", "旧 ctx")
    agent = InterruptFakeAgent([], interrupt=_interrupt_value())
    frames = _collect(server._chat_event_stream(FakePt(agent), "新问", 0.0, "env"))
    assert frames[-1][0] == "error"
    assert frames[-1][1]["code"] == "pending_approval_exists"


# ---------------------------------------------------------------------------
# 端点：/agent/chat 的 409 未决检查 + X-User-HITL 透传
# ---------------------------------------------------------------------------


@pytest.fixture()
def client(monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setattr(server, "_preflight", lambda: None)
    monkeypatch.setattr(server, "INTERNAL_TOKEN", "")
    monkeypatch.setattr(server, "DEEPSEEK_API_KEY", "")
    with TestClient(server.app) as c:
        yield c


UUID1 = "8f3a1111-2222-3333-4444-555555555555"
HEADERS = {"X-User-LLM-Key": "sk-test-key-0123456789abcdef", "X-User-Id": "42"}


def test_chat_endpoint_409_when_thread_has_pending(client, monkeypatch):
    """未决审批期间同 thread 新提问 → 409 pending_approval_exists（§3.10）。"""
    approval.register("u42:" + UUID1, "SELECT 1", "旧问", "ctx")
    called = {"prepare_turn": False}

    def _no_prepare(*args, **kwargs):
        called["prepare_turn"] = True
        raise AssertionError("409 必须发生在 prepare_turn 之前")

    monkeypatch.setattr(server, "prepare_turn", _no_prepare)
    resp = client.post("/agent/chat", json={"question": "新问题", "threadId": UUID1}, headers=HEADERS)
    assert resp.status_code == 409
    assert resp.json()["code"] == "pending_approval_exists"
    assert called["prepare_turn"] is False


def test_chat_endpoint_passes_hitl_header_to_prepare_turn(client, monkeypatch):
    """X-User-HITL 头 → prepare_turn(hitl=...)：'1' 开、缺省关（§3.8 开关是请求域的）。"""
    captured = {}

    def fake_prepare_turn(question, thread_key, api_key=None, top_n=None, hitl=False):
        captured["hitl"] = hitl
        captured["thread_key"] = thread_key
        return FakePt(PlainFakeAgent([_usage_chunk("答", 1, 1)]), thread_id=thread_key)

    monkeypatch.setattr(server, "prepare_turn", fake_prepare_turn)
    resp = client.post("/agent/chat", json={"question": "问题", "threadId": UUID1},
                       headers={**HEADERS, "X-User-HITL": "1"})
    assert resp.status_code == 200
    assert captured["hitl"] is True
    resp = client.post("/agent/chat", json={"question": "问题", "threadId": UUID1}, headers=HEADERS)
    assert resp.status_code == 200
    assert captured["hitl"] is False


def test_chat_endpoint_hitl_off_stream_completes_with_done(client, monkeypatch):
    """关开关（W4 语义）：无 interrupt → done 照常，全程无 pending_approval。"""
    monkeypatch.setattr(
        server, "prepare_turn",
        lambda q, tk, api_key=None, top_n=None, hitl=False: FakePt(PlainFakeAgent([_usage_chunk("答", 1, 1)]), thread_id=tk),
    )
    resp = client.post("/agent/chat", json={"question": "问题", "threadId": UUID1}, headers=HEADERS)
    body = resp.text
    assert "event: done" in body
    assert "pending_approval" not in body


# ---------------------------------------------------------------------------
# 端点：/agent/chat/approval（§3.4 [批准]/[拒绝]）
# ---------------------------------------------------------------------------


def _seed_pending(thread_uuid: str = UUID1, sql: str = "SELECT SUM(amount) FROM payments") -> str:
    return approval.register(f"u42:{thread_uuid}", sql, "上个月GMV", "停车轮参考资料")


def test_approval_endpoint_forbidden_403(client, monkeypatch):
    monkeypatch.setattr(server, "INTERNAL_TOKEN", "tok")  # 非空才启用校验（开发机空串=跳过）
    resp = client.post(
        "/agent/chat/approval",
        json={"threadId": UUID1, "approvalId": "x", "decision": "approve"},
        headers={"X-Internal-Token": "wrong", **HEADERS},
    )
    assert resp.status_code == 403
    resp = client.post(
        "/agent/chat/approval",
        json={"threadId": UUID1, "approvalId": "x", "decision": "approve"},
        headers={"X-Internal-Token": "tok", **HEADERS},
    )
    assert resp.status_code != 403  # 正确 token 放行（后续因 approvalId 不存在而 409）


def test_approval_endpoint_key_missing_401(client):
    resp = client.post(
        "/agent/chat/approval",
        json={"threadId": UUID1, "approvalId": "x", "decision": "approve"},
    )
    assert resp.status_code == 401
    assert resp.json()["code"] == "llm_key_missing"


def test_approval_endpoint_bad_decision_422(client):
    resp = client.post(
        "/agent/chat/approval",
        json={"threadId": UUID1, "approvalId": "x", "decision": "edit"},
        headers=HEADERS,
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "approval_bad_decision"


def test_approval_endpoint_bad_thread_id_422(client):
    resp = client.post(
        "/agent/chat/approval",
        json={"threadId": "not-a-uuid", "approvalId": "x", "decision": "approve"},
        headers=HEADERS,
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "bad_request"


def test_approval_endpoint_not_found_409(client):
    resp = client.post(
        "/agent/chat/approval",
        json={"threadId": UUID1, "approvalId": "deadbeef", "decision": "approve"},
        headers=HEADERS,
    )
    assert resp.status_code == 409
    assert resp.json()["code"] == "approval_not_found"


def test_approval_endpoint_expired_410(client, monkeypatch):
    monkeypatch.setattr(approval, "PENDING_TTL_SECONDS", 0.0)
    aid = _seed_pending()
    resp = client.post(
        "/agent/chat/approval",
        json={"threadId": UUID1, "approvalId": aid, "decision": "approve"},
        headers=HEADERS,
    )
    assert resp.status_code == 410
    assert resp.json()["code"] == "approval_expired"


def test_approval_endpoint_cross_user_mismatch_409(client):
    aid = approval.register("u43:" + UUID1, "SELECT 1", "问", "ctx")  # 属于用户 43
    resp = client.post(
        "/agent/chat/approval",
        json={"threadId": UUID1, "approvalId": aid, "decision": "approve"},
        headers=HEADERS,  # 请求者 42
    )
    assert resp.status_code == 409
    assert resp.json()["code"] == "approval_not_found"  # 归属不匹配按 not_found 报，不泄露存在性


def test_approval_endpoint_approve_happy_path(client, monkeypatch):
    """批准 → 重建 agent + Command(resume={"decisions":[{"type":"approve"}]}) → SSE 恢复流。"""
    aid = _seed_pending()
    captured: dict = {}

    class ResumeFakeAgent:
        def stream(self, inputs, config=None, stream_mode=None):
            captured["input"] = inputs
            captured["config"] = config
            # 实测：resume 的 messages 流首个 chunk 是 ToolMessage（执行结果），mapper 必须滤掉
            yield ToolMessage(content="[2 行结果]", name="execute_readonly_sql", tool_call_id="c1"), {"langgraph_node": "tools"}
            yield _usage_chunk("上月 GMV 为 1603 万元", 50, 20), {"langgraph_node": "model"}

    def fake_build_agent(system_prompt, api_key=None, hitl=False):
        captured["system_prompt"] = system_prompt
        captured["hitl"] = hitl
        assert hitl is True  # 恢复轮必须带 middleware（停车状态依赖同配置 agent，§8-⑦）
        return ResumeFakeAgent()

    monkeypatch.setattr(server, "build_agent", fake_build_agent)
    resp = client.post(
        "/agent/chat/approval",
        json={"threadId": UUID1, "approvalId": aid, "decision": "approve"},
        headers=HEADERS,
    )
    body = resp.text
    assert resp.status_code == 200
    # 恢复流事件序列：meta → delta（模型收尾作答）→ done；无 saved（那是 Java 注入的）
    assert "event: meta" in body
    assert 'data: {"text": "上月 GMV 为 1603 万元"}' in body
    assert "event: done" in body
    assert "event: saved" not in body
    assert "[2 行结果]" not in body  # ToolMessage 内容不泄漏
    # resume 输入契约（§3.3-3 实测）：Command 且带 decisions 键的 dict
    assert isinstance(captured["input"], Command)
    assert captured["input"].resume == {"decisions": [{"type": "approve"}]}
    assert captured["config"]["configurable"]["thread_id"] == "u42:" + UUID1
    # 提示词连续（§3.5 恢复轮口径）：context_block 原样进 system prompt
    assert "停车轮参考资料" in captured["system_prompt"]
    # pop 已消费：注册表清空
    assert approval.find_by_thread("u42:" + UUID1) is None


def test_approval_endpoint_reject_sends_chinese_guidance(client, monkeypatch):
    aid = _seed_pending(sql="SELECT SLEEP(1)")
    captured: dict = {}

    class ResumeFakeAgent:
        def stream(self, inputs, config=None, stream_mode=None):
            captured["resume"] = inputs.resume
            yield ToolMessage(
                content=f"User rejected the tool call for `execute_readonly_sql` with reason: {approval.REJECT_MESSAGE}",
                name="execute_readonly_sql", tool_call_id="c1", status="error",
            ), {"langgraph_node": "tools"}
            yield _usage_chunk("好的，请告诉我您希望如何调整？", 60, 25), {"langgraph_node": "model"}

    monkeypatch.setattr(server, "build_agent", lambda sp, api_key=None, hitl=False: ResumeFakeAgent())
    resp = client.post(
        "/agent/chat/approval",
        json={"threadId": UUID1, "approvalId": aid, "decision": "reject"},
        headers=HEADERS,
    )
    body = resp.text
    assert resp.status_code == 200
    # reject 载荷：type=reject + 中文引导（§3.4）；英文 ToolMessage 回执不泄漏进 delta
    assert captured["resume"] == {"decisions": [{"type": "reject", "message": approval.REJECT_MESSAGE}]}
    assert "好的，请告诉我您希望如何调整？" in body
    assert "User rejected" not in body


def test_approval_endpoint_chained_interrupt_registers_new_pending(client, monkeypatch):
    """§8-⑰ 链式审批：批准第 1 条后模型为第 2 条 SQL 重新 interrupt → 新 pending、无 done。"""
    aid = _seed_pending(sql="SELECT 1")

    class ChainedAgent:
        def __init__(self):
            self.phase = 0

        def stream(self, inputs, config=None, stream_mode=None):
            assert inputs.resume["decisions"][0]["type"] == "approve"
            self.phase += 1
            yield _usage_chunk("第一条已执行", 40, 10), {"langgraph_node": "model"}
            # 随后模型又发起一条 SQL → 新 interrupt（messages 流自然结束）
            # （真 agent 的 interrupt 不会再走 stream 产出——这里用 get_state 表达停车态）

        def get_state(self, config):
            if self.phase >= 1:
                return SimpleNamespace(tasks=[SimpleNamespace(interrupts=[SimpleNamespace(
                    value=_interrupt_value(sql="SELECT 2"))])])
            return SimpleNamespace(tasks=())

    monkeypatch.setattr(server, "build_agent", lambda sp, api_key=None, hitl=False: ChainedAgent())
    resp = client.post(
        "/agent/chat/approval",
        json={"threadId": UUID1, "approvalId": aid, "decision": "approve"},
        headers=HEADERS,
    )
    body = resp.text
    assert "event: pending_approval" in body
    assert "event: done" not in body
    new_sql = json.loads(
        next(line for line in body.splitlines() if line.startswith("data: ") and "approvalId" in line).removeprefix("data: ")
    )
    assert new_sql["sql"] == "SELECT 2"
    entry = approval.find_by_thread("u42:" + UUID1)
    assert entry is not None and entry["approvalId"] == new_sql["approvalId"]
    assert entry["context_block"] == "停车轮参考资料"  # 链式轮复用停车轮 context_block


def test_full_loop_pending_then_approve_via_two_endpoints(client, monkeypatch):
    """端到端串跑（mock agent）：提问停车 → pending_approval → 审批端点批准 → done。"""
    interrupt_agent = InterruptFakeAgent(
        [_usage_chunk("让我先查询", 100, 10)],
        interrupt=_interrupt_value(sql="SELECT 1"),
    )
    monkeypatch.setattr(
        server, "prepare_turn",
        lambda q, tk, api_key=None, top_n=None, hitl=False: FakePt(
            interrupt_agent, thread_id=tk, injected_chunks=[SimpleNamespace(doc_type="glossary", title="口径", text="GMV=支付成功金额")],
        ),
    )
    first = client.post("/agent/chat", json={"question": "上个月GMV", "threadId": UUID1},
                        headers={**HEADERS, "X-User-HITL": "1"})
    assert "event: pending_approval" in first.text
    assert "event: done" not in first.text
    pending_data = json.loads(
        next(line for line in first.text.splitlines() if line.startswith("data: ") and "approvalId" in line).removeprefix("data: ")
    )
    approval_id = pending_data["approvalId"]
    assert pending_data["sql"] == "SELECT 1"

    # 同 thread 未决期间新提问 → 409
    resp = client.post("/agent/chat", json={"question": "另一个问题", "threadId": UUID1}, headers=HEADERS)
    assert resp.status_code == 409

    # 批准 → 恢复流（mock build_agent）
    class ResumeFakeAgent:
        def stream(self, inputs, config=None, stream_mode=None):
            assert isinstance(inputs, Command)
            yield _usage_chunk("执行完成，结果为 1", 30, 15), {"langgraph_node": "model"}

    monkeypatch.setattr(server, "build_agent", lambda sp, api_key=None, hitl=False: ResumeFakeAgent())
    second = client.post(
        "/agent/chat/approval",
        json={"threadId": UUID1, "approvalId": approval_id, "decision": "approve"},
        headers=HEADERS,
    )
    assert "event: done" in second.text
    assert approval.find_by_thread("u42:" + UUID1) is None


def test_approval_thread_id_is_uuid_validated_like_chat(client):
    """threadId 校验与 /agent/chat 同口径（uuid），防注册表 thread_key 被污染。"""
    resp = client.post(
        "/agent/chat/approval",
        json={"threadId": str(uuid.uuid4()) + "x", "approvalId": "x", "decision": "approve"},
        headers=HEADERS,
    )
    assert resp.status_code == 422
