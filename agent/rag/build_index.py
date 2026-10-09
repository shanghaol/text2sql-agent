"""把 data/rag_corpus 语料解析成 chunk，并删除重建 Chroma 索引。

用法：python -m agent.rag.build_index

切分与 id 规则（W2 设计 §2.5）：
- schema_docs/*.md：每文件 1 片，id = schema:<表名>，title = 文件内第一个
  "# 表：xxx" 标题行（解析不到就用文件名）；
- glossary.md：按 "## 指标：" 切分，每条 1 片，id = glossary:<english_id>；
  条目标题行格式为 "## 指标：中文名（english_id）"，english_id 从全角括号
  里提取，解析不到时报错退出并列出问题行；
- qa_pairs.md：按 "### QA-" 切分，每条 1 片，id = qa_pair:qa_001 式小写序号，
  title = 该条"问"字段内容（截 50 字）；
- metadata 四字段 doc_type/source_id/title/table_name，缺失用 "" 不用 None
  （Chroma metadata 只收标量）。

为什么"删除重建"（设计 §3.1）：增量 upsert 清不掉改名/删除语料的幽灵片，
全量重建天然自愈，28 片 <1s。
"""

import sys
from pathlib import Path

from agent.rag.chroma_store import recreate_collection
from agent.rag.embedder import BgeEmbedder

REPO_ROOT = Path(__file__).resolve().parents[2]
CORPUS_DIR = REPO_ROOT / "data" / "rag_corpus"

_GLOSSARY_HEADING_PREFIX = "## 指标："
_QA_HEADING_PREFIX = "### QA-"
_QA_TITLE_MAX_CHARS = 50


def _split_entries(text: str, heading_prefix: str) -> list[tuple[int, str, str]]:
    """按标题行前缀切分文档。

    返回 (起始行号 1-based, 标题行, 条目全文含标题行) 列表；
    没有命中标题行的前置内容（如文件总标题）被丢弃。

    边界规则：条目正文在两种情况下结束——
    ① 遇到下一个同级标题（heading_prefix）；
    ② 遇到 `## ` 级别的章节标题（如"## 待真库核对清单"）。
    规则 ② 防止"最后一条条目切到文件尾"把文件末尾附加内容
    （核对清单、金标答案等非语料内容）粘进最后一个语料片
    ——曾导致 W3 金标答案进入 RAG 索引（见 docs/qa_verification.md 头注）。
    """
    lines = text.splitlines()
    starts = [i for i, line in enumerate(lines) if line.startswith(heading_prefix)]
    entries: list[tuple[int, str, str]] = []
    for n, start in enumerate(starts):
        end = len(lines)
        if n + 1 < len(starts):
            end = starts[n + 1]
        else:
            # 最后一条：在剩余行里找第一个非本前缀的 `## ` 章节标题作为终止边界
            for j in range(start + 1, len(lines)):
                if lines[j].startswith("## ") and not lines[j].startswith(heading_prefix):
                    end = j
                    break
        entry_text = "\n".join(lines[start:end]).strip()
        entries.append((start + 1, lines[start], entry_text))
    return entries


def _parse_schema_docs(corpus_dir: Path) -> list[dict]:
    """schema_docs/*.md → 每文件 1 片（表结构强内聚，按表切不分）。"""
    schema_dir = corpus_dir / "schema_docs"
    if not schema_dir.is_dir():
        return []
    chunks: list[dict] = []
    for md_file in sorted(schema_dir.glob("*.md")):
        source_id = md_file.stem
        text = md_file.read_text(encoding="utf-8-sig")
        title = source_id  # 解析不到标题行时的兜底：用文件名
        for line in text.splitlines():
            if line.startswith("# 表："):
                title = line.lstrip("#").strip()
                break
        chunks.append(
            {
                "id": f"schema:{source_id}",
                "doc_type": "schema",
                "source_id": source_id,
                "title": title,
                "table_name": source_id,
                "text": text.strip(),
            }
        )
    return chunks


