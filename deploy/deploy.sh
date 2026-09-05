#!/usr/bin/env bash
# 更新线上：拉最新代码 → 重建并重启容器（GitHub Actions deploy.yml 在 CI 通过后也会调用它）
set -euo pipefail
APP_DIR="${APP_DIR:-/opt/webApp/devflow}"
BRANCH="${BRANCH:-main}"

cd "$APP_DIR"
git fetch -q origin "$BRANCH"
git reset -q --hard "origin/$BRANCH"
docker compose up -d --build --remove-orphans
for _ in $(seq 1 40); do curl -fsS http://127.0.0.1:8765/health >/dev/null 2>&1 && break; sleep 3; done
curl -fsS http://127.0.0.1:8765/health && echo " ✅ $(git log -1 --format='%h %s')"
docker image prune -f >/dev/null
