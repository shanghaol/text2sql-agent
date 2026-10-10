"""SSE 事件帧纯函数的默认套件测试（W4 设计 §3.2 事件契约 / §1.6 错误映射表）。

范围：agent/server.py 里可裸机测的纯逻辑——
- sse_frame 帧格式（event:/data:/空行分帧）；
- StreamEventMapper：chunk → delta/tool（同名去重，与 main.py 打印逻辑逐行同构）
  / done 的 usage 累加；
- map_llm_error：openai SDK 异常 → 业务错误码（§1.6 表）；
- internal_token_ok / validate_chat_request：流前 403/422 的判定；
- _chat_event_stream：用假 agent 走完整事件序列 / 异常转 error / 断连 GeneratorExit。

默认套件零环境依赖：不连 MySQL、不需要 key、不发起网络；
导入 agent.server 不触发 preflight（preflight 挂在 lifespan，仅启动应用时运行）。
"""

import json

import httpx
import openai
import pytest
from langchain_core.messages import AIMessageChunk

from agent import server

# ---------------------------------------------------------------------------
# 测试替身：chunk / agent / pt
# ---------------------------------------------------------------------------


def _text_chunk(text: str) -> AIMessageChunk:
    return AIMessageChunk(content=text)


def _tool_chunk(name: str) -> AIMessageChunk:
    return AIMessageChunk(
        content="",
        tool_call_chunks=[{"name": name, "args": "", "id": f"call_{name}", "index": 0, "type": "tool_call_chunk"}],
    )


def _usage_chunk(text: str, input_tokens: int, output_tokens: int) -> AIMessageChunk:
    """每条 AIMessage 的收尾 chunk：携带 usage_metadata（2026-10-11 实测语义）。"""
    return AIMessageChunk(
        content=text,
        usage_metadata={
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
            "input_token_details": {"cache_read": 0},
            "output_token_details": {"reasoning": 0},
        },
    )


class FakeAgent:
    """agent.stream 的最小替身：按脚本吐 (chunk, metadata) 元组（同 stream_mode="messages"），
    可编程在吐完后抛异常。"""

    def __init__(self, chunks=(), error: Exception | None = None):
        self._chunks = list(chunks)
        self._error = error
        self.stream_started = False

    def stream(self, _inputs, config=None, stream_mode=None):
        self.stream_started = True
        for chunk in self._chunks:
            yield chunk, {"langgraph_node": "agent"}
        if self._error is not None:
            raise self._error


class FakePt:
    """PreparedTurn 的最小替身（生成器只读 agent / config）。"""

    def __init__(self, agent):
        self.agent = agent
        self.config = {"configurable": {"thread_id": "u42:8f3a"}}


# ---------------------------------------------------------------------------
# sse_frame
# ---------------------------------------------------------------------------


def test_sse_frame_format():
    """event: + data: 两行一组、空行分帧；中文不转义。"""
    frame = server.sse_frame("delta", {"text": "根据订单表…"})
    assert frame == 'event: delta\ndata: {"text": "根据订单表…"}\n\n'


def test_sse_frame_data_is_valid_json_line():
    frame = server.sse_frame("done", {"tokens": {"input": 1}, "latencyMs": 2})
    lines = frame.split("\n")
    assert lines[0] == "event: done"
    assert json.loads(lines[1].removeprefix("data: ")) == {"tokens": {"input": 1}, "latencyMs": 2}
    assert lines[2] == "" and lines[3] == ""  # data 行后空行分帧


# ---------------------------------------------------------------------------
# StreamEventMapper：delta / tool 去重 / usage 累加
# ---------------------------------------------------------------------------


def test_mapper_text_chunks_become_delta_events():
    mapper = server.StreamEventMapper()
    events = mapper.feed(_text_chunk("根据")) + mapper.feed(_text_chunk("订单表"))
    assert events == [("delta", {"text": "根据"}), ("delta", {"text": "订单表"})]


