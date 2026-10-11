"""SiliconFlow rerank 客户端（W6 设计 §4.1）——评测侧专属，线上不接线（设计 §4.4）。

API 契约（SiliconFlow /v1/rerank，模型 BAAI/bge-reranker-v2-m3，需求 §5 定案）：
    POST {SILICONFLOW_BASE_URL}/rerank
    Authorization: Bearer <SILICONFLOW_API_KEY>
    {"model": "BAAI/bge-reranker-v2-m3",
     "query": "...", "documents": ["...", ...], "top_n": N, "return_documents": false}
    → {"results": [{"index": 0, "relevance_score": 0.87}, ...]}   # 按 relevance_score 降序

> **§8-1 验证清单状态**：编码时开发机无 SILICONFLOW_API_KEY，以上字段名按设计
> §4.1 记载实现、单测用 httpx.MockTransport 按 mock 冒烟；**真 key curl 冒烟待补**
> （拿到 key 后：`.venv/Scripts/python.exe -m agent.rag.rerank "测试查询" "文档一" "文档二"`
> 一次实测，把请求/响应原文贴回 W6 工单；字段与实测不符以实测为准改本模块）。

HTTP 库：httpx（仓库既有依赖——agent/server.py 已在使用并压制其 logger，§8-6 确认，
不新增依赖）。

失败语义（设计 §4.1，**fail-closed，不静默降级**）：网络异常 / 超时 / 429 / 5xx
内部重试一次（指数退避），仍失败抛 RerankError。被否的"fail-open 回退原序"：
静默降级会让消融 F 行部分题实际没 rerank、消融变量失控；显式抛错由评测 runner
既有题级 try/except 落成 error 记录（error_detail 留痕），再 --rerun 补跑——
"消融行不中断"由 runner 兜底，而不是靠谎报一个没有 rerank 的结果。
"""

from __future__ import annotations

import time
from typing import Protocol

import httpx

from agent.config import RERANK_MODEL, SILICONFLOW_API_KEY, SILICONFLOW_BASE_URL


class RerankError(RuntimeError):
    """rerank 调用失败（重试耗尽 / HTTP 非 2xx / 响应结构不合法）。

    fail-closed 语义（设计 §4.1）：调用方不得吞掉后伪装成功——
    评测 runner 的题级 try/except 会把它落成 error 明细记录。
    """


class Reranker(Protocol):
    """检索层 rerank 钩子的最小契约（SiliconFlowReranker 与测试 mock 共同满足）。"""

    def rerank(
        self, query: str, documents: list[str], top_n: int | None = None
    ) -> list[tuple[int, float]]:
        """对 documents 重排，返回 [(index, relevance_score), ...] 按分数降序。

        index 是 documents 的下标（0 基）；top_n=None 表示全部返回。
        """
        ...  # pragma: no cover - Protocol 存根


