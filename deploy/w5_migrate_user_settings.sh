#!/usr/bin/env bash
# =============================================================================
# W5 守护迁移：user 表补 hitl_enabled 列（W5 设计 §3.8 / §8-⑮）
#
# 为什么需要这个脚本：
#   - schema-app.sql 是幂等 CREATE TABLE IF NOT EXISTS，对【已存在】的表不补列；
#   - MySQL 8.0 不支持 ADD COLUMN IF NOT EXISTS；
#   → 先查 information_schema.columns，无该列才 ALTER（幂等，可重复执行）。
#
# 用法（服务器上，凭据走环境变量，同 W4 env.prod 命名）：
#   MYSQL_ADMIN_USER=root MYSQL_ADMIN_PASSWORD=... bash w5_migrate_user_settings.sh
#   （未配 ADMIN 时回退 MYSQL_APP_USER/MYSQL_APP_PASSWORD——app 库本就归它读写）
# 验收：连续执行两次，第二次输出"列已存在，跳过"且退出码 0。
# =============================================================================
set -euo pipefail

MYSQL_HOST="${MYSQL_HOST:-127.0.0.1}"
MYSQL_PORT="${MYSQL_PORT:-3306}"
MYSQL_USER="${MYSQL_ADMIN_USER:-${MYSQL_APP_USER:-root}}"
MYSQL_PASSWORD="${MYSQL_ADMIN_PASSWORD:-${MYSQL_APP_PASSWORD:-}}"
DB_NAME="app"
TABLE_NAME="user"
COLUMN_NAME="hitl_enabled"

if [ -z "$MYSQL_PASSWORD" ]; then
  echo "缺少 MySQL 密码：请设置 MYSQL_ADMIN_PASSWORD（或 MYSQL_APP_PASSWORD）" >&2
  exit 1
fi

# 密码经 -p 命令行传给客户端（MYSQL_PWD 实测对该账号 1045，见 experiment-log）

mysql_client() {
  mysql -h"$MYSQL_HOST" -P"$MYSQL_PORT" -u"$MYSQL_USER" -p"$MYSQL_PASSWORD" "$@" 2>/dev/null
}

echo "[w5-migrate] 检查 ${DB_NAME}.${TABLE_NAME}.${COLUMN_NAME} ..."
HAS_COLUMN=$(mysql_client -NBe \
  "SELECT COUNT(*) FROM information_schema.columns \
   WHERE table_schema='${DB_NAME}' AND table_name='${TABLE_NAME}' AND column_name='${COLUMN_NAME}';")

if [ "$HAS_COLUMN" -eq 0 ]; then
  echo "[w5-migrate] 列不存在 → ALTER TABLE 补列"
  mysql_client -e "ALTER TABLE \`${DB_NAME}\`.\`${TABLE_NAME}\` \
    ADD COLUMN \`${COLUMN_NAME}\` TINYINT NOT NULL DEFAULT 0 \
    COMMENT 'SQL 执行前人工确认开关（W5 §3.8：0 关 / 1 开）' AFTER \`password_hash\`;"
  echo "[w5-migrate] 完成：hitl_enabled 已添加（默认 0 = 关）"
else
  echo "[w5-migrate] 列已存在，跳过（幂等）"
fi

# 复核：无论走哪条路径，执行后该列必须存在
FINAL=$(mysql_client -NBe \
  "SELECT COUNT(*) FROM information_schema.columns \
   WHERE table_schema='${DB_NAME}' AND table_name='${TABLE_NAME}' AND column_name='${COLUMN_NAME}';")
if [ "$FINAL" -ne 1 ]; then
  echo "[w5-migrate] 复核失败：列仍不存在" >&2
  exit 1
fi
echo "[w5-migrate] 复核通过"