def test_mapper_tool_name_deduped_like_main_py():
    """同一轮里同名工具只发一次；换工具再发——与 main.py 去重打印逻辑同构。"""
    mapper = server.StreamEventMapper()
    events = (
        mapper.feed(_tool_chunk("list_tables"))
        + mapper.feed(_tool_chunk("list_tables"))  # 同名增量，去重
        + mapper.feed(_tool_chunk("execute_readonly_sql"))  # 换工具，发
        + mapper.feed(_tool_chunk("execute_readonly_sql"))  # 再去重
    )
    assert events == [("tool", {"name": "list_tables"}), ("tool", {"name": "execute_readonly_sql"})]


def test_mapper_accumulates_usage_per_llm_call():
    """usage_metadata 每条 AIMessage 收尾 chunk 一份：累加 = 每次 LLM 请求计一次。"""
    mapper = server.StreamEventMapper()
    mapper.feed(_usage_chunk("", 100, 10))  # 第一次 LLM 调用收尾
    mapper.feed(_tool_chunk("execute_readonly_sql"))  # 无 usage 的中间 chunk
    mapper.feed(_usage_chunk("答案是…", 200, 30))  # 第二次 LLM 调用收尾
    assert mapper.usage_input == 300
    assert mapper.usage_output == 40


def test_mapper_mixed_chunk_emits_delta_and_tool():
    """一个 chunk 同时带文本与工具调用增量：两类事件都发（与 main.py 相同的两条独立分支）。"""
    chunk = AIMessageChunk(
        content="让我查一下",
        tool_call_chunks=[{"name": "execute_readonly_sql", "args": "", "id": "c1", "index": 0, "type": "tool_call_chunk"}],
    )
    events = server.StreamEventMapper().feed(chunk)
    assert ("delta", {"text": "让我查一下"}) in events
    assert ("tool", {"name": "execute_readonly_sql"}) in events


# ---------------------------------------------------------------------------
# map_llm_error（§1.6 错误映射表）
# ---------------------------------------------------------------------------


def _status_error(status: int) -> openai.APIStatusError:
    request = httpx.Request("POST", "https://api.deepseek.com/chat/completions")
    response = httpx.Response(status, request=request)
    return openai.APIStatusError("上游错误", response=response, body=None)


def test_map_auth_error_to_key_invalid():
    request = httpx.Request("POST", "https://api.deepseek.com/chat/completions")
    exc = openai.AuthenticationError("Invalid key", response=httpx.Response(401, request=request), body=None)
    code, message = server.map_llm_error(exc)
    assert code == "llm_key_invalid"
    assert "重新录入" in message


def test_map_402_to_insufficient_balance():
    code, message = server.map_llm_error(_status_error(402))
    assert code == "llm_insufficient_balance"
    assert "余额" in message


def test_map_429_to_rate_limited():
    code, _ = server.map_llm_error(_status_error(429))
    assert code == "llm_rate_limited"


def test_map_5xx_and_connection_and_generic_to_upstream_error():
    assert server.map_llm_error(_status_error(500))[0] == "llm_upstream_error"
    assert server.map_llm_error(_status_error(503))[0] == "llm_upstream_error"
    request = httpx.Request("POST", "https://api.deepseek.com/chat/completions")
    assert server.map_llm_error(openai.APIConnectionError(request=request))[0] == "llm_upstream_error"
    assert server.map_llm_error(ValueError("boom"))[0] == "llm_upstream_error"  # 非 LLM 异常也兜底


# ---------------------------------------------------------------------------
# internal_token_ok / validate_chat_request（流前 403 / 422）
# ---------------------------------------------------------------------------


def test_internal_token_skipped_when_config_empty(monkeypatch):
    """INTERNAL_TOKEN 空串 = 跳过校验（开发机直连调试，§3.4）。"""
    monkeypatch.setattr(server, "INTERNAL_TOKEN", "")
    assert server.internal_token_ok(None) is True
    assert server.internal_token_ok("anything") is True


