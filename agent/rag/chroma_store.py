"""Chroma 持久化客户端与 collection 的模块级单例。

坑清单落实（实战文档 §7）：
- #1 path 用绝对路径（相对仓库根拼出），一个进程只建一次 PersistentClient；
- #2 建库显式指定 space=cosine（默认是 l2，不显式设是新手第一大坑）；
- #3 get_or_create_collection 对已存在的 collection 会忽略新传的 configuration，
  所以改配置 = 删目录重建，不能指望 get_or_create "升级"参数。

path 由 config.CHROMA_PERSIST_DIR 提供（默认 <仓库根>/data/chroma_db，
可用环境变量 CHROMA_PERSIST_DIR 覆盖），config 侧已做绝对化。
"""

import chromadb
from chromadb import Collection

from agent.config import CHROMA_PERSIST_DIR

COLLECTION_NAME = "rag_corpus"

# 持久化路径统一收口到 config（T4 改造：原来是本文件里 parents[2] 拼路径）
CHROMA_PERSIST_PATH = CHROMA_PERSIST_DIR

# HNSW 建库参数：cosine 相似度 + 官方建议的建图质量值（设计 §3.1）
_HNSW_CONFIGURATION = {"hnsw": {"space": "cosine", "ef_construction": 200}}

# 模块级单例缓存（PersistentClient 按 path 缓存单实例，多实例混用会出诡异冲突）
_client = None
_collection: Collection | None = None


def get_client():
    """PersistentClient 单例：一个进程只建一次。"""
    global _client
    if _client is None:
        _client = chromadb.PersistentClient(path=str(CHROMA_PERSIST_PATH))
    return _client


def get_collection() -> Collection:
    """取得（必要时创建）rag_corpus collection，单例。"""
    global _collection
    if _collection is None:
        _collection = get_client().get_or_create_collection(
            name=COLLECTION_NAME,
            configuration=_HNSW_CONFIGURATION,
        )
    return _collection


def recreate_collection() -> Collection:
    """删除并重建 collection（全量重建专用），返回新 collection。

    为什么删除重建而不是增量 upsert（设计 §3.1）：语料条目改名/删除后，
    增量 upsert 清不掉旧 id 的"幽灵片"；28 片全量重建 <1s，天然自愈。
    同时失效 get_collection 的缓存，后续调用拿到的是新 collection。
    """
    global _collection
    client = get_client()
    existing_names = {col.name for col in client.list_collections()}
    if COLLECTION_NAME in existing_names:
        client.delete_collection(COLLECTION_NAME)
    _collection = client.get_or_create_collection(
        name=COLLECTION_NAME,
        configuration=_HNSW_CONFIGURATION,
    )
    return _collection
