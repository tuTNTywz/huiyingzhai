#!/usr/bin/env bash
# Ubuntu 22.04/24.04 一键部署脚本（在仓库根目录执行：sudo bash server/deploy/install.sh）
# 仅负责安装 Python/Nginx、部署代码、注册 systemd 服务；域名与证书需按 README 手动配置。
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
apt-get install -y nginx curl rsync ca-certificates python3 python3-venv python3-pip sqlite3

if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
  echo "需要 Python 3.10 及以上（Ubuntu 22.04 自带 3.10）"
  exit 1
fi

echo "==> 创建服务用户与目录"
id -u "${APP_USER}" >/dev/null 2>&1 || \
  useradd --system --home /opt/huiyingzhai --shell /usr/sbin/nologin "${APP_USER}"
mkdir -p "${TARGET}" "${TARGET}/data" "${TARGET}/uploads"

echo "==> 同步代码到 ${TARGET}"
rsync -a --delete \
  --exclude .venv --exclude __pycache__ --exclude '*.pyc' \
  --exclude data --exclude uploads --exclude .env \
  "${REPO_ROOT}/server/" "${TARGET}/"
chown -R "${APP_USER}:${APP_USER}" /opt/huiyingzhai

echo "==> 创建虚拟环境并安装依赖"
if [[ ! -x "${TARGET}/.venv/bin/python" ]]; then
  sudo -u "${APP_USER}" python3 -m venv "${TARGET}/.venv"
fi
sudo -u "${APP_USER}" "${TARGET}/.venv/bin/pip" install --upgrade pip
sudo -u "${APP_USER}" "${TARGET}/.venv/bin/pip" install -r "${TARGET}/requirements.txt"

echo "==> 导入学号名单"
if [[ -f "${REPO_ROOT}/roster_import.json" ]]; then
  # 先复制到服务目录并交给服务用户：/home/<user> 可能不可被 huiyingzhai 读取
  install -m 644 -o "${APP_USER}" -g "${APP_USER}" \
    "${REPO_ROOT}/roster_import.json" "${TARGET}/roster_import.json"
  sudo -u "${APP_USER}" "${TARGET}/.venv/bin/python" \
    "${TARGET}/scripts/import_roster.py" "${TARGET}/roster_import.json"
else
  echo "未找到 ${REPO_ROOT}/roster_import.json，跳过名单导入（之后可手动执行 python scripts/import_roster.py）"
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

echo "==> 安装网页部署面板"
PANEL_USER="huiyingzhai-panel"
PANEL_HOME="/var/lib/huiyingzhai-panel"
DEPLOY_STATE_DIR="/var/lib/huiyingzhai-deploy"
PANEL_CONF="/etc/huiyingzhai/panel.env"
DEPLOY_CONF="/etc/huiyingzhai/deploy.conf"

# 面板以独立系统用户运行，只能写请求目录；真正部署由 root 的 systemd 单元执行
id -u "${PANEL_USER}" >/dev/null 2>&1 || \
  useradd --system --user-group --home "${PANEL_HOME}" --shell /usr/sbin/nologin "${PANEL_USER}"

install -d -m 755 /etc/huiyingzhai
install -d -m 755 "${DEPLOY_STATE_DIR}"
install -d -o "${PANEL_USER}" -g "${PANEL_USER}" -m 700 "${PANEL_HOME}"
chown "${PANEL_USER}:${PANEL_USER}" "${PANEL_HOME}"
chmod 700 "${PANEL_HOME}"
touch /var/log/huiyingzhai-deploy.log
chmod 644 /var/log/huiyingzhai-deploy.log

if [[ ! -f "${DEPLOY_CONF}" ]]; then
  install -m 600 -o root -g root \
    "${REPO_ROOT}/server/deploy/deploy.conf.example" "${DEPLOY_CONF}"
fi
chown root:root "${DEPLOY_CONF}"
chmod 600 "${DEPLOY_CONF}"

