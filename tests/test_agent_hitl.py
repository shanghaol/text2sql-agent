"""W5 HITL 的组装层测试（W5 设计 §3.3/§3.9）：build_agent 条件注入 + prepare_turn 透传。

红线：hitl=False（默认）时 build_agent **不传 middleware 参数**——CLI / 评测 / W4
构造路径逐字节等价（W4 §6.2 三道同构验收的前提）。用 recorder 捕获 create_agent
的 kwargs 断言"存在与不存在"，不起真模型、不连检索。
"""

import pytest

from agent.core import agent as agent_mod
from agent.core import turn as turn_mod


class _Recorder:
    """create_agent 替身：记录 kwargs，返回可识别的哨兵对象。"""

    def __init__(self):
        self.calls: list[dict] = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return object()


@pytest.fixture()
def recorder(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(agent_mod, "create_agent", rec)
    return rec


def test_build_agent_default_has_no_middleware(recorder):
    """默认（hitl 缺省/False）：绝不传 middleware 参数——W4 构造路径逐字节等价（§3.9）。"""
    agent_mod.build_agent("系统提示词")
    agent_mod.build_agent("系统提示词", api_key=None, hitl=False)
    assert len(recorder.calls) == 2
    for call in recorder.calls:
        assert "middleware" not in call
        assert set(call) == {"model", "tools", "system_prompt", "checkpointer"}


def test_build_agent_hitl_injects_middleware_for_sql_tool_only(recorder):
    agent_mod.build_agent("系统提示词", hitl=True)
    assert len(recorder.calls) == 1
    middleware = recorder.calls[0]["middleware"]
    assert len(middleware) == 1
    from langchain.agents.middleware import HumanInTheLoopMiddleware

    assert isinstance(middleware[0], HumanInTheLoopMiddleware)
    interrupt_on = middleware[0].interrupt_on
    # 只有 execute_readonly_sql 需要审批；list_tables 不在名单 → 自动放行（§3.3-1 实测）
    assert set(interrupt_on) == {"execute_readonly_sql"}
    config = interrupt_on["execute_readonly_sql"]
    assert config["allowed_decisions"] == ["approve", "reject"]  # 不给 edit/respond（§9-1）
    # description callable 的中文定制（含 SQL）——2026-10-11 实测它是 interrupt_on 的字段
    description = config["description"]({"args": {"sql": "SELECT 1"}}, None, None)
    assert "SELECT 1" in description


def test_build_agent_hitl_description_handles_missing_sql(recorder):
    agent_mod.build_agent("sp", hitl=True)
    description = recorder.calls[0]["middleware"][0].interrupt_on["execute_readonly_sql"]["description"]
    assert "未提供 SQL" in description({"args": {}}, None, None)
    assert "未提供 SQL" in description({}, None, None)


def test_prepare_turn_passes_hitl_through(monkeypatch, recorder):
    """prepare_turn(..., hitl=...) 透传给 build_agent；默认 False（评测/CLI 零变化）。"""
    captured: dict = {}

    def fake_build_agent(system_prompt, api_key=None, hitl=False):
        captured["hitl"] = hitl
        captured["api_key"] = api_key
        return object()

    class _FakeRetriever:
        def search(self, query, top_n=None):
            return []

    monkeypatch.setattr(turn_mod, "build_agent", fake_build_agent)
    monkeypatch.setattr(turn_mod, "get_retriever", lambda: _FakeRetriever())

    turn_mod.prepare_turn("问题", "t1", hitl=True)
    assert captured["hitl"] is True
    turn_mod.prepare_turn("问题", "t1", api_key="sk-x", hitl=False)
    assert captured["hitl"] is False and captured["api_key"] == "sk-x"
    turn_mod.prepare_turn("问题", "t1")
    assert captured["hitl"] is False and captured["api_key"] is None