class SiliconFlowReranker:
    """SiliconFlow /v1/rerank 客户端：query + docs → 重排后的 (index, score) 序列。

    构造即校验 key（缺失抛 ValueError 中文指引——评测 --rerank 在启动期失败，
    而不是跑了一半才在第一题上炸）；http_client 参数供单测注入 MockTransport。
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        timeout_s: float = 15.0,
        max_retries: int = 1,
        http_client: httpx.Client | None = None,
    ) -> None:
        key = api_key or SILICONFLOW_API_KEY
        if not key:
            raise ValueError(
                "未配置 SiliconFlow API Key，无法启用 rerank。请按以下步骤配置：\n"
                "  1. 到 https://siliconflow.cn 注册并创建 API Key；\n"
                "  2. 在仓库根目录 .env 中填入：SILICONFLOW_API_KEY=sk-xxxxxxxx；\n"
                "  3. 重新运行本程序。（rerank 仅评测侧使用，服务器 .env 不配置）"
            )
        self.api_key = key
        self.base_url = (base_url or SILICONFLOW_BASE_URL).rstrip("/")
        self.model = model or RERANK_MODEL
        self.timeout_s = timeout_s
        self.max_retries = max_retries  # 内部重试次数（首次之外的重试）
        self._owns_client = http_client is None
        self._client = http_client or httpx.Client(timeout=timeout_s)

    # -- 对 Reranker 协议的实现（hybrid_retriever 的 rerank 钩子消费） ----------

    def rerank(
        self, query: str, documents: list[str], top_n: int | None = None
    ) -> list[tuple[int, float]]:
        """调 /v1/rerank 重排，返回 [(index, relevance_score), ...] 分数降序。

        空文档列表直接返回空（无候选可排，不算失败）；
        平局按 (分数降序, index 升序) 保证可复现——与 rrf_fuse 的决胜哲学一致。
        """
        if not documents:
            return []
        payload: dict = {
            "model": self.model,
            "query": query,
            "documents": list(documents),
            "return_documents": False,
        }
        if top_n is not None:
            payload["top_n"] = top_n

        url = f"{self.base_url}/rerank"
        last_error: str = ""
        for attempt in range(self.max_retries + 1):
            if attempt > 0:  # 指数退避（设计 §4.1：单次内部重试一次后仍失败即抛）
                time.sleep(0.5 * (2 ** (attempt - 1)))
            try:
                response = self._client.post(
                    url,
                    json=payload,
                    headers={"Authorization": f"Bearer {self.api_key}"},
                )
            except httpx.HTTPError as exc:  # 网络异常/超时：可重试
                last_error = f"{type(exc).__name__}: {exc}"
                continue
            if response.status_code == 200:
                return self._parse_response(response, len(documents))
            last_error = f"HTTP {response.status_code}: {response.text[:200]}"
            # 429（限流）与 5xx（服务端抖动）重试；4xx 参数错误重试无意义，直接抛
            if response.status_code == 429 or response.status_code >= 500:
                continue
            break
        raise RerankError(
            f"SiliconFlow rerank 调用失败（已重试 {self.max_retries} 次）：{last_error}"
        )

    def _parse_response(self, response: httpx.Response, doc_count: int) -> list[tuple[int, float]]:
        """解析 results: [{index, relevance_score}]——结构不合法即抛（不猜不兜底）。"""
        try:
            body = response.json()
        except ValueError as exc:
            raise RerankError(f"rerank 响应不是合法 JSON：{exc}") from exc
        results = body.get("results") if isinstance(body, dict) else None
        if not isinstance(results, list):
            raise RerankError(f"rerank 响应缺少 results 列表：{str(body)[:200]}")
        ranked: list[tuple[int, float]] = []
        for item in results:
            if not isinstance(item, dict) or "index" not in item or "relevance_score" not in item:
                raise RerankError(f"rerank results 条目缺少 index/relevance_score：{str(item)[:200]}")
            index = item["index"]
            if not isinstance(index, int) or not 0 <= index < doc_count:
                raise RerankError(f"rerank results.index 越界（{index}，文档数 {doc_count}）")
            ranked.append((index, float(item["relevance_score"])))
        # 服务端已按分数降序，这里再排一次保证契约 + 平局决胜可复现
        ranked.sort(key=lambda pair: (-pair[1], pair[0]))
        return ranked

    def close(self) -> None:
        """释放自建的 httpx 连接池（注入的 client 归调用方管）。"""
        if self._owns_client:
            self._client.close()


if __name__ == "__main__":  # §8-1 冒烟入口：拿到 key 后一次实测请求/响应原文
    import json
    import sys

    reranker = SiliconFlowReranker()
    query = sys.argv[1] if len(sys.argv) > 1 else "各商品类目的销售额是多少"
    docs = sys.argv[2:] or ["order_items 表：订单明细，一行 = 订单里的一种商品", "users 表：用户注册信息"]
    print(f"POST {reranker.base_url}/rerank  model={reranker.model}")
    print(f"query={query!r}  documents({len(docs)})={docs}")
    results = reranker.rerank(query, docs)
    print("results（index, relevance_score，降序）:")
    for index, score in results:
        print(f"  [{index}] {score:.6f}  {docs[index][:60]}")
    print("\n原始响应字段核对：", json.dumps({"index": "int", "relevance_score": "float"}))
