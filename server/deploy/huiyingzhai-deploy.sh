#!/usr/bin/env bash
# 汇英斋服务端部署脚本
#
# 执行链路：
#   网页面板写入 /var/lib/huiyingzhai-panel/deploy.request
#   → systemd path 单元 huiyingzhai-deploy.path 监听到文件
#   → huiyingzhai-deploy.service 以 root 执行本脚本
#
# 本文件在仓库中的路径：server/deploy/huiyingzhai-deploy.sh
# install.sh 会安装到 /usr/local/sbin/huiyingzhai-deploy（root 所有）。
# 修改本文件后需要重新执行 sudo bash server/deploy/install.sh 才会生效。
#
# 部署范围：只把 GitHub 仓库的 server/ 目录同步到 /opt/huiyingzhai/server。
# 保留：.env、data/、uploads/、.venv。
# 失败时自动回滚到部署前的代码，并重启服务。
set -Eeuo pipefail
umask 027

CONF_FILE=/etc/huiyingzhai/deploy.conf
if [[ -r "$CONF_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$CONF_FILE"
  set +a
fi

REPO="${DEPLOY_REPO:-tuTNTywz/huiyingzhai}"
BRANCH="${DEPLOY_BRANCH:-main}"
GITHUB_TOKEN="${GITHUB_TOKEN:-}"
ARCHIVE_URL_TEMPLATE="${DEPLOY_ARCHIVE_URL_TEMPLATE:-}"
TARGET="${DEPLOY_TARGET:-/opt/huiyingzhai/server}"
APP_USER="${DEPLOY_APP_USER:-huiyingzhai}"
SERVICE="${DEPLOY_SERVICE:-huiyingzhai}"
BACKUP_ROOT="${DEPLOY_BACKUP_ROOT:-/opt/huiyingzhai/backups}"
KEEP_BACKUPS="${DEPLOY_KEEP_BACKUPS:-5}"

STATE_DIR=/var/lib/huiyingzhai-deploy
STATUS_FILE="$STATE_DIR/status.json"
CURRENT_FILE="$STATE_DIR/current.json"
LOG_FILE=/var/log/huiyingzhai-deploy.log
REQUEST_FILE=/var/lib/huiyingzhai-panel/deploy.request
LOCK_FILE=/run/lock/huiyingzhai-deploy.lock

STEP="准备"
STARTED_AT="$(date +%s%3N)"
FINISHED_AT=""
COMMIT_SHA=""
COMMIT_MSG=""
ARCHIVE_SHA=""
BACKUP_DIR=""
STAMP=""
TMP_DIR=""
SYNCED=0
REQ_CHANGED=0
PORT=3000
FAIL_MSG=""
ROLLED_BACK=false
CURL_AUTH_ARGS=()

mkdir -p "$STATE_DIR" "$BACKUP_ROOT" "$(dirname "$LOG_FILE")"
# 状态目录要让无特权面板用户能进入读取 status.json/current.json
chmod 755 "$STATE_DIR" 2>/dev/null || true
chmod 700 "$BACKUP_ROOT" 2>/dev/null || true
touch "$LOG_FILE"
chmod 644 "$LOG_FILE" 2>/dev/null || true

log() {
  local line
  line="$(date '+%F %T') $*"
  printf '%s\n' "$line" >>"$LOG_FILE"
  printf '%s\n' "$line" >&2
}

# 把当前状态原子写入 status.json，供网页面板读取
write_status() {
  local state="$1"
  local message="$2"
  STATE="$state" STEP="$STEP" MESSAGE="$message" STARTED_AT="$STARTED_AT" \
  FINISHED_AT="$FINISHED_AT" COMMIT_SHA="$COMMIT_SHA" COMMIT_MSG="$COMMIT_MSG" \
  BRANCH="$BRANCH" ARCHIVE_SHA="$ARCHIVE_SHA" ROLLED_BACK="$ROLLED_BACK" \
  python3 - "$STATUS_FILE" <<'PY'
import json
import os
import sys
import time

path = sys.argv[1]


def val(name, default=""):
    value = os.environ.get(name)
    return default if value is None or value == "" else value


def as_int(name):
    try:
        value = int(val(name, "0"))
    except ValueError:
        return 0
    return value or None


data = {
    "state": val("STATE", "unknown"),
    "step": val("STEP"),
    "message": val("MESSAGE"),
    "startedAt": as_int("STARTED_AT") or 0,
    "finishedAt": as_int("FINISHED_AT"),
    "branch": val("BRANCH"),
    "commit": val("COMMIT_SHA"),
    "commitMessage": val("COMMIT_MSG"),
    "archiveSha256": val("ARCHIVE_SHA"),
    "rolledBack": val("ROLLED_BACK", "false") == "true",
    "updatedAt": int(time.time() * 1000),
}
tmp = path + ".tmp"
with open(tmp, "w", encoding="utf-8") as fh:
    json.dump(data, fh, ensure_ascii=False, indent=2)
os.replace(tmp, path)
PY
  chmod 644 "$STATUS_FILE" 2>/dev/null || true
}

# 健康检查：读取 .env 里的 PORT，轮询本机 /api/health
wait_healthy() {
  local i
  for i in $(seq 1 30); do
    if systemctl is-active --quiet "$SERVICE" \
      && curl -fsS --max-time 2 "http://127.0.0.1:${PORT}/api/health" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  return 1
}

rollback() {
  set +e
  if [[ -z "$BACKUP_DIR" || ! -d "$BACKUP_DIR" ]]; then
    log "没有可用备份，无法回滚"
    return 1
  fi

  log "回滚代码：$BACKUP_DIR -> $TARGET"
  rsync -a --delete \
    --exclude .env --exclude data --exclude uploads --exclude .venv \
    --exclude __pycache__ --exclude '*.pyc' \
    "$BACKUP_DIR/" "$TARGET/" >>"$LOG_FILE" 2>&1
  if (( $? != 0 )); then
    log "回滚同步失败"
  fi

  chown -R -h "$APP_USER:$APP_USER" "$TARGET" >>"$LOG_FILE" 2>&1

  if (( REQ_CHANGED == 1 )) && [[ -x "$TARGET/.venv/bin/pip" && -f "$TARGET/requirements.txt" ]]; then
    run_as_app "$TARGET/.venv/bin/pip" install \
      --disable-pip-version-check -r "$TARGET/requirements.txt" >>"$LOG_FILE" 2>&1
  fi

  systemctl restart "$SERVICE" >>"$LOG_FILE" 2>&1
  if wait_healthy; then
    log "回滚完成，服务已恢复正常"
    return 0
  fi
  log "回滚后服务仍未通过健康检查，请人工处理"
  return 1
}

cleanup() {
  local rc=$?
  trap - EXIT
  set +e
  rm -f "$REQUEST_FILE"
  if [[ -n "$TMP_DIR" && -d "$TMP_DIR" ]]; then
    rm -rf -- "$TMP_DIR"
  fi
  if (( rc != 0 )); then
    FINISHED_AT="$(date +%s%3N)"
    local msg="${FAIL_MSG:-部署失败（退出码 $rc）}"
    if (( SYNCED == 1 )) && [[ -n "$BACKUP_DIR" && -d "$BACKUP_DIR" ]]; then
      log "部署失败，开始自动回滚"
      if rollback; then
        ROLLED_BACK=true
        msg="$msg；已自动回滚"
      else
        ROLLED_BACK=false
        msg="$msg；自动回滚失败，请人工处理"
      fi
    fi
    write_status failed "$msg"
    log "部署失败：$msg"
  fi
  exit "$rc"
}
trap cleanup EXIT

# 防止并发部署（正常由 systemd 触发，这里兜底手动执行的情况）
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  log "已有部署任务在运行，忽略本次请求"
  rm -f "$REQUEST_FILE"
  exit 0
fi

# 配置校验：避免把不受控内容拼进 URL 或参数
if [[ ! "$REPO" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]]; then
  FAIL_MSG="DEPLOY_REPO 格式应为 owner/repo：$REPO"
  exit 1
fi
if [[ ! "$BRANCH" =~ ^[A-Za-z0-9._/-]+$ || "$BRANCH" == *..* || "$BRANCH" == -* ]]; then
  FAIL_MSG="DEPLOY_BRANCH 不合法：$BRANCH"
  exit 1
fi
if [[ ! "$KEEP_BACKUPS" =~ ^[0-9]+$ ]] || (( KEEP_BACKUPS < 1 )); then
  KEEP_BACKUPS=5
fi

APP_UID="$(id -u "$APP_USER")"
APP_GID="$(id -g "$APP_USER")"
APP_HOME="$(getent passwd "$APP_USER" 2>/dev/null | cut -d: -f6 || true)"
APP_HOME="${APP_HOME:-/opt/huiyingzhai}"

# 以服务用户身份执行 pip/venv。setpriv 不经过 PAM，在 systemd 沙箱里更稳；
# 没有 setpriv 时退回 runuser。
run_as_app() {
  if command -v setpriv >/dev/null 2>&1; then
    env HOME="$APP_HOME" USER="$APP_USER" LOGNAME="$APP_USER" \
      setpriv --reuid="$APP_UID" --regid="$APP_GID" --init-groups -- "$@"
  else
    runuser -u "$APP_USER" -- "$@"
  fi
}

log "===== 开始部署 ${REPO}@${BRANCH} ====="

# 先从现有 .env 读取端口：即使部署在早期失败，回滚后的健康检查也能用对端口
ENV_PORT="$(sed -n 's/^[[:space:]]*PORT[[:space:]]*=[[:space:]]*\([0-9][0-9]*\).*/\1/p' "$TARGET/.env" 2>/dev/null | tail -n1 || true)"
if [[ "$ENV_PORT" =~ ^[0-9]+$ ]]; then
  PORT="$ENV_PORT"
fi

STEP="下载代码"
write_status running "正在从 GitHub 下载 ${REPO}@${BRANCH}"

TMP_DIR="$(mktemp -d)"
TARBALL="$TMP_DIR/repo.tar.gz"

# 令牌不放在命令行参数里（否则同机用户可从 /proc/<pid>/cmdline 读到）
CURL_AUTH_ARGS=()
if [[ -n "$GITHUB_TOKEN" ]]; then
  AUTH_FILE="$TMP_DIR/curl-auth.conf"
  (
    umask 077
    printf 'header = "Authorization: Bearer %s"\n' "$GITHUB_TOKEN" >"$AUTH_FILE"
  )
  CURL_AUTH_ARGS=(--config "$AUTH_FILE")
fi

if [[ -n "$ARCHIVE_URL_TEMPLATE" ]]; then
  ARCHIVE_URL="${ARCHIVE_URL_TEMPLATE//%REPO%/$REPO}"
  ARCHIVE_URL="${ARCHIVE_URL//%BRANCH%/$BRANCH}"
else
  ARCHIVE_URL="https://codeload.github.com/${REPO}/tar.gz/refs/heads/${BRANCH}"
fi

CURL_ARGS=(-fsSL --retry 3 --retry-delay 2 --connect-timeout 20 --max-time 300
  --max-filesize 104857600 -o "$TARBALL")
CURL_ARGS+=("${CURL_AUTH_ARGS[@]}")

log "下载地址：$ARCHIVE_URL"
if ! curl "${CURL_ARGS[@]}" "$ARCHIVE_URL" >>"$LOG_FILE" 2>&1; then
  FAIL_MSG="GitHub 下载失败，请检查服务器网络或在 deploy.conf 配置 DEPLOY_ARCHIVE_URL_TEMPLATE"
  exit 1
fi
ARCHIVE_SHA="$(sha256sum "$TARBALL" | awk '{print $1}')"
log "下载完成，sha256=${ARCHIVE_SHA}"

STEP="校验代码"
write_status running "正在校验代码包"
if ! tar --no-same-owner -xzf "$TARBALL" -C "$TMP_DIR" >>"$LOG_FILE" 2>&1; then
  FAIL_MSG="解压代码包失败"
  exit 1
fi
SRC_DIR="$(find "$TMP_DIR" -mindepth 1 -maxdepth 1 -type d -print -quit)"
if [[ -z "$SRC_DIR" || ! -f "$SRC_DIR/server/app/main.py" || ! -f "$SRC_DIR/server/requirements.txt" ]]; then
  FAIL_MSG="代码包结构异常：未找到 server/app/main.py 或 server/requirements.txt"
  exit 1
fi

# 尽力获取最新提交信息（失败不影响部署）
if command -v python3 >/dev/null 2>&1; then
  API_HEADERS=(-H "Accept: application/vnd.github+json")
  if COMMIT_JSON="$(curl -fsSL --max-time 15 "${API_HEADERS[@]}" "${CURL_AUTH_ARGS[@]}" \
    "https://api.github.com/repos/${REPO}/commits/${BRANCH}" 2>/dev/null)"; then
    mapfile -t COMMIT_INFO < <(printf '%s' "$COMMIT_JSON" | python3 -c '
import json
import sys

data = json.load(sys.stdin)
print((data.get("sha") or "")[:12])
message = (data.get("commit") or {}).get("message") or ""
print(message.splitlines()[0] if message else "")
' 2>/dev/null || true)
    COMMIT_SHA="${COMMIT_INFO[0]:-}"
    COMMIT_MSG="${COMMIT_INFO[1]:-}"
    COMMIT_SHA="${COMMIT_SHA%$'\r'}"
    COMMIT_MSG="${COMMIT_MSG%$'\r'}"
  fi
fi

STEP="备份当前版本"
write_status running "正在备份当前版本"
STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP_DIR="$BACKUP_ROOT/$STAMP"
mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"
if ! rsync -a \
  --exclude .env --exclude data --exclude uploads --exclude .venv \
  --exclude __pycache__ --exclude '*.pyc' \
  "$TARGET/" "$BACKUP_DIR/" >>"$LOG_FILE" 2>&1; then
  FAIL_MSG="备份当前版本失败"
  exit 1
fi
log "已备份到 $BACKUP_DIR"

# 只保留最近 N 份备份
mapfile -t OLD_BACKUPS < <(ls -1dt "$BACKUP_ROOT"/*/ 2>/dev/null | tail -n +$((KEEP_BACKUPS + 1)) || true)
for old in "${OLD_BACKUPS[@]}"; do
  if [[ -n "$old" ]]; then
    rm -rf -- "$old"
  fi
done

STEP="同步代码"
write_status running "正在同步 server/ 代码"
SYNCED=1
if ! rsync -a --delete \
  --exclude .env --exclude data --exclude uploads --exclude .venv \
  --exclude __pycache__ --exclude '*.pyc' \
  "$SRC_DIR/server/" "$TARGET/" >>"$LOG_FILE" 2>&1; then
  FAIL_MSG="同步代码失败"
  exit 1
fi
mkdir -p "$TARGET/data" "$TARGET/uploads"
# -h 避免跟随符号链接去修改目标文件属主
if ! chown -R -h "$APP_USER:$APP_USER" "$TARGET" >>"$LOG_FILE" 2>&1; then
  FAIL_MSG="调整文件属主失败"
  exit 1
fi

# .env 缺失时从模板创建；不覆盖已有配置
if [[ ! -f "$TARGET/.env" && -f "$TARGET/.env.example" ]]; then
  install -m 600 -o "$APP_USER" -g "$APP_USER" "$TARGET/.env.example" "$TARGET/.env"
  log "未找到 .env，已从 .env.example 创建，请填写后重新部署"
fi

STEP="安装依赖"
write_status running "正在检查 Python 依赖"
if [[ ! -x "$TARGET/.venv/bin/python" ]]; then
  log "虚拟环境不存在，正在创建"
  if ! run_as_app python3 -m venv "$TARGET/.venv" >>"$LOG_FILE" 2>&1; then
    FAIL_MSG="创建虚拟环境失败（是否缺少 python3-venv？）"
    exit 1
  fi
  REQ_CHANGED=1
fi

if [[ ! -f "$BACKUP_DIR/requirements.txt" ]] || ! cmp -s "$BACKUP_DIR/requirements.txt" "$TARGET/requirements.txt"; then
  REQ_CHANGED=1
  log "requirements.txt 有变化，执行 pip install"
  if ! run_as_app "$TARGET/.venv/bin/pip" install \
    --disable-pip-version-check -r "$TARGET/requirements.txt" >>"$LOG_FILE" 2>&1; then
    FAIL_MSG="pip 安装依赖失败"
    exit 1
  fi
else
  log "requirements.txt 未变化，跳过 pip install"
fi

# 注意：这里不安装仓库里的 systemd 单元。
# 单元一旦被自动安装，仓库被篡改就等于拿到 root；systemd 单元只由
# install.sh（root 手动执行）更新。同理，部署脚本和面板脚本也不会被自动更新。

STEP="重启服务"
write_status running "正在重启服务"
if ! systemctl restart "$SERVICE" >>"$LOG_FILE" 2>&1; then
  FAIL_MSG="重启服务失败"
  exit 1
fi

STEP="健康检查"
write_status running "正在等待服务通过健康检查"
if ! wait_healthy; then
  FAIL_MSG="服务重启后健康检查失败（http://127.0.0.1:${PORT}/api/health）"
  exit 1
fi

STEP="完成"
FINISHED_AT="$(date +%s%3N)"
write_status success "部署完成"
cp -f "$STATUS_FILE" "$CURRENT_FILE"
chmod 644 "$CURRENT_FILE" 2>/dev/null || true
log "===== 部署完成：${BRANCH} ${COMMIT_SHA:-（未知提交）} ====="
