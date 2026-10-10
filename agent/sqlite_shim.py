"""SQLite 版本垫片：Chroma 要求系统 SQLite ≥ 3.35。

服务器（alinux3）的 python3.11 链接的是系统老版 sqlite，chromadb 导入即报错。
官方修复方案（docs.trychroma.com troubleshooting）：安装 pysqlite3-binary，
并在任何 chromadb 导入之前用本模块把 sqlite3 指向 pysqlite3。

使用方式：在 server.py 的最顶部 `import agent.sqlite_shim  # noqa: F401`。
本地开发环境（sqlite 版本达标或未装 pysqlite3）导入本模块是无操作——两种环境一套代码。
"""

import sqlite3 as _stdlib_sqlite3

if _stdlib_sqlite3.sqlite_version_info < (3, 35, 0):
    try:
        __import__("pysqlite3")
        import sys

        sys.modules["sqlite3"] = sys.modules.pop("pysqlite3")
    except ImportError:
        # 既版本不达标又没有 pysqlite3 兜底——保持现状，让 chromadb 自己报出明确错误
        pass
