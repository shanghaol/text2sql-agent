"""config 模块 W2 新增配置项的默认套件测试。

重点验证设计 §4.2 的两个约定：
1. MYSQL_PASSWORD 缺失时回退 MYSQL_RO_PASSWORD（本项目的 .env 只维护后者）；
2. CHROMA_PERSIST_DIR 绝对化（Chroma 坑清单 #1：相对路径换启动目录即"丢数据"）。

importlib.reload 只在本文件使用并在用例结束前恢复，
避免影响其他测试已持有的 config 属性引用。
"""

import importlib


def test_mysql_password_uses_ro_var_and_ignores_app_var(monkeypatch):
    """W4 教训回归：Python 只认 MYSQL_RO_PASSWORD；MYSQL_PASSWORD 属于 app 账号，必须忽略。

    生产 env.prod 同时存在两个名字（Java 用 MYSQL_APP_*，Python 用 MYSQL_RO_*），
    若回退链先读 MYSQL_PASSWORD，ro 用户就会配上 app 密码 → 1045（2026-10-11 实录）。
    """
    from agent import config

    monkeypatch.setenv("MYSQL_RO_PASSWORD", "ro-secret")
    monkeypatch.setenv("MYSQL_PASSWORD", "app-secret")
    importlib.reload(config)
    assert config.MYSQL_PASSWORD == "ro-secret"

    monkeypatch.delenv("MYSQL_RO_PASSWORD", raising=False)
    importlib.reload(config)
    # delenv 后 config 的 load_dotenv 会从本地 .env 重新加载真实值——
    # 这里的断言点是"app 密码绝不会被采用"（- - 就是它），而非空串
    assert config.MYSQL_PASSWORD != "app-secret"

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
