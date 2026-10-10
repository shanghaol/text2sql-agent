#!/usr/bin/env bash
# MySQL 5.7(宝塔 bt-mysql57) → 8.0(alinux3 mysql-server) 切换脚本
# 服务对象：相册平台(album_maker) + Text-to-SQL(app/mart 新库)
# 回滚：/etc/my.cnf.57-pre8 与 /opt/text2sql/data/mysql57-datadir-cold.tar.gz 保留在案
# 运行：以 root 在服务器上执行；分两段调用（段A=切换安装，段B=建库导入+验证+起站点）
set -u

MODE="${1:?用法: $0 A|B}"

if [ "$MODE" = "A" ]; then
  echo "=== [A1] 停相册平台（停机窗口开始）==="
  systemctl stop spring_album-maker
  echo "=== [A2] 停 MySQL 5.7 ==="
  systemctl stop mysqld
  sleep 2
  echo "=== [A3] 冷备 datadir（永久保留，回滚资产）==="
  tar czf /opt/text2sql/data/mysql57-datadir-cold.tar.gz -C /www/server data
  ls -la /opt/text2sql/data/mysql57-datadir-cold.tar.gz
  echo "=== [A4] 切换 my.cnf（5.7 配置备份为 .57-pre8）==="
  [ -f /etc/my.cnf.57-pre8 ] || cp /etc/my.cnf /etc/my.cnf.57-pre8
  cat > /etc/my.cnf <<'CNF'
[client]
port=3306
socket=/var/lib/mysql/mysql.sock

[mysqld]
port=3306
datadir=/var/lib/mysql
socket=/var/lib/mysql/mysql.sock
bind-address=127.0.0.1
innodb_buffer_pool_size=128M
character-set-server=utf8mb4
collation-server=utf8mb4_general_ci
performance_schema=OFF
max_connections=100
log-error=/var/log/mysqld.log
slow-query-log
slow-query-log-file=/var/log/mysql-slow.log
long_query_time=2
CNF
  chmod -x /etc/init.d/mysqld 2>/dev/null || true
  echo "=== [A5] 安装 mysql-server 8.0（alinux3 源）==="
  yum install -y mysql-server >/dev/null
  systemctl daemon-reload
  echo "mysqld unit 来源：$(systemctl show -p FragmentPath mysqld --value)"
  echo "=== [A6] 初始化数据目录并启动 ==="
  if [ -z "$(ls -A /var/lib/mysql 2>/dev/null)" ]; then
    mysqld --initialize-insecure --user=mysql
  fi
  systemctl enable --now mysqld
  for i in 1 2 3 4 5 6 7 8 9 10; do
    sleep 3
    mysqladmin --protocol=socket ping >/dev/null 2>&1 && break
    echo "等待 mysqld 就绪 ($i)"
  done
  mysqladmin --protocol=socket ping && echo "MySQL 8.0 已就绪"
  echo "=== 段A 完成 ==="

elif [ "$MODE" = "B" ]; then
  echo "=== [B1] root 密码与内部账号（运行时生成，落 600 权限文件）==="
  ROOTPW=$(openssl rand -hex 16)
  ROPW=$(openssl rand -hex 12)
  APPPW=$(openssl rand -hex 12)
  mysql -uroot -e "ALTER USER 'root'@'localhost' IDENTIFIED BY '$ROOTPW';"
  cat > /root/.my.cnf <<CNF2
[client]
user=root
password=$ROOTPW
socket=/var/lib/mysql/mysql.sock
CNF2
  chmod 600 /root/.my.cnf
  echo "=== [B2] 建库建账号 + 导入 album_maker ==="
  mysql -e "CREATE DATABASE album_maker;
    CREATE USER 'album_maker'@'localhost' IDENTIFIED BY '651d2684bf';
    GRANT ALL PRIVILEGES ON album_maker.* TO 'album_maker'@'localhost';
    CREATE DATABASE app;
    CREATE DATABASE mart;
    CREATE USER 'text2sql_ro'@'%' IDENTIFIED BY '$ROPW';
    GRANT SELECT ON mart.* TO 'text2sql_ro'@'%';
    CREATE USER 'text2sql_app'@'localhost' IDENTIFIED BY '$APPPW';
    GRANT ALL PRIVILEGES ON app.* TO 'text2sql_app'@'localhost';"
  gunzip -c /opt/text2sql/data/album_maker_5.7_dump.sql.gz | mysql album_maker
  cat > /opt/text2sql/data/mysql_credentials.txt <<EOF2
# MySQL 8.0（native 3306）凭证清单（机密，勿外传）— 生成于 $(date '+%F %T')
root          : 见 /root/.my.cnf
text2sql_ro   : $ROPW   （SELECT ON mart.*，Python Agent 只读账号）
text2sql_app  : $APPPW   （ALL ON app.*，Java 后端账号）
album_maker   : 651d2684bf（原密码重建，相册平台使用）
EOF2
  chmod 600 /opt/text2sql/data/mysql_credentials.txt
  echo "=== [B3] 完整性核对（与切换前逐表行数比对）==="
  for t in project_pages projects stickers template_pages template_zones templates users; do
    c=$(mysql -N -e "SELECT COUNT(*) FROM album_maker.\`$t\`")
    echo "$t=$c"
  done | tee /opt/text2sql/data/album_maker_rowcounts_after.txt
  diff /opt/text2sql/data/album_maker_rowcounts_before.txt \
       /opt/text2sql/data/album_maker_rowcounts_after.txt \
    && echo "行数一致 ✓" || echo "行数不一致 ✗ 需人工核查"
  mysql -N -e "SELECT VERSION()"
  echo "=== [B4] 相册平台堆上限 1024M→512M 并启动 ==="
  UNIT=$(systemctl show -p FragmentPath spring_album-maker --value)
  echo "unit 文件: $UNIT"
  sed -i 's/-Xmx1024M/-Xmx512M/g' "$UNIT"
  systemctl daemon-reload
  systemctl start spring_album-maker
  sleep 10
  CODE=$(curl -sk -o /dev/null -w "%{http_code}" https://shiguang.shanghl.club/ 2>/dev/null || true)
  CODE_LOCAL=$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8080/ 2>/dev/null || true)
  echo "相册平台 HTTPS=$CODE LOCAL8080=$CODE_LOCAL"
  echo "=== [B5] 服务状态 ==="
  systemctl --no-pager --lines=0 status mysqld spring_album-maker 2>/dev/null | grep -E "●|Active"
  free -m | head -2
  echo "=== 段B 完成 ==="
else
  echo "用法: $0 A|B"
fi