def _parse_glossary(corpus_dir: Path) -> list[dict]:
    """glossary.md → 每条指标 1 片；english_id 缺失时 ValueError 并列出问题行。"""
    glossary_file = corpus_dir / "glossary.md"
    if not glossary_file.is_file():
        return []
    text = glossary_file.read_text(encoding="utf-8-sig")
    chunks: list[dict] = []
    problems: list[str] = []
    for line_no, heading, entry_text in _split_entries(text, _GLOSSARY_HEADING_PREFIX):
        title = heading.removeprefix(_GLOSSARY_HEADING_PREFIX).strip()
        # english_id 在全角括号里，如 "## 指标：成交总额（gmv）"
        english_id = ""
        open_idx = title.find("（")
        close_idx = title.find("）")
        if 0 <= open_idx < close_idx:
            english_id = title[open_idx + 1 : close_idx].strip()
        if not english_id:
            problems.append(
                f"  glossary.md 第 {line_no} 行：{heading!r}"
                "（条目标题缺少全角括号里的英文 id，应为 ## 指标：中文名（english_id））"
            )
            continue
        chunks.append(
            {
                "id": f"glossary:{english_id}",
                "doc_type": "glossary",
                "source_id": english_id,
                "title": title,
                "table_name": "",
                "text": entry_text,
            }
        )
    if problems:
        raise ValueError("以下 glossary 条目解析不到 english_id：\n" + "\n".join(problems))
    return chunks


def _parse_qa_pairs(corpus_dir: Path) -> list[dict]:
    """qa_pairs.md → 每条问答对 1 片（注入单位 = 一个完整示例）。"""
    qa_file = corpus_dir / "qa_pairs.md"
    if not qa_file.is_file():
        return []
    text = qa_file.read_text(encoding="utf-8-sig")
    chunks: list[dict] = []
    for _line_no, heading, entry_text in _split_entries(text, _QA_HEADING_PREFIX):
        raw_id = heading.removeprefix("### ").strip()  # 如 "QA-001"
        source_id = raw_id.lower().replace("-", "_")  # 统一成 qa_001 式小写序号
        title = ""
        for line in entry_text.splitlines():
            if line.startswith("**问**"):
                title = line.removeprefix("**问**").lstrip("：:").strip()
                title = title[:_QA_TITLE_MAX_CHARS]
                break
        chunks.append(
            {
                "id": f"qa_pair:{source_id}",
                "doc_type": "qa_pair",
                "source_id": source_id,
                "title": title,
                "table_name": "",
                "text": entry_text,
            }
        )
    return chunks


def parse_corpus(corpus_dir: Path) -> list[dict]:
    """解析语料目录为 chunk 列表（纯函数，供单测）。

    单个语料文件/目录缺失时跳过该类型（比如先写好 schema 先建索引）；
    完全没有片数由调用方（main）给出指引。
    """
    return [
        *_parse_schema_docs(corpus_dir),
        *_parse_glossary(corpus_dir),
        *_parse_qa_pairs(corpus_dir),
    ]


def main() -> None:
    if not CORPUS_DIR.is_dir():
        print(f"未找到语料目录：{CORPUS_DIR}")
        print(
            "请先编写 data/rag_corpus 语料"
            "（schema_docs/*.md、glossary.md、qa_pairs.md），"
            "再运行 python -m agent.rag.build_index 建索引。"
        )
        sys.exit(1)

    try:
        chunks = parse_corpus(CORPUS_DIR)
    except ValueError as exc:
        print(f"语料解析失败，请修正后重试：\n{exc}")
        sys.exit(1)
    if not chunks:
        print(f"语料目录 {CORPUS_DIR} 中没有解析到任何片段。")
        print(
            "请先编写 data/rag_corpus 语料"
            "（schema_docs/*.md、glossary.md、qa_pairs.md），"
            "再运行 python -m agent.rag.build_index 建索引。"
        )
        sys.exit(1)

    # 先算向量再删库：embedding 环节失败（如模型未缓存）时不丢现有索引
    print(f"共解析出 {len(chunks)} 片，开始计算向量 ...")
    embedder = BgeEmbedder()
    embeddings = embedder.embed([chunk["text"] for chunk in chunks])

    print("删除并重建 collection ...")
    collection = recreate_collection()
    collection.upsert(
        ids=[chunk["id"] for chunk in chunks],
        embeddings=embeddings,
        documents=[chunk["text"] for chunk in chunks],
        metadatas=[
            {
                "doc_type": chunk["doc_type"],
                "source_id": chunk["source_id"],
                "title": chunk["title"],
                "table_name": chunk["table_name"],
            }
            for chunk in chunks
        ],
    )

    counts: dict[str, int] = {}
    for chunk in chunks:
        counts[chunk["doc_type"]] = counts.get(chunk["doc_type"], 0) + 1
    print("索引重建完成。各类型片数：")
    for doc_type in sorted(counts):
        print(f"  {doc_type}: {counts[doc_type]} 片")
    print(f"  总计: {len(chunks)} 片")


if __name__ == "__main__":
    main()
