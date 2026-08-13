#!/bin/bash
# deploy_ssl.sh — acme.sh 证书更新后自动部署到 nginx 容器
# 由 acme.sh --reloadcmd 调用，也可手动运行
#
# 环境：在宿主机（root@example.com）上运行
# 证书来源：~/.acme.sh/example.com_ecc/
# 部署目标：/opt/ZZLXWeb/SSL/（nginx 容器挂载此目录）

set -e

DOMAIN="example.com"
CERT_DIR="/root/.acme.sh/${DOMAIN}_ecc"
DEST_DIR="/opt/ZZLXWeb/SSL"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] 开始部署 SSL 证书..."

# 复制证书文件（acme.sh 已合并完整链）
cp -f "${CERT_DIR}/fullchain.cer"   "${DEST_DIR}/${DOMAIN}.pem"
cp -f "${CERT_DIR}/${DOMAIN}.key"   "${DEST_DIR}/${DOMAIN}.key"

# 保留旧文件名兼容（nginx.conf 中使用 www.example.com.* 命名）
cp -f "${DEST_DIR}/${DOMAIN}.pem"   "${DEST_DIR}/www.${DOMAIN}.pem"
cp -f "${DEST_DIR}/${DOMAIN}.key"   "${DEST_DIR}/www.${DOMAIN}.key"

# 证书权限：nginx容器 root 读取
chmod 644 "${DEST_DIR}"/*.pem
chmod 600 "${DEST_DIR}"/*.key

echo "[$(date '+%Y-%m-%d %H:%M:%S')] 证书文件已复制到 ${DEST_DIR}"

# 重载 nginx（不重启容器，热加载证书）
docker exec zzlxweb-nginx-1 nginx -s reload
echo "[$(date '+%Y-%m-%d %H:%M:%S')] nginx 已热重载，证书部署完成。"

# 验证证书到期时间
EXPIRE=$(openssl x509 -noout -enddate -in "${DEST_DIR}/${DOMAIN}.pem" 2>/dev/null | cut -d= -f2)
echo "[$(date '+%Y-%m-%d %H:%M:%S')] 新证书到期时间：${EXPIRE}"
