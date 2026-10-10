#!/usr/bin/env bash
# 服务器侧 env.prod 生成（W4 设计 §5.5——手工放置原则的自动化版）
# 在服务器上以 root 执行一次：bash /opt/text2sql/server_env_setup.sh
# 原则：所有密钥运行时随机生成/从已有凭据文件提取，不经手任何第三方传输
set -e

CRED=/opt/text2sql/data/mysql_credentials.txt
OUT=/opt/text2sql/env.prod
[ -f "$CRED" ] || { echo "缺少 $CRED（MySQL 切换脚本生成）"; exit 1; }
[ -f "$OUT" ] && { echo "env.prod 已存在，跳过（如需重生成请先删除 $OUT）"; exit 0; }

# 提取 MySQL 切换时已创建账号的真实密码（重跑脚本不得重新随机，否则与 CREATE USER 不匹配）
APP_PW=$(grep "^text2sql_app" "$CRED" | awk '{print $3}')
RO_PW=$(grep "^text2sql_ro" "$CRED" | awk '{print $3}')
[ -n "$APP_PW" ] || { echo "credentials 文件里没有 text2sql_app 密码"; exit 1; }
[ -n "$RO_PW" ] || { echo "credentials 文件里没有 text2sql_ro 密码"; exit 1; }
JWT=$(openssl rand -base64 48 | tr -d "\n")
BYOK=$(openssl rand -base64 32 | tr -d "\n")  # 必须恰好 32 字节（256 位），Java 侧 fail-fast 校验
INTERNAL=$(openssl rand -hex 24)

cat > "$OUT" <<EOF
# Text-to-SQL 生产环境变量（机密！chmod 600，不进 git、不进发布包）
# 语义见 W4 设计 §5.5；BYOK_MASTER_KEY 一旦启用不可更换（换 = 全员重录 key）
MYSQL_URL=jdbc:mysql://127.0.0.1:3306/app?useUnicode=true&characterEncoding=utf8&serverTimezone=Asia/Shanghai&useSSL=false&allowPublicKeyRetrieval=true

# —— Java（app 库读写）——
MYSQL_APP_USER=text2sql_app
MYSQL_APP_PASSWORD=$APP_PW

# —— Python（mart 库只读）——
MYSQL_RO_USER=text2sql_ro
MYSQL_RO_PASSWORD=$RO_PW

# —— 站点密钥 ——
JWT_SECRET=$JWT
BYOK_MASTER_KEY=$BYOK
INTERNAL_TOKEN=$INTERNAL
# ⚠️ BYOK 原则：本文件【绝不】包含 DEEPSEEK_API_KEY（服务端不留站点级 key）
EOF
chmod 600 "$OUT"
echo "env.prod 已生成（600 权限）。核对：$(grep -c DEEPSEEK_API_KEY "$OUT") 处 DEEPSEEK_API_KEY（应为 0）"