cat > "${PANEL_CONF}" <<EOF
# 由 install.sh 生成；账号保存在 ${PANEL_HOME}/accounts.json
PANEL_PORT=3999
PANEL_QUEUE_DIR=${PANEL_HOME}
PANEL_ACCOUNTS_FILE=${PANEL_HOME}/accounts.json
PANEL_STATUS_FILE=${DEPLOY_STATE_DIR}/status.json
PANEL_CURRENT_FILE=${DEPLOY_STATE_DIR}/current.json
PANEL_LOG_FILE=/var/log/huiyingzhai-deploy.log
EOF
chown root:root "${PANEL_CONF}"
chmod 600 "${PANEL_CONF}"

# root 拥有并执行部署脚本；面板脚本只需读权限
install -m 755 "${REPO_ROOT}/server/deploy/huiyingzhai-deploy.sh" \
  /usr/local/sbin/huiyingzhai-deploy
install -d -m 755 /usr/local/lib/huiyingzhai
install -m 755 "${REPO_ROOT}/server/deploy/deploy_panel.py" \
  /usr/local/lib/huiyingzhai/deploy_panel.py
install -m 755 "${REPO_ROOT}/server/deploy/manage_accounts.py" \
  /usr/local/lib/huiyingzhai/manage_accounts.py

install -m 644 "${REPO_ROOT}/server/deploy/huiyingzhai-deploy.service" \
  /etc/systemd/system/huiyingzhai-deploy.service
install -m 644 "${REPO_ROOT}/server/deploy/huiyingzhai-deploy.path" \
  /etc/systemd/system/huiyingzhai-deploy.path
install -m 644 "${REPO_ROOT}/server/deploy/huiyingzhai-panel.service" \
  /etc/systemd/system/huiyingzhai-panel.service

ACCOUNTS_FILE="${PANEL_HOME}/accounts.json"
ACCOUNT_INFO=""
if [[ ! -f "${ACCOUNTS_FILE}" ]]; then
  ACCOUNT_INFO="$(runuser -u "${PANEL_USER}" -- \
    env PANEL_ACCOUNTS_FILE="${ACCOUNTS_FILE}" \
    python3 /usr/local/lib/huiyingzhai/manage_accounts.py init tutnt zhl)"
fi
ACCOUNT_SUMMARY="$(printf '%s\n' "${ACCOUNT_INFO}" | sed 's/^/    /')"
if [[ -z "${ACCOUNT_SUMMARY}" ]]; then
  ACCOUNT_SUMMARY="    账号文件已存在，未修改：${ACCOUNTS_FILE}"
fi

rm -f "${PANEL_HOME}/deploy.request"
systemctl daemon-reload
systemctl enable huiyingzhai-deploy.path
systemctl restart huiyingzhai-deploy.path
systemctl enable huiyingzhai-panel.service
systemctl restart huiyingzhai-panel.service

if ! systemctl is-active --quiet huiyingzhai-panel.service; then
  echo "警告：部署面板启动失败，执行 journalctl -u huiyingzhai-panel 查看原因"
fi

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

NGINX_SITE="/etc/nginx/sites-enabled/huiyingzhai"
if [[ -f "${NGINX_SITE}" ]] && \
  ! grep -Eq 'huiyingzhai-panel\.conf|location = /' "${NGINX_SITE}"; then
  echo "提示：现有 Nginx 站点缺少个人主页/部署面板反代（见 server/README.md），请更新后 reload nginx"
fi

cat <<EOF

==> 个人主页与部署面板已安装
  个人主页：https://<你的域名>/（Nginx 需包含 location = / 反代）
  部署页：  https://<你的域名>/deploy/
  登录账号（登录后可修改密码）：
${ACCOUNT_SUMMARY}
  账号管理：python3 /usr/local/lib/huiyingzhai/manage_accounts.py
  面板配置：${PANEL_CONF}（600）
  部署参数：${DEPLOY_CONF}（600，可改仓库/分支/镜像）
  部署日志：/var/log/huiyingzhai-deploy.log

  账号密码只保存 PBKDF2 哈希；修改密码请登录后在部署页操作。
EOF
