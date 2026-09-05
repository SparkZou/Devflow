#!/usr/bin/env bash
# ============================================================
# DevFlow AI 服务器安装 / 更新（Ubuntu + Docker + 共享 Caddy）
# 用 ubuntu 用户运行（需在 docker 组），重复运行是安全的：已有的 conf/config.yaml、.env、证书不会被覆盖。
#
#   curl -fsSL https://raw.githubusercontent.com/SparkZou/Devflow/main/deploy/install.sh | bash
#
# 环境变量：
#   DOMAIN=devflow.aicloud.co.nz    域名（DNS 必须已指向本机）
#   BRANCH=main                     部署分支
#   APP_DIR=/opt/webApp/devflow     代码 + 数据目录
#   CADDY_DIR=/opt/webApp/caddy     共享 Caddy 目录（没有的话跳过反代，见 deploy/nginx.conf）
# ============================================================
set -euo pipefail

DOMAIN="${DOMAIN:-devflow.aicloud.co.nz}"
REPO="${REPO:-https://github.com/SparkZou/Devflow.git}"
BRANCH="${BRANCH:-main}"
APP_DIR="${APP_DIR:-/opt/webApp/devflow}"
CADDY_DIR="${CADDY_DIR:-/opt/webApp/caddy}"

log()  { printf '\n\033[1;32m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m!!  %s\033[0m\n' "$*"; }
command -v docker >/dev/null || { echo "需要 Docker：https://docs.docker.com/engine/install/ubuntu/"; exit 1; }
docker compose version >/dev/null 2>&1 || { echo "需要 docker compose 插件（docker-compose-plugin）"; exit 1; }

log "代码 → $APP_DIR ($BRANCH)"
if [ -d "$APP_DIR/.git" ]; then
  git -C "$APP_DIR" fetch -q origin "$BRANCH"
  git -C "$APP_DIR" checkout -q "$BRANCH"
  git -C "$APP_DIR" reset -q --hard "origin/$BRANCH"
else
  parent="$(dirname "$APP_DIR")"
  [ -w "$parent" ] || sudo install -d -o "$(id -u)" -g "$(id -g)" "$parent"
  git clone -q -b "$BRANCH" "$REPO" "$APP_DIR"
fi
cd "$APP_DIR"
mkdir -p conf data repos
[ -f conf/config.yaml ] || cp deploy/config.server.yaml conf/config.yaml
[ -f .env ] || cp .env.example .env
chmod 600 conf/config.yaml .env

log "构建并启动容器（首次要几分钟：下载 Chromium 和 Claude Code）"
docker compose up -d --build --remove-orphans
for _ in $(seq 1 40); do curl -fsS http://127.0.0.1:8765/health >/dev/null 2>&1 && break; sleep 3; done
if ! curl -fsS http://127.0.0.1:8765/health; then
  warn "容器没起来，看日志：cd $APP_DIR && docker compose logs --tail 100"
  exit 1
fi
echo

if [ -f "$CADDY_DIR/Caddyfile" ]; then
  log "共享 Caddy 加站点 $DOMAIN（自动申请 HTTPS 证书）"
  if ! grep -q "^$DOMAIN" "$CADDY_DIR/Caddyfile"; then
    cp "$CADDY_DIR/Caddyfile" "$CADDY_DIR/Caddyfile.bak.pre-devflow"
    { echo; sed "s/devflow\.aicloud\.co\.nz/$DOMAIN/" deploy/Caddyfile.snippet; } >> "$CADDY_DIR/Caddyfile"
  fi
  ( cd "$CADDY_DIR" \
    && docker compose exec -T caddy caddy validate --config /etc/caddy/Caddyfile >/dev/null \
    && docker compose exec -T caddy caddy reload --config /etc/caddy/Caddyfile )
  sleep 5
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 "https://$DOMAIN/health" || true)
  [ "$code" = "200" ] && echo "https://$DOMAIN/health → 200 ✅" || warn "https://$DOMAIN 还没通（$code）；证书可能还在签发，稍等再试或看：cd $CADDY_DIR && docker compose logs --tail 50"
else
  warn "没找到 $CADDY_DIR/Caddyfile：请自行把 $DOMAIN 反代到 127.0.0.1:8765（nginx 参考 deploy/nginx.conf）"
fi

cat <<EOF

============================================================
 DevFlow AI 已部署：https://$DOMAIN
 登录：admin / （见 $APP_DIR/conf/config.yaml 的 auth 段）

 让流水线跑起来还需要在容器里登录一次（面板本身不需要）：
   cd $APP_DIR
   docker compose exec devflow gh auth login        # GitHub
   docker compose exec devflow claude               # Claude Code 登录（或在 .env 写 ANTHROPIC_API_KEY 后 docker compose up -d）
   vi conf/config.yaml                              # projects 照本机的写，repo_path 用 /opt/repos/<repo>；仓库缺失时首次用到会自动 clone

 更新代码：bash $APP_DIR/deploy/deploy.sh      日志：cd $APP_DIR && docker compose logs -f
============================================================
EOF