def test_internal_token_enforced_when_configured(monkeypatch):
    monkeypatch.setattr(server, "INTERNAL_TOKEN", "devtoken")
    assert server.internal_token_ok("devtoken") is True
    assert server.internal_token_ok("wrong") is False
    assert server.internal_token_ok(None) is False
    assert server.internal_token_ok("") is False


def test_validate_chat_request_accepts_normal_question():
    assert server.validate_chat_request("上个月的GMV是多少？", "8f3a1111-2222-3333-4444-555555555555") is None


def test_validate_chat_request_rejects_blank_question():
    assert server.validate_chat_request("", "8f3a1111-2222-3333-4444-555555555555") is not None
    assert server.validate_chat_request("   ", "8f3a1111-2222-3333-4444-555555555555") is not None


def test_validate_chat_request_rejects_overlong_question(monkeypatch):
    monkeypatch.setattr(server, "MAX_QUESTION_CHARS", 10)
    message = server.validate_chat_request("字" * 11, "8f3a1111-2222-3333-4444-555555555555")
    assert message is not None and "2000" not in message  # 用的是被替换的上限
    assert server.validate_chat_request("字" * 10, "8f3a1111-2222-3333-4444-555555555555") is None


def test_validate_chat_request_rejects_bad_thread_id():
    assert server.validate_chat_request("问题", "not-a-uuid") is not None
    assert server.validate_chat_request("问题", "") is not None


def test_validate_chat_request_length_uses_configured_limit(monkeypatch):
    """默认上限读 config.MAX_QUESTION_CHARS（2000）——贴着上限的边界两侧。"""
    assert server.MAX_QUESTION_CHARS == 2000
    message = server.validate_chat_request("字" * 2001, "8f3a1111-2222-3333-4444-555555555555")
    assert message is not None


# ---------------------------------------------------------------------------
# _chat_event_stream：完整事件序列 / 异常转 error / 断连 GeneratorExit
# ---------------------------------------------------------------------------


def _collect(generator) -> list[tuple[str, dict]]:
    frames = []
    for frame in generator:
        lines = frame.strip().split("\n")
        event = lines[0].removeprefix("event: ")
        data = json.loads(lines[1].removeprefix("data: "))
        frames.append((event, data))
    return frames


def test_event_stream_full_sequence_meta_delta_tool_done():
    agent = FakeAgent(
        [
            _tool_chunk("list_tables"),
            _tool_chunk("list_tables"),
            _usage_chunk("本月GMV是", 8123, 640),
            _text_chunk(" 123.45 元"),
        ]
    )
    frames = _collect(server._chat_event_stream(FakePt(agent), "上个月GMV", started_at=0.0, key_fingerprint="env"))

    assert frames[0] == ("meta", {"threadKey": "u42:8f3a", "model": server.DEEPSEEK_MODEL})
    assert ("tool", {"name": "list_tables"}) in frames  # 去重后只一次
    assert ("delta", {"text": "本月GMV是"}) in frames
    assert ("delta", {"text": " 123.45 元"}) in frames
    done = frames[-1]
    assert done[0] == "done"
    assert done[1]["tokens"] == {"input": 8123, "output": 640}
    assert isinstance(done[1]["latencyMs"], int)


def test_event_stream_error_event_and_no_done_after_failure():
    """流中异常 → error 事件、之后流结束（无 done）——异常绝不裸抛打断连接。"""
    agent = FakeAgent([_text_chunk("部分回答")], error=_status_error(402))
    frames = _collect(server._chat_event_stream(FakePt(agent), "问题", 0.0, "1a2b3c4d"))
    assert frames[0][0] == "meta"
    assert frames[1] == ("delta", {"text": "部分回答"})
    assert frames[-1][0] == "error"
    assert frames[-1][1]["code"] == "llm_insufficient_balance"
    assert all(name != "done" for name, _ in frames)


