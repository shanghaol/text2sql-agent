"""SiliconFlow rerank 客户端（W6 设计 §4.1）的默认套件测试。

零依赖原则：不联网、不需要真 key——HTTP 面用 httpx.MockTransport mock
（§8-1 真冒烟待 key 后补，见模块 docstring）。key 缺失分支用 monkeypatch
改写 rerank 模块持有的 SILICONFLOW_API_KEY 绑定（import 时按值绑定，
改 agent.config 不影响已导入的名字——这是刻意的解耦，测试同款处理）。
"""

import json

import httpx
import pytest

from agent.rag import rerank as rerank_mod
from agent.rag.rerank import RerankError, SiliconFlowReranker

# SF /v1/rerank 的 mock 冒烟响应（§8-1 待真 key 实测核对的字段名假设）
_OK_BODY = {
    "results": [
        {"index": 1, "relevance_score": 0.92},
        {"index": 0, "relevance_score": 0.35},
        {"index": 2, "relevance_score": 0.92},  # 与 index=1 平局 → 按 index 升序决胜
    ],
    "model": "BAAI/bge-reranker-v2-m3",
}


def _make_reranker(handler, **kwargs) -> tuple[SiliconFlowReranker, list[httpx.Request]]:
    """MockTransport 组装的 reranker；返回 (实例, 收到的请求列表)。"""
    requests_seen: list[httpx.Request] = []

    def recording_handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(request)
        return handler(request)

    client = httpx.Client(transport=httpx.MockTransport(recording_handler))
    reranker = SiliconFlowReranker(api_key="sk-test-key", http_client=client, **kwargs)
    return reranker, requests_seen


# ---------------------------------------------------------------------------
# ① 构造期 key 校验
# ---------------------------------------------------------------------------


def test_constructor_without_key_raises_chinese_guidance(monkeypatch: pytest.MonkeyPatch):
    """key 缺失（显式空 / 环境也未配）构造即抛——中文指引，评测启动期失败。"""
    monkeypatch.setattr(rerank_mod, "SILICONFLOW_API_KEY", "")
    for api_key in (None, ""):
        with pytest.raises(ValueError, match="SILICONFLOW_API_KEY"):
            SiliconFlowReranker(api_key=api_key)


def test_constructor_explicit_key_beats_empty_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(rerank_mod, "SILICONFLOW_API_KEY", "")
    reranker = SiliconFlowReranker(api_key="sk-explicit", http_client=httpx.Client())
    assert reranker.api_key == "sk-explicit"
    reranker.close()


