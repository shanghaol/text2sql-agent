#!/usr/bin/env bash
# W4 部署脚本（开发机执行；服务器侧仅阶段②③通过 ssh 自动化）
# 前置：DNS A 记录 sql.shanghl.club → SERVER_IP_REDACTED 已生效；ssh aliyun 可达
# 产物原则（W2 教训）：服务器零构建零拉取——jar/dist 由开发机产出 rsync 过去
set -e
REMOTE="aliyun"
REMOTE_DIR="/opt/text2sql"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

echo "=== 阶段① 开发机构建 ==="
echo "--- 1a. Java 打包（跳过测试，测试在 CI/本地已跑）---"
(cd "$REPO_ROOT/backend" && mvn -q package -DskipTests)
echo "--- 1b. 前端构建 ---"
(cd "$REPO_ROOT/frontend" && npm run build)
echo "--- 1c. mart 数据（如需重置服务器数据才执行，默认跳过）---"

echo "=== 阶段② 传输三件套（rsync 增量）==="
rsync -avz --progress "$REPO_ROOT/backend/target/"*.jar "$REMOTE:$REMOTE_DIR/releases/backend/"
rsync -avz --progress "$REPO_ROOT/frontend/dist/" "$REMOTE:/www/text2sql/ui/"
rsync -avz --delete "$REPO_ROOT/agent" "$REMOTE:$REMOTE_DIR/current/" --exclude "__pycache__" --exclude ".venv"
rsync -avz "$REPO_ROOT/deploy/text2sql-python.service" "$REPO_ROOT/deploy/text2sql-java.service" "$REPO_ROOT/deploy/nginx-sql.shanghl.club.conf" "$REMOTE:$REMOTE_DIR/releases/"

echo "=== 阶段③ 服务器侧安装（ssh 远程执行）==="
ssh $REMOTE bash -s <<'REMOTE_SCRIPT'
set -e
echo "--- 3a. Python venv 安装依赖（首次较慢，此后增量）---"
cd /opt/text2sql/current
/opt/text2sql/venv/bin/pip install -q -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt
echo "--- 3b. Java jar 放位 ---"
cp /opt/text2sql/releases/backend/text2sql-backend-*.jar /opt/text2sql/current/backend/text2sql-backend.jar
echo "--- 3c. systemd 单元安装 ---"
cp /opt/text2sql/releases/text2sql-python.service /etc/systemd/system/
cp /opt/text2sql/releases/text2sql-java.service /etc/systemd/system/
systemctl daemon-reload
echo "--- 3d. nginx 站点安装（证书若未签发先走 bootstrap）---"
cp /opt/text2sql/releases/nginx-sql.shanghl.club.conf /www/server/panel/vhost/nginx/sql.shanghl.club.conf
mkdir -p /www/text2sql/acme /www/text2sql/ui
nginx -t && nginx -s reload
echo "--- 3e. 证书签发（webroot 模式，需 DNS 已生效）---"
if [ ! -d /etc/letsencrypt/live/sql.shanghl.club ]; then
  yum install -y -q certbot >/dev/null 2>&1 || true
  certbot certonly --webroot -w /www/text2sql/acme -d sql.shanghl.club \
    --email shanghl123@outlook.com --agree-tos --no-eff-email --non-interactive
fi
nginx -s reload
echo "--- 3f. 起服务 ---"
systemctl enable --now text2sql-python
systemctl enable --now text2sql-java
sleep 5
systemctl --no-pager --lines=0 status text2sql-python text2sql-java | grep -E "●|Active"
REMOTE_SCRIPT

echo "=== 阶段④ 网关冒烟（设计 §6.3 的 8 步，输出对照）==="
echo "提示：完整 8 步冒烟见 deploy/gateway_smoke.sh（下一步执行）"
echo "部署完成。访问：https://sql.shanghl.club"