def test_event_stream_client_disconnect_generator_exit():
    """客户端断连：生成器被 close → GeneratorExit 在 yield 点抛出 →
    记日志后原样退出（不 yield、不吞异常），close() 正常返回，无僵尸迭代。"""
    agent = FakeAgent([_text_chunk("a"), _text_chunk("b"), _text_chunk("c")])
    generator = server._chat_event_stream(FakePt(agent), "问题", 0.0, "env")
    assert next(generator).startswith("event: meta")
    generator.close()  # 模拟 Starlette 在客户端断连时取消迭代
    with pytest.raises(StopIteration):
        next(generator)  # close 后生成器已终结（GeneratorExit 未被吞、未继续 yield）


def test_event_stream_meta_first_even_when_agent_fails_immediately():
    agent = FakeAgent([], error=RuntimeError("x"))
    frames = _collect(server._chat_event_stream(FakePt(agent), "问题", 0.0, "env"))
    assert [name for name, _ in frames] == ["meta", "error"]


# ---------------------------------------------------------------------------
# 端点级冒烟（TestClient，lifespan 跳过 preflight：不依赖 MySQL/Chroma）
# ---------------------------------------------------------------------------


def test_chat_endpoint_forbidden_and_bad_request_without_preflight(monkeypatch):
    """403（坏内部头）与 422（坏 threadId）在流之前返回 JSON——
    monkeypatch 掉 _preflight，避免测试起 lifespan 时连库。"""
    from fastapi.testclient import TestClient

    monkeypatch.setattr(server, "_preflight", lambda: None)
    monkeypatch.setattr(server, "INTERNAL_TOKEN", "tok")
    with TestClient(server.app) as client:
        resp = client.post(
            "/agent/chat",
            json={"question": "问题", "threadId": "8f3a1111-2222-3333-4444-555555555555"},
            headers={"X-Internal-Token": "wrong"},
        )
        assert resp.status_code == 403
        assert resp.json() == {"code": "forbidden", "message": "内部令牌缺失或不正确"}

        resp = client.post(
            "/agent/chat",
            json={"question": "问题", "threadId": "not-a-uuid"},
            headers={"X-Internal-Token": "tok"},
        )
        assert resp.status_code == 422
        assert resp.json()["code"] == "bad_request"

    assert server.INTERNAL_TOKEN == "tok"  # monkeypatch 作用域内不变


def test_chat_endpoint_key_missing_401(monkeypatch):
    """无 X-User-LLM-Key 且无 env key → 401 llm_key_missing（§1.6 流前错误）。"""
    from fastapi.testclient import TestClient

    monkeypatch.setattr(server, "_preflight", lambda: None)
    monkeypatch.setattr(server, "INTERNAL_TOKEN", "")
    monkeypatch.setattr(server, "DEEPSEEK_API_KEY", "")
    with TestClient(server.app) as client:
        resp = client.post(
            "/agent/chat",
            json={"question": "问题", "threadId": "8f3a1111-2222-3333-4444-555555555555"},
        )
        assert resp.status_code == 401
        assert resp.json()["code"] == "llm_key_missing"


def test_health_endpoint_shape(monkeypatch):
    """/agent/health：{status, chromaChunks, mysqlOk}，无内部头要求。"""
    from fastapi.testclient import TestClient

    monkeypatch.setattr(server, "_preflight", lambda: None)
    monkeypatch.setattr(server, "_check_mysql_ok", lambda: True)
    monkeypatch.setattr(server, "_chroma_count", lambda: 28)
    with TestClient(server.app) as client:
        resp = client.get("/agent/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "up", "chromaChunks": 28, "mysqlOk": True}

    monkeypatch.setattr(server, "_check_mysql_ok", lambda: False)
    with TestClient(server.app) as client:
        assert client.get("/agent/health").json()["status"] == "degraded"


@pytest.fixture(autouse=True)
def _restore_server_globals():
    """防呆：任何用例漏用 monkeypatch 也不会污染后续用例/其他文件。"""
    yield
    from agent import config as cfg

    server.INTERNAL_TOKEN = cfg.INTERNAL_TOKEN
    server.DEEPSEEK_API_KEY = cfg.DEEPSEEK_API_KEY
