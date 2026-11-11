#!/usr/bin/env bash
# Ubuntu 22.04/24.04 一键部署脚本（在仓库根目录执行：sudo bash server/deploy/install.sh）
# 仅负责安装 Node/Nginx、部署代码、注册 systemd 服务；域名与证书需按 README 手动配置。
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "请使用 sudo 运行：sudo bash server/deploy/install.sh"
  exit 1
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TARGET="/opt/huiyingzhai/server"
APP_USER="huiyingzhai"

echo "==> 安装基础软件"
apt-get update
apt-get install -y nginx curl rsync ca-certificates build-essential python3 sqlite3

NODE_MAJOR="$(node -p 'process.versions.node.split(".")[0]' 2>/dev/null || echo 0)"
if [[ "${NODE_MAJOR}" -lt 22 ]]; then
  echo "==> 安装 Node.js 22 LTS"
  curl -fsSL https://deb.nodesource.com/setup_22.x | bash -
  apt-get install -y nodejs
fi

echo "==> 创建服务用户与目录"
id -u "${APP_USER}" >/dev/null 2>&1 || \
  useradd --system --home /opt/huiyingzhai --shell /usr/sbin/nologin "${APP_USER}"
mkdir -p "${TARGET}" "${TARGET}/data" "${TARGET}/uploads"

echo "==> 同步代码到 ${TARGET}"
rsync -a --delete \
  --exclude node_modules --exclude data --exclude uploads --exclude .env \
  "${REPO_ROOT}/server/" "${TARGET}/"
chown -R "${APP_USER}:${APP_USER}" /opt/huiyingzhai

echo "==> 安装依赖"
cd "${TARGET}"
if [[ -f package-lock.json ]]; then
  sudo -u "${APP_USER}" npm ci --omit=dev
else
  sudo -u "${APP_USER}" npm install --omit=dev
fi

echo "==> 导入学号名单"
if [[ -f "${REPO_ROOT}/roster_import.json" ]]; then
  sudo -u "${APP_USER}" node scripts/import-roster.js "${REPO_ROOT}/roster_import.json"
else
  echo "未找到 ${REPO_ROOT}/roster_import.json，跳过名单导入（之后可手动执行 npm run seed:roster）"
fi

if [[ ! -f "${TARGET}/.env" ]]; then
  cp "${TARGET}/.env.example" "${TARGET}/.env"
  chown "${APP_USER}:${APP_USER}" "${TARGET}/.env"
  chmod 600 "${TARGET}/.env"
  NEED_ENV=1
else
  NEED_ENV=0
fi

echo "==> 注册 systemd 服务"
install -m 644 "${TARGET}/deploy/huiyingzhai.service" /etc/systemd/system/huiyingzhai.service
systemctl daemon-reload
systemctl enable --now huiyingzhai

if [[ "${NEED_ENV}" -eq 1 ]]; then
  cat <<EOF

部署脚本已完成，但还需要：

1. 编辑环境变量：sudo nano ${TARGET}/.env
   填写 WX_APPID、WX_SECRET、BASE_URL，并将 WX_PUSH_STATE 设为实际版本
2. 修改 Nginx 配置并启用：
   sudo cp ${TARGET}/deploy/nginx.conf /etc/nginx/sites-available/huiyingzhai
   sudo nano /etc/nginx/sites-available/huiyingzhai   # 替换 api.example.com
   sudo ln -s /etc/nginx/sites-available/huiyingzhai /etc/nginx/sites-enabled/
   sudo nginx -t && sudo systemctl reload nginx
3. 申请 HTTPS 证书：
   sudo apt-get install -y certbot python3-certbot-nginx
   sudo certbot --nginx -d 你的域名
4. 重启服务：sudo systemctl restart huiyingzhai
5. 本机自检：curl http://127.0.0.1:3000/api/health

详见 server/README.md
EOF
else
  echo "检测到已有 .env，未覆盖。请确认配置后执行：sudo systemctl restart huiyingzhai"
fi
