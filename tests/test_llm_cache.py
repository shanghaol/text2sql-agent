"""get_chat_model LRU 客户端缓存的默认套件测试（W4 设计 §1.6）。

零环境依赖：monkeypatch 掉 agent.core.agent.ChatDeepSeek 为假构造器，
不发起任何网络调用、不需要 .env 里有 key。验证：
- 同 key 同实例（命中缓存复用 httpx 连接池）；
- 不同 key 不同实例（auth 隔离，绝不串号）；
- None 走 "__env__" 分支且**绝不传 api_key 参数**（与 W3 构造路径逐字节等价）；
- LRU 容量逐出（最旧条目先出）。
"""

import threading
from typing import ClassVar

import pytest

from agent.core import agent as core_agent


class _FakeChatDeepSeek:
    """记录构造参数的假 ChatDeepSeek（get_chat_model 的注入点）。"""

    instances: ClassVar[list["_FakeChatDeepSeek"]] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        _FakeChatDeepSeek.instances.append(self)


@pytest.fixture()
def fake_llm(monkeypatch):
    """替换 ChatDeepSeek、清空缓存、缩容缓存，并在用例后全部还原。"""
    _FakeChatDeepSeek.instances = []
    monkeypatch.setattr(core_agent, "ChatDeepSeek", _FakeChatDeepSeek)
    monkeypatch.setattr(core_agent, "LLM_CLIENT_CACHE_SIZE", 16)
    core_agent._LLM_CACHE.clear()
    yield _FakeChatDeepSeek
    core_agent._LLM_CACHE.clear()


def test_same_key_returns_same_instance(fake_llm):
    """同 key 两次获取 = 同一实例（缓存命中，move_to_end 不重建）。"""
    m1 = core_agent.get_chat_model("sk-aaaaaaaaaaaaaaaaaaaa")
    m2 = core_agent.get_chat_model("sk-aaaaaaaaaaaaaaaaaaaa")
    assert m1 is m2
    assert len(_FakeChatDeepSeek.instances) == 1


def test_different_keys_get_different_instances(fake_llm):
    """不同 key = 不同实例：并发请求互不串号（§1.6 反面教材的结构性防线）。"""
    m1 = core_agent.get_chat_model("sk-key-one")
    m2 = core_agent.get_chat_model("sk-key-two")
    assert m1 is not m2
    assert len(_FakeChatDeepSeek.instances) == 2


def test_none_uses_env_branch_and_never_passes_api_key(fake_llm):
    """api_key=None：走 "__env__" 缓存键，构造参数里**没有** api_key——
    显式条件传参，CLI/评测路径与 W3 构造逐字节等价。"""
    m1 = core_agent.get_chat_model(None)
    m2 = core_agent.get_chat_model(None)
    assert m1 is m2
    assert "api_key" not in m1.kwargs
    assert m1.kwargs == {"model": core_agent.DEEPSEEK_MODEL, "api_base": "https://api.deepseek.com", "temperature": 0}
    # env 分支与显式 key 分支互为不同实例
    m3 = core_agent.get_chat_model("sk-explicit")
    assert m3 is not m1


def test_explicit_key_passed_verbatim_and_cache_key_is_hash(fake_llm):
    """显式 key 原样传入构造；缓存键用 sha256 前 16 位而非明文（不缓存明文 key）。"""
    key = "sk-explicit-key-value"
    m = core_agent.get_chat_model(key)
    assert m.kwargs["api_key"] == key
    import hashlib

    expected = (hashlib.sha256(key.encode()).hexdigest()[:16], core_agent.DEEPSEEK_MODEL)
    assert expected in core_agent._LLM_CACHE
    assert core_agent._LLM_CACHE[expected] is m


def test_lru_evicts_oldest_when_over_capacity(fake_llm, monkeypatch):
    """容量 2：超出即逐出最旧条目；再取被逐出的 key 会重建出新实例。"""
    monkeypatch.setattr(core_agent, "LLM_CLIENT_CACHE_SIZE", 2)
    m1 = core_agent.get_chat_model("sk-old")
    core_agent.get_chat_model("sk-mid")
    m3 = core_agent.get_chat_model("sk-new")  # 插入第 3 个 → sk-old 被逐出
    assert len(core_agent._LLM_CACHE) == 2
    core_agent.get_chat_model("sk-another")  # 逐出 sk-mid
    assert len(core_agent._LLM_CACHE) == 2
    # 重建出的 sk-old 是新实例（旧的已被逐出、不再被缓存引用）
    m1_again = core_agent.get_chat_model("sk-old")
    assert m1_again is not m1
    assert m1_again is not m3


def test_lru_touch_refreshes_recency(fake_llm, monkeypatch):
    """命中即 move_to_end：被再次访问的 key 不在下一轮逐出中先出。"""
    monkeypatch.setattr(core_agent, "LLM_CLIENT_CACHE_SIZE", 2)
    m_first = core_agent.get_chat_model("sk-first")
    core_agent.get_chat_model("sk-second")
    core_agent.get_chat_model("sk-first")  # touch → sk-second 变成最旧
    core_agent.get_chat_model("sk-third")  # 逐出 sk-second
    assert core_agent.get_chat_model("sk-first") is m_first


def test_cache_is_lock_protected_under_threads(fake_llm):
    """多线程并发首次取同一 key：无异常、缓存最终收敛为该 key 的一个条目。

    （按设计骨架，构造在锁外进行——并发首miss最多多构造一次、以最后写入者
    进缓存，属良性竞争；此处断言的是锁保护下缓存状态的一致性，不锁会堆积多行。）"""
    results: list = []
    errors: list[Exception] = []
    barrier = threading.Barrier(4)

    def worker():
        barrier.wait()
        try:
            results.append(core_agent.get_chat_model("sk-concurrent"))
        except Exception as exc:  # noqa: BLE001 - 线程里收集异常到主线程断言
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert len(results) == 4
    assert len(core_agent._LLM_CACHE) == 1
    # 全部返回值都可追溯到当次运行构造出的实例（无凭空对象）
    assert all(m in _FakeChatDeepSeek.instances for m in results)


def test_capacity_one_keeps_single_entry(fake_llm, monkeypatch):
    """容量 1：每次换 key 都逐出旧条目，缓存恒为单条目（while 循环的下界行为）。"""
    monkeypatch.setattr(core_agent, "LLM_CLIENT_CACHE_SIZE", 1)
    m1 = core_agent.get_chat_model("sk-a")
    m2 = core_agent.get_chat_model("sk-b")
    assert len(core_agent._LLM_CACHE) == 1
    assert m1 is not m2


def test_real_chatdeepseek_not_constructed_in_tests(fake_llm):
    """防呆：确认 monkeypatch 生效——真实 ChatDeepSeek 构造器未被调用
    （本文件默认套件不依赖 .env key / 网络的原则不破）。"""
    assert core_agent.ChatDeepSeek is _FakeChatDeepSeek
    assert not _FakeChatDeepSeek.instances or all(isinstance(i, _FakeChatDeepSeek) for i in _FakeChatDeepSeek.instances)
