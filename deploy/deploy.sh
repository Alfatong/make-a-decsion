#!/usr/bin/env bash
# 银发 AI 互动小说 · 一键部署脚本（在腾讯云服务器上以 root 执行）
# 用法: bash deploy.sh
set -euo pipefail

APP_DIR="/opt/novel-app"
REPO="https://github.com/Alfatong/make-a-decsion.git"
DOMAIN="xiaoshuo.neigeshei.com"

echo "==> [1/6] 安装 Docker 与依赖"
if ! command -v docker >/dev/null 2>&1; then
  curl -fsSL https://get.docker.com | bash
  systemctl enable --now docker
fi
if ! docker compose version >/dev/null 2>&1; then
  apt-get update && apt-get install -y docker-compose-plugin || true
fi
command -v git >/dev/null 2>&1 || apt-get install -y git

echo "==> [2/6] 拉取代码"
if [ -d "$APP_DIR/.git" ]; then
  git -C "$APP_DIR" pull
else
  git clone "$REPO" "$APP_DIR"
fi
cd "$APP_DIR"

echo "==> [3/6] 准备环境变量 .env"
if [ ! -f .env ]; then
  cp .env.example .env
  # 生成数据库密码
  PGPW=$(openssl rand -hex 16 2>/dev/null || head -c16 /dev/urandom | od -An -tx1 | tr -d ' \n')
  sed -i "s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=$PGPW|" .env || echo "POSTGRES_PASSWORD=$PGPW" >> .env
  echo "POSTGRES_PASSWORD=$PGPW" >> .env
  echo "  已生成 .env（请按需填入 DEEPSEEK_API_KEY / TENCENT_SECRET_ID/KEY）"
fi

echo "==> [4/6] 准备前端占位与证书目录"
mkdir -p deploy/web deploy/certbot/conf deploy/certbot/www
if [ ! -f deploy/web/index.html ]; then
  cat > deploy/web/index.html <<'HTML'
<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>银发 AI 互动小说</title></head>
<body style="font-family:sans-serif;text-align:center;padding:4rem 1rem;background:#FBF7F0">
<h1>银发 AI 互动小说</h1><p>H5 验证版部署成功 · M1 内容管线开发中</p>
<p style="color:#7C7266">API: <a href="/api/health">/api/health</a></p>
</body></html>
HTML
fi

echo "==> [5/6] 构建并启动服务"
cd deploy
docker compose up -d --build

echo "==> [6/6] 申请 SSL 证书（若尚未签发）"
if [ ! -d "certbot/conf/live/$DOMAIN" ]; then
  docker run --rm -it \
    -v "$PWD/certbot/conf:/etc/letsencrypt" \
    -v "$PWD/certbot/www:/var/www/certbot" \
    certbot/certbot certonly --webroot -w /var/www/certbot \
    -d "$DOMAIN" --agree-tos --no-eff-email \
    -m admin@neigeshei.com --non-interactive || echo "  证书申请失败，可稍后重试"
  docker compose restart nginx || true
fi

echo ""
echo "==> 部署完成"
echo "    健康检查: http://$DOMAIN/api/health"
echo "    查看状态: docker compose -f $APP_DIR/deploy/docker-compose.yml ps"
