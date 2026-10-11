#!/usr/bin/env bash
# W5 反馈自举语料上线（设计 §4.8）：开发机执行、手动触发的语料同步脚本。
#
# 为什么需要专门脚本：deploy.sh 的 rsync 只同步 agent/ 目录、不同步 data/（W2 教训：
# 数据与代码分离），语料更新必须走这里。
#
# 使用顺序（每一步都是人的动作，脚本不代做第 0 步）：
#   0. 本地：python -m agent.rag.absorb_feedback --dry-run 人工过目 → --apply 收录
#      → git commit data/rag_corpus/qa_pairs.md（版本化回滚点，§4.5-3；本脚本启动时检查未提交改动）
#   1. rsync qa_pairs.md → 服务器 /opt/text2sql/current/data/rag_corpus/
#   2. ssh 服务器重建 Chroma 索引（fastembed 缓存已固化在服务器，W4 实录；纯本地计算）
#   3. systemctl restart text2sql-python   ← 必须，见下方注释
#   4. 冒烟：curl /agent/health 看 chromaChunks 是否 = 28+k
#
# 【为什么必须重启 text2sql-python（W2 坑 #3 的变体，踩过一次别再踩）】
#   build_index 的 recreate_collection() 会删除并重建 collection，但**已运行进程**里的
#   get_collection() 是进程内单例（chroma_store._collection 缓存），仍指向旧的
#   collection 对象——不重启的话 Python 服务的检索继续走旧索引，新旧并存直到
#   下次重启，表现为"语料明明收录了、线上却检索不到"，且无报错、极难排查。
#   停机窗口 = 单 worker 重启的几秒，演示规模可接受。
#
# 前置：ssh aliyun 可达；W6 消融红线（§4.7-4）：冻结语料快照上的基线必须先跑完，
# 再吸收新样本，每次 absorb 后把语料版本（qa 片数 + git commit）记入 experiment-log。
set -e

REMOTE="aliyun"
REMOTE_DIR="/opt/text2sql"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CORPUS_FILE="$REPO_ROOT/data/rag_corpus/qa_pairs.md"
HEALTH_URL="http://127.0.0.1:8000/agent/health"   # Python 服务绑 127.0.0.1，冒烟走服务器本机

[ -f "$CORPUS_FILE" ] || { echo "未找到语料文件：$CORPUS_FILE"; exit 1; }

echo "=== 步骤 0：本地语料状态检查 ==="
if [ -n "$(git -C "$REPO_ROOT" status --porcelain -- data/rag_corpus/qa_pairs.md)" ]; then
  echo "qa_pairs.md 有未提交改动。请先人工确认内容并 git commit（语料版本化回滚点，§4.5-3）："
  git -C "$REPO_ROOT" status --short -- data/rag_corpus/qa_pairs.md
  exit 1
fi
echo "qa_pairs.md 已提交：$(git -C "$REPO_ROOT" log -1 --format='%h %s' -- data/rag_corpus/qa_pairs.md)"

echo "=== 步骤 1：同步语料到服务器 ==="
rsync -avz "$CORPUS_FILE" "$REMOTE:$REMOTE_DIR/current/data/rag_corpus/qa_pairs.md"

echo "=== 步骤 2：服务器重建 Chroma 索引 ==="
# 在服务器重建而不是开发机重建后拷 chroma_db（§4.8 被否备选）：持久化目录含
# sqlite + HNSW 二进制，跨机拷贝有一致性风险（须停服务拷）；fastembed 缓存
# 已固化在服务器（FASTEMBED_CACHE_DIR），重建是纯本地计算，28+k 片秒级。
ssh "$REMOTE" "cd $REMOTE_DIR/current && /opt/text2sql/venv/bin/python -m agent.rag.build_index"

echo "=== 步骤 3：重启 text2sql-python（单例持旧 collection 引用，必须重启才走新索引） ==="
ssh "$REMOTE" "systemctl restart text2sql-python && sleep 5 && systemctl --no-pager --lines=0 status text2sql-python | grep -E '●|Active'"

echo "=== 步骤 4：冒烟（health 探针的 chromaChunks 应 = 重建后的总片数） ==="
ssh "$REMOTE" "curl -s $HEALTH_URL"
echo ""
echo "完成。请在开发机跑 eval smoke 确认链路：python -m eval.run_eval --tag absorb_smoke --limit 2 --runs 1"
echo "（W6 前记得跑全量基线并把语料版本记入 experiment-log，§4.7-4）"
