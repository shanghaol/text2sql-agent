"""fastembed 的极薄封装：BgeEmbedder。

设计要点（W2 设计 §3.2）：
- 模型在构造时加载一次（TextEmbedding 加载约几秒），进程内复用；
  模块级单例由调用方控制（core/agent.py T4 接入时做懒加载单例）。
- embed() 给文档算向量、embed_query() 给查询算向量——查询侧自动拼
  bge 指令前缀、文档侧不加。两处口径在 W2 就统一，W6 消融不再作为变量。
- fastembed 是"按需"依赖：import 本模块不会加载 fastembed（惰性导入），
  纯本地测试/语料解析不因缺模型而受影响。
"""

DIM = 512  # BAAI/bge-small-zh-v1.5 输出维度


class BgeEmbedder:
    """bge-small-zh-v1.5 的 embed / embed_query 薄接口。"""

    # bge 系模型做检索时查询侧要加的指令前缀（文档侧不加，坑清单 #9）
    QUERY_INSTRUCTION_PREFIX = "为这个句子生成表示以检索相关文章："

    DEFAULT_MODEL_NAME = "BAAI/bge-small-zh-v1.5"

    def __init__(self, model_name: str = DEFAULT_MODEL_NAME, cache_dir: str | None = None) -> None:
        # 惰性导入：只在真正实例化（= 真要算向量）时才加载 fastembed。
        # TextEmbedding(model_name=...) 构造即完成模型加载。
        # cache_dir：模型缓存目录。不传时 fastembed 用默认（Windows 是 %TEMP% 下，
        # 生产不可靠——W4 部署实录：服务器 %TEMP% 重启清空 → 重新向 HuggingFace
        # 下载 → 国内网络挂死，服务假死）。生产由 env FASTEMBED_CACHE_DIR 指到稳定路径。
        from fastembed import TextEmbedding

        self.model_name = model_name
        kwargs: dict = {"model_name": model_name}
        if cache_dir:
            kwargs["cache_dir"] = cache_dir
        self._model = TextEmbedding(**kwargs)

    def embed(self, docs: list[str]) -> list[list[float]]:
        """文档向量：不加指令前缀。fastembed 返回可迭代对象，转成 list。"""
        return [vec.tolist() for vec in self._model.embed(docs)]

    def embed_query(self, q: str) -> list[float]:
        """查询单条向量：自动拼指令前缀后编码。"""
        return self.embed([self.QUERY_INSTRUCTION_PREFIX + q])[0]
