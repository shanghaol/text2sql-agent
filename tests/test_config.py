"""config 模块 W2 新增配置项的默认套件测试。

重点验证设计 §4.2 的两个约定：
1. MYSQL_PASSWORD 缺失时回退 MYSQL_RO_PASSWORD（本项目的 .env 只维护后者）；
2. CHROMA_PERSIST_DIR 绝对化（Chroma 坑清单 #1：相对路径换启动目录即"丢数据"）。

importlib.reload 只在本文件使用并在用例结束前恢复，
避免影响其他测试已持有的 config 属性引用。
"""

import importlib


def test_mysql_password_prefers_main_var_then_falls_back_to_ro(monkeypatch):
    """有 MYSQL_PASSWORD 用之；没有则回退 MYSQL_RO_PASSWORD（.env 实际变量名）。"""
    from agent import config

    monkeypatch.delenv("MYSQL_PASSWORD", raising=False)
    monkeypatch.setenv("MYSQL_RO_PASSWORD", "ro-secret")
    importlib.reload(config)
    assert config.MYSQL_PASSWORD == "ro-secret"

    monkeypatch.setenv("MYSQL_PASSWORD", "main-secret")
    importlib.reload(config)
    assert config.MYSQL_PASSWORD == "main-secret"

    # 测试结束前恢复真实配置（monkeypatch.undo 后再 reload 回实际环境值）
    monkeypatch.undo()
    importlib.reload(config)


def test_chroma_persist_dir_is_absolute_and_defaulted_to_data_dir():
    from agent.config import CHROMA_PERSIST_DIR, REPO_ROOT

    assert CHROMA_PERSIST_DIR.is_absolute()
    assert CHROMA_PERSIST_DIR == REPO_ROOT / "data" / "chroma_db"


def test_retriever_top_n_is_positive_int():
    from agent.config import RETRIEVER_TOP_N

    assert isinstance(RETRIEVER_TOP_N, int)
    assert RETRIEVER_TOP_N >= 1


def test_embedding_model_default_is_bge_small_zh():
    from agent.config import EMBEDDING_MODEL

    assert EMBEDDING_MODEL == "BAAI/bge-small-zh-v1.5"