def test_constructor_env_key_used_when_param_none(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(rerank_mod, "SILICONFLOW_API_KEY", "sk-from-env")
    reranker = SiliconFlowReranker(http_client=httpx.Client())
    assert reranker.api_key == "sk-from-env"
    reranker.close()


# ---------------------------------------------------------------------------
# ② 请求/响应契约（mock 冒烟：§8-1 待真 key 核对的部分）
# ---------------------------------------------------------------------------


def test_rerank_request_shape_and_response_parsing():
    """请求字段（model/query/documents/top_n/return_documents + Bearer 头）与
    响应解析（results[].index/relevance_score，降序 + 平局按 index 升序）。"""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("Authorization")
        captured["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, json=_OK_BODY)

    reranker, _ = _make_reranker(handler)
    ranked = reranker.rerank("客单价怎么算", ["文档零", "文档一", "文档二"], top_n=3)

    assert captured["url"].endswith("/rerank")
    assert captured["auth"] == "Bearer sk-test-key"
    assert captured["body"] == {
        "model": "BAAI/bge-reranker-v2-m3",
        "query": "客单价怎么算",
        "documents": ["文档零", "文档一", "文档二"],
        "return_documents": False,
        "top_n": 3,
    }
    # 降序；平局（0.92, 0.92）按 index 升序决胜 → (1, 0.92) 在 (2, 0.92) 前
    assert ranked == [(1, pytest.approx(0.92)), (2, pytest.approx(0.92)), (0, pytest.approx(0.35))]


def test_rerank_empty_documents_short_circuits_without_http():
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - 不该被调到
        return httpx.Response(200, json=_OK_BODY)

    reranker, requests_seen = _make_reranker(handler)
    assert reranker.rerank("任何查询", []) == []
    assert requests_seen == []


def test_rerank_top_n_omitted_when_none():
    """top_n=None 不进 payload（服务端默认返回全量）——字段最小化。"""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, json=_OK_BODY)

    reranker, _ = _make_reranker(handler)
    reranker.rerank("q", ["a", "b", "c"])
    assert "top_n" not in captured["body"]


# ---------------------------------------------------------------------------
# ③ 重试与失败语义（设计 §4.1：内部重试一次；耗尽即抛，fail-closed 不降级）
# ---------------------------------------------------------------------------


def test_rerank_retries_once_on_500_then_succeeds(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(rerank_mod.time, "sleep", lambda _s: None)  # 不真等退避
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(500, text="server boom")
        return httpx.Response(200, json=_OK_BODY)

    reranker, _ = _make_reranker(handler)
    ranked = reranker.rerank("q", ["a", "b", "c"])
    assert calls["n"] == 2
    assert ranked[0] == (1, pytest.approx(0.92))


def test_rerank_retries_on_timeout_then_succeeds(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(rerank_mod.time, "sleep", lambda _s: None)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectTimeout("timed out")
        return httpx.Response(200, json=_OK_BODY)

    reranker, _ = _make_reranker(handler)
    assert reranker.rerank("q", ["a", "b", "c"])
    assert calls["n"] == 2


def test_rerank_raises_after_retry_exhausted(monkeypatch: pytest.MonkeyPatch):
    """重试一次仍 5xx → RerankError（fail-closed：调用方不得吞掉伪装成功）。"""
    monkeypatch.setattr(rerank_mod.time, "sleep", lambda _s: None)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503, text="overloaded")

    reranker, _ = _make_reranker(handler)
    with pytest.raises(RerankError, match="已重试 1 次.*503"):
        reranker.rerank("q", ["a", "b"])
    assert calls["n"] == 2  # 首次 + 1 重试，没有第三次


def test_rerank_4xx_raises_immediately_without_retry():
    """4xx 参数错误重试无意义 → 立即抛（只发一次请求）。"""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(401, text="bad key")

    reranker, _ = _make_reranker(handler)
    with pytest.raises(RerankError, match="401"):
        reranker.rerank("q", ["a", "b"])
    assert calls["n"] == 1


def test_rerank_429_is_retryable(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(rerank_mod.time, "sleep", lambda _s: None)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, text="rate limited")
        return httpx.Response(200, json=_OK_BODY)

    reranker, _ = _make_reranker(handler)
    assert reranker.rerank("q", ["a", "b", "c"])
    assert calls["n"] == 2


# ---------------------------------------------------------------------------
# ④ 响应结构防御（不猜不兜底：结构不合法即抛）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        {"no_results": []},  # 缺 results
        {"results": "not-a-list"},
        {"results": [{"index": 0}]},  # 条目缺 relevance_score
        {"results": [{"relevance_score": 0.5}]},  # 条目缺 index
        {"results": [{"index": 5, "relevance_score": 0.5}]},  # index 越界（2 篇文档）
        {"results": [{"index": "zero", "relevance_score": 0.5}]},  # index 非整数
    ],
)
def test_rerank_invalid_response_shape_raises(body):
    reranker, _ = _make_reranker(lambda request: httpx.Response(200, json=body))
    with pytest.raises(RerankError):
        reranker.rerank("q", ["a", "b"])


def test_rerank_non_json_body_raises():
    reranker, _ = _make_reranker(lambda request: httpx.Response(200, text="<html>not json</html>"))
    with pytest.raises(RerankError, match="不是合法 JSON"):
        reranker.rerank("q", ["a", "b"])
