#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""汇英斋部署面板 + 个人主页。

运行在 127.0.0.1:3999，由 Nginx 反向代理：
  /          → 个人主页（tuTNT's home）
  /deploy/   → 登录 + 部署控制台

本进程没有 root 权限：点击部署时只写入
/var/lib/huiyingzhai-panel/deploy.request，
由 root 的 systemd path 单元触发 /usr/local/sbin/huiyingzhai-deploy 执行实际部署。

账号保存在 /var/lib/huiyingzhai-panel/accounts.json，密码只存 PBKDF2 哈希。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

PORT = int(os.environ.get("PANEL_PORT") or "3999")
QUEUE_DIR = Path(os.environ.get("PANEL_QUEUE_DIR") or "/var/lib/huiyingzhai-panel")
ACCOUNTS_FILE = Path(
    os.environ.get("PANEL_ACCOUNTS_FILE")
    or "/var/lib/huiyingzhai-panel/accounts.json"
)
STATUS_FILE = Path(
    os.environ.get("PANEL_STATUS_FILE") or "/var/lib/huiyingzhai-deploy/status.json"
)
CURRENT_FILE = Path(
    os.environ.get("PANEL_CURRENT_FILE") or "/var/lib/huiyingzhai-deploy/current.json"
)
LOG_FILE = Path(os.environ.get("PANEL_LOG_FILE") or "/var/log/huiyingzhai-deploy.log")
REQUEST_FILE = QUEUE_DIR / "deploy.request"

SESSION_COOKIE = "hyz_session"
SESSION_TTL = 12 * 3600
PBKDF2_ITERATIONS = 600_000
MIN_PASSWORD_LEN = 6
MAX_PASSWORD_LEN = 128
LOGIN_MAX_FAILURES = 10
LOGIN_WINDOW_SECONDS = 300
MAX_LOG_BYTES = 64 * 1024
LOG_LINES = 300
RUNNING_STALE_MS = 30 * 60 * 1000
MIN_DEPLOY_INTERVAL_SECONDS = 5

CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
    "connect-src 'self'; img-src 'self' data:; base-uri 'none'; "
    "form-action 'none'; frame-ancestors 'none'"
)

_accounts_lock = threading.RLock()
_sessions_lock = threading.Lock()
_deploy_lock = threading.Lock()
_login_lock = threading.Lock()
_sessions: dict[str, dict] = {}
_login_failures: dict[str, list[float]] = {}
_last_deploy_request_at = 0.0


def _now_ms() -> int:
    return int(time.time() * 1000)


def _read_json(path: Path):
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _tail_text(path: Path, max_bytes: int = MAX_LOG_BYTES) -> str:
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - max_bytes))
            raw = fh.read()
        return raw.decode("utf-8", "replace")
    except OSError:
        return ""


# ---------- 账号 ----------


def load_accounts() -> dict:
    try:
        with ACCOUNTS_FILE.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    users = data.get("users") if isinstance(data, dict) else None
    return users if isinstance(users, dict) else {}


def make_record(password: str) -> dict:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt), PBKDF2_ITERATIONS
    )
    return {
        "salt": salt,
        "hash": digest.hex(),
        "iterations": PBKDF2_ITERATIONS,
        "updatedAt": _now_ms(),
    }


def verify_password(record: dict, password: str) -> bool:
    try:
        salt = str(record["salt"])
        expected = str(record["hash"])
        iterations = int(record.get("iterations") or PBKDF2_ITERATIONS)
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt), iterations
        )
        return hmac.compare_digest(digest.hex(), expected)
    except (KeyError, TypeError, ValueError):
        return False


def save_accounts(users: dict) -> None:
    ACCOUNTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = ACCOUNTS_FILE.with_name(ACCOUNTS_FILE.name + ".tmp")
    payload = {"version": 1, "users": users}
    with _accounts_lock:
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, ACCOUNTS_FILE)


# ---------- 会话 ----------


def create_session(username: str) -> tuple[str, str]:
    token = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(32)
    now = time.time()
    with _sessions_lock:
        _sessions[token] = {
            "user": username,
            "csrf": csrf,
            "expires": now + SESSION_TTL,
            "created": now,
        }
    return token, csrf


def get_session(token: str):
    if not token:
        return None
    now = time.time()
    with _sessions_lock:
        record = _sessions.get(token)
        if not record:
            return None
        if record["expires"] <= now:
            _sessions.pop(token, None)
            return None
        return record


def drop_session(token: str) -> None:
    with _sessions_lock:
        _sessions.pop(token, None)


def drop_user_sessions(username: str, keep: str | None = None) -> None:
    with _sessions_lock:
        for token in [
            token
            for token, record in _sessions.items()
            if record.get("user") == username and token != keep
        ]:
            _sessions.pop(token, None)


def _login_rate_ok(ip: str) -> bool:
    now = time.time()
    with _login_lock:
        recent = [t for t in _login_failures.get(ip, []) if now - t < LOGIN_WINDOW_SECONDS]
        _login_failures[ip] = recent
        return len(recent) < LOGIN_MAX_FAILURES


def _login_failed(ip: str) -> None:
    with _login_lock:
        _login_failures.setdefault(ip, []).append(time.time())


def _login_succeeded(ip: str) -> None:
    with _login_lock:
        _login_failures.pop(ip, None)


# ---------- 页面 ----------

HOME_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>tuTNT's home</title>
<link rel="icon" href="data:,">
<style>
  :root { color-scheme: light dark; }
  * { box-sizing: border-box; }
  html, body { height: 100%; }
  body {
    margin: 0;
    display: grid;
    place-items: center;
    font: 16px/1.6 -apple-system, BlinkMacSystemFont, "Segoe UI", "Noto Sans CJK SC", "Microsoft YaHei", sans-serif;
    color: #0f172a;
    background: radial-gradient(circle at 50% 18%, #eef4ff 0%, #f7f9fc 46%, #e9edf4 100%);
  }
  main { padding: 24px; text-align: center; }
  h1 {
    margin: 0 0 30px;
    font-size: clamp(34px, 7vw, 58px);
    line-height: 1.1;
    font-weight: 700;
    letter-spacing: -0.035em;
  }
  .btn {
    display: inline-block;
    padding: 12px 32px;
    border-radius: 999px;
    background: #111827;
    color: #fff;
    text-decoration: none;
    font-size: 15px;
    letter-spacing: 0.04em;
    box-shadow: 0 10px 30px rgba(15, 23, 42, 0.18);
    transition: transform 0.16s ease, box-shadow 0.16s ease, background 0.16s ease;
  }
  .btn:hover {
    transform: translateY(-2px);
    background: #1f2937;
    box-shadow: 0 14px 36px rgba(15, 23, 42, 0.26);
  }
  @media (prefers-color-scheme: dark) {
    body {
      color: #e5e7eb;
      background: radial-gradient(circle at 50% 18%, #111c33 0%, #0b1220 55%, #070b13 100%);
    }
    .btn { background: #e5e7eb; color: #0b1220; box-shadow: 0 10px 30px rgba(0, 0, 0, 0.5); }
    .btn:hover { background: #ffffff; }
  }
</style>
</head>
<body>
<main>
  <h1>tuTNT's home</h1>
  <a class="btn" href="/deploy/">Deploy</a>
</main>
</body>
</html>
"""

DEPLOY_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>部署控制台 · tuTNT's home</title>
<link rel="icon" href="data:,">
<style>
  :root { color-scheme: light dark; }
  * { box-sizing: border-box; }
  body { margin: 0; font: 15px/1.6 -apple-system, BlinkMacSystemFont, "Segoe UI", "Noto Sans CJK SC", "Microsoft YaHei", sans-serif; background: #f4f6f8; color: #1f2937; }
  main { max-width: 880px; margin: 0 auto; padding: 24px 16px 64px; }
  .top { margin-bottom: 16px; }
  .home { color: #6b7280; text-decoration: none; font-size: 13px; }
  .home:hover { color: #111827; }
  h1 { font-size: 20px; margin: 0 0 4px; }
  h2 { font-size: 16px; margin: 0 0 14px; }
  .sub { color: #6b7280; font-size: 13px; margin-bottom: 20px; }
  .card { background: #fff; border: 1px solid #e5e7eb; border-radius: 12px; padding: 16px; margin-bottom: 16px; }
  .row { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; }
  button { font: inherit; border: 0; border-radius: 8px; padding: 9px 16px; cursor: pointer; }
  button.primary { background: #2563eb; color: #fff; }
  button.danger { background: #dc2626; color: #fff; }
  button.secondary { background: #eef2f7; color: #1f2937; }
  button:disabled { opacity: .5; cursor: not-allowed; }
  input { font: inherit; border: 1px solid #d1d5db; border-radius: 8px; padding: 9px 12px; width: 100%; max-width: 360px; background: #fff; color: inherit; }
  label { font-size: 13px; color: #6b7280; display: block; margin: 10px 0 6px; }
  #login label:first-child, .form label:first-child { margin-top: 0; }
  .badge { display: inline-block; font-size: 12px; padding: 2px 8px; border-radius: 999px; background: #e5e7eb; color: #374151; }
  .badge.running { background: #fef3c7; color: #92400e; }
  .badge.success { background: #dcfce7; color: #166534; }
  .badge.failed { background: #fee2e2; color: #991b1b; }
  pre { background: #f9fafb; border: 1px solid #e5e7eb; border-radius: 8px; padding: 12px; overflow: auto; max-height: 360px; font: 12px/1.5 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; white-space: pre-wrap; word-break: break-word; }
  .kv { display: grid; grid-template-columns: 96px 1fr; gap: 4px 12px; font-size: 13px; }
  .kv div:nth-child(odd) { color: #6b7280; }
  .msg { margin-top: 10px; font-size: 13px; color: #6b7280; min-height: 1.4em; }
  .who { font-size: 12px; color: #6b7280; margin-left: 8px; }
  .hidden { display: none !important; }
  code { font-size: 12px; background: #f3f4f6; padding: 1px 5px; border-radius: 4px; }
  @media (prefers-color-scheme: dark) {
    body { background: #0f172a; color: #e5e7eb; }
    .card { background: #111c33; border-color: #23314f; }
    input, pre, code { background: #0b1324; color: #e5e7eb; border-color: #23314f; }
    button.secondary { background: #1f2c47; color: #e5e7eb; }
    .sub, label, .msg, .kv div:nth-child(odd), .who, .home { color: #94a3b8; }
    .home:hover { color: #e5e7eb; }
  }
</style>
</head>
<body>
<main>
  <div class="top"><a class="home" href="/">← tuTNT's home</a></div>
  <h1>部署控制台</h1>
  <div class="sub">登录后可查看部署状态、更新服务端代码和修改密码。</div>

  <section id="login" class="card hidden">
    <h2>登录</h2>
    <label for="username">用户名</label>
    <input id="username" autocomplete="username" autocapitalize="off" spellcheck="false">
    <label for="password">密码</label>
    <input id="password" type="password" autocomplete="current-password">
    <div class="row" style="margin-top: 16px">
      <button id="login-btn" class="primary">登录</button>
    </div>
    <div id="login-msg" class="msg"></div>
  </section>

  <section id="app" class="hidden">
    <div class="card">
      <div class="row" style="justify-content: space-between">
        <div>
          <span id="state-badge" class="badge">加载中</span>
          <span class="who">已登录：<b id="who"></b></span>
        </div>
        <button id="refresh" class="secondary">刷新</button>
      </div>
      <div class="kv" style="margin-top: 14px">
        <div>当前版本</div><div id="current">—</div>
        <div>上次部署</div><div id="last">—</div>
      </div>
      <div class="row" style="margin-top: 16px">
        <button id="deploy" class="danger">部署最新代码</button>
        <button id="pwd-toggle" class="secondary">修改密码</button>
        <button id="logout" class="secondary">退出</button>
      </div>
      <div id="msg" class="msg"></div>
    </div>

    <div id="pwd-card" class="card hidden">
      <h2>修改密码</h2>
      <div class="form">
        <label for="pwd-current">当前密码</label>
        <input id="pwd-current" type="password" autocomplete="current-password">
        <label for="pwd-new">新密码（6-128 位）</label>
        <input id="pwd-new" type="password" autocomplete="new-password">
        <label for="pwd-confirm">确认新密码</label>
        <input id="pwd-confirm" type="password" autocomplete="new-password">
      </div>
      <div class="row" style="margin-top: 14px">
        <button id="pwd-save" class="primary">保存</button>
        <button id="pwd-cancel" class="secondary">取消</button>
      </div>
      <div id="pwd-msg" class="msg"></div>
    </div>

    <div class="card">
      <div class="row" style="justify-content: space-between; margin-bottom: 10px">
        <strong style="font-size: 14px">部署日志</strong>
        <button id="toggle-log" class="secondary">隐藏</button>
      </div>
      <pre id="log" class="hidden">—</pre>
    </div>
  </section>
</main>
<script>
(() => {
  const $ = (id) => document.getElementById(id);
  let csrf = '';
  let currentUser = '';
  let pollTimer = null;
  let forcePollUntil = 0;
  let busy = false;
  let lastLog = '';

  function api(path, options) {
    options = options || {};
    const headers = Object.assign({}, options.headers || {});
    if (csrf) headers['X-CSRF-Token'] = csrf;
    return fetch(path, Object.assign({ credentials: 'same-origin', cache: 'no-store' }, options, { headers }));
  }

  function showLogin(message) {
    csrf = '';
    currentUser = '';
    stopPolling();
    $('app').classList.add('hidden');
    $('login').classList.remove('hidden');
    $('login-msg').textContent = message || '';
    $('password').value = '';
    setTimeout(() => $('username').focus(), 30);
  }

  async function showApp(user) {
    currentUser = user || '';
    $('who').textContent = currentUser;
    $('login').classList.add('hidden');
    $('app').classList.remove('hidden');
    $('pwd-card').classList.add('hidden');
    $('pwd-current').value = '';
    $('pwd-new').value = '';
    $('pwd-confirm').value = '';
    $('pwd-msg').textContent = '';
    await loadStatus();
  }

  async function checkSession() {
    try {
      const resp = await fetch('api/session', { credentials: 'same-origin', cache: 'no-store' });
      if (resp.ok) {
        const data = await resp.json();
        csrf = data.csrf || '';
        await showApp(data.user || '');
        return;
      }
    } catch (err) { /* ignore */ }
    showLogin('');
  }

  async function login() {
    const username = $('username').value.trim();
    const password = $('password').value;
    if (!username || !password) {
      $('login-msg').textContent = '请输入用户名和密码';
      return;
    }
    $('login-btn').disabled = true;
    $('login-msg').textContent = '登录中…';
    try {
      const resp = await fetch('api/login', {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username: username, password: password })
      });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) {
        $('login-msg').textContent = data.message || ('登录失败（HTTP ' + resp.status + '）');
        return;
      }
      csrf = data.csrf || '';
      $('password').value = '';
      await showApp(data.user || username);
    } catch (err) {
      $('login-msg').textContent = '网络错误，请重试';
    } finally {
      $('login-btn').disabled = false;
    }
  }

  async function logout() {
    try {
      await api('api/logout', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: '{}'
      });
    } catch (err) { /* ignore */ }
    showLogin('');
  }

  function setMsg(text, isError) {
    $('msg').textContent = text || '';
    $('msg').style.color = isError ? '#dc2626' : '';
  }

  function fmtTime(ms) {
    if (!ms) return '—';
    try { return new Date(ms).toLocaleString('zh-CN', { hour12: false }); }
    catch (err) { return '—'; }
  }

  function commitText(obj) {
    if (!obj) return '—';
    const sha = obj.commit ? obj.commit.slice(0, 12) : '';
    const msg = obj.commitMessage ? ' · ' + obj.commitMessage : '';
    const when = obj.deployedAt || obj.finishedAt;
    return (sha || '未知提交') + msg + (when ? '（' + fmtTime(when) + '）' : '');
  }

  function setBadge(state) {
    const labels = { running: '部署中', success: '已完成', failed: '部署失败', idle: '空闲' };
    const classes = { running: 'running', success: 'success', failed: 'failed' };
    const el = $('state-badge');
    const cls = classes[state] || '';
    el.className = 'badge' + (cls ? ' ' + cls : '');
    el.textContent = labels[state] || '未知';
  }

  async function loadStatus() {
    if (!currentUser) return;
    let resp;
    try {
      resp = await api('api/status');
    } catch (err) {
      $('current').textContent = '无法连接部署面板';
      return;
    }
    if (resp.status === 401) {
      showLogin('登录已过期，请重新登录');
      return;
    }
    if (!resp.ok) {
      $('current').textContent = '读取状态失败（HTTP ' + resp.status + '）';
      return;
    }
    const data = await resp.json();
    const last = data.last || null;
    const current = data.current || null;
    setBadge(last ? last.state : 'idle');
    $('current').textContent = commitText(current);
    if (last) {
      const extra = last.finishedAt ? '（' + fmtTime(last.finishedAt) + '）' : '';
      $('last').textContent = (last.message || last.step || '—') + extra;
    } else {
      $('last').textContent = '—';
    }
    $('deploy').disabled = !!data.running || busy;
    await loadLog();
    if (data.running) {
      forcePollUntil = Date.now() + 10000;
      startPolling();
    } else if (Date.now() < forcePollUntil) {
      startPolling();
    } else {
      stopPolling();
    }
  }

  async function loadLog() {
    try {
      const resp = await api('api/log');
      if (resp.status === 401) {
        showLogin('登录已过期，请重新登录');
        return;
      }
      if (!resp.ok) return;
      const text = await resp.text();
      if (text !== lastLog) {
        lastLog = text;
        const el = $('log');
        el.textContent = text || '（暂无日志）';
        el.scrollTop = el.scrollHeight;
      }
    } catch (err) { /* ignore */ }
  }

  function startPolling() {
    if (pollTimer) return;
    pollTimer = setInterval(loadStatus, 2500);
  }

  function stopPolling() {
    if (pollTimer) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
  }

  async function deploy() {
    if (busy) return;
    if (!confirm('确定从 GitHub 拉取最新代码并重启服务吗？\\n\\n部署期间服务会短暂中断；失败会自动回滚。')) return;
    busy = true;
    $('deploy').disabled = true;
    setMsg('已提交部署请求，等待执行…');
    forcePollUntil = Date.now() + 30000;
    try {
      const resp = await api('api/deploy', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: '{}'
      });
      if (resp.status === 401) {
        showLogin('登录已过期，请重新登录');
        return;
      }
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) {
        setMsg(data.message || ('请求失败（HTTP ' + resp.status + '）'), true);
      } else {
        setMsg('部署已开始，页面会自动刷新状态。');
        startPolling();
      }
    } catch (err) {
      setMsg('网络错误，请重试', true);
    } finally {
      busy = false;
      loadStatus();
    }
  }

  async function savePassword() {
    const current = $('pwd-current').value;
    const next = $('pwd-new').value;
    const confirmValue = $('pwd-confirm').value;
    const msg = $('pwd-msg');
    msg.style.color = '';
    if (!current || !next) {
      msg.textContent = '请填写当前密码和新密码';
      return;
    }
    if (next !== confirmValue) {
      msg.textContent = '两次输入的新密码不一致';
      return;
    }
    $('pwd-save').disabled = true;
    msg.textContent = '保存中…';
    try {
      const resp = await api('api/account/password', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ currentPassword: current, newPassword: next })
      });
      if (resp.status === 401) {
        showLogin('登录已过期，请重新登录');
        return;
      }
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) {
        msg.style.color = '#dc2626';
        msg.textContent = data.message || ('修改失败（HTTP ' + resp.status + '）');
        return;
      }
      msg.textContent = '密码已更新';
      $('pwd-current').value = '';
      $('pwd-new').value = '';
      $('pwd-confirm').value = '';
    } catch (err) {
      msg.style.color = '#dc2626';
      msg.textContent = '网络错误，请重试';
    } finally {
      $('pwd-save').disabled = false;
    }
  }

  $('login-btn').addEventListener('click', login);
  $('password').addEventListener('keydown', (e) => { if (e.key === 'Enter') login(); });
  $('username').addEventListener('keydown', (e) => { if (e.key === 'Enter') $('password').focus(); });
  $('refresh').addEventListener('click', loadStatus);
  $('deploy').addEventListener('click', deploy);
  $('logout').addEventListener('click', logout);
  $('pwd-toggle').addEventListener('click', () => {
    const card = $('pwd-card');
    card.classList.toggle('hidden');
    if (!card.classList.contains('hidden')) {
      $('pwd-msg').textContent = '';
      setTimeout(() => $('pwd-current').focus(), 30);
    }
  });
  $('pwd-cancel').addEventListener('click', () => {
    $('pwd-card').classList.add('hidden');
    $('pwd-msg').textContent = '';
  });
  $('pwd-save').addEventListener('click', savePassword);
  $('toggle-log').addEventListener('click', () => {
    const el = $('log');
    el.classList.toggle('hidden');
    $('toggle-log').textContent = el.classList.contains('hidden') ? '显示' : '隐藏';
  });

  checkSession();
})();
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "huiyingzhai-panel"
    sys_version = ""

    def log_message(self, fmt, *args):
        # 只记录访问行，不记录请求头/正文（密码不在日志里）
        print("%s %s" % (self.address_string(), fmt % args), flush=True)

    def _route(self) -> tuple[str, bool]:
        raw = urlparse(self.path).path
        if raw == "/deploy" or raw.startswith("/deploy/"):
            return raw[len("/deploy"):] or "/", True
        return raw, False

    def _send(self, code, body, content_type="application/json; charset=utf-8", extra_headers=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", CSP)
        for name, value in extra_headers or []:
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _cookie_token(self) -> str:
        raw = self.headers.get("Cookie") or ""
        if not raw:
            return ""
        cookie = SimpleCookie()
        try:
            cookie.load(raw)
        except Exception:
            return ""
        morsel = cookie.get(SESSION_COOKIE)
        return morsel.value if morsel else ""

    def _client_ip(self) -> str:
        return (self.headers.get("X-Real-IP") or self.client_address[0] or "").strip()

    def _session_cookie(self, token: str) -> tuple[str, str]:
        parts = [
            "%s=%s" % (SESSION_COOKIE, token),
            "Path=/deploy",
            "HttpOnly",
            "SameSite=Strict",
            "Max-Age=%d" % SESSION_TTL,
        ]
        proto = (self.headers.get("X-Forwarded-Proto") or "").lower()
        if proto == "https":
            parts.append("Secure")
        return ("Set-Cookie", "; ".join(parts))

    @staticmethod
    def _expired_cookie() -> tuple[str, str]:
        return (
            "Set-Cookie",
            "%s=; Path=/deploy; HttpOnly; SameSite=Strict; Max-Age=0" % SESSION_COOKIE,
        )

    def _session_record(self):
        token = self._cookie_token()
        return token, get_session(token)

    def _require_session(self):
        token, record = self._session_record()
        if not record:
            self._send(401, json.dumps({"ok": False, "message": "请先登录"}, ensure_ascii=False))
            return None, None
        return token, record

    def _require_csrf(self, record) -> bool:
        supplied = self.headers.get("X-CSRF-Token", "")
        try:
            valid = hmac.compare_digest(supplied, record.get("csrf", ""))
        except TypeError:
            valid = False
        if not valid:
            self._send(403, json.dumps({"ok": False, "message": "校验失败，请刷新页面后重试"}, ensure_ascii=False))
            return False
        return True

    def _read_json(self, limit: int = 8192):
        content_type = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if content_type != "application/json":
            self._send(415, json.dumps({"ok": False, "message": "不支持的请求类型"}, ensure_ascii=False))
            return None
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            length = 0
        if length > limit:
            self.close_connection = True
            self._send(413, json.dumps({"ok": False, "message": "请求体过大"}, ensure_ascii=False))
            return None
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            self._send(400, json.dumps({"ok": False, "message": "请求格式不正确"}, ensure_ascii=False))
            return None
        if not isinstance(data, dict):
            self._send(400, json.dumps({"ok": False, "message": "请求格式不正确"}, ensure_ascii=False))
            return None
        return data

    def _status_payload(self) -> str:
        last = _read_json(STATUS_FILE)
        current = _read_json(CURRENT_FILE)
        running = False
        if last and last.get("state") == "running":
            try:
                started = int(last.get("startedAt") or 0)
            except (TypeError, ValueError):
                started = 0
            running = (_now_ms() - started) < RUNNING_STALE_MS
        payload = {
            "ok": True,
            "running": running,
            "serverTime": _now_ms(),
            "last": last,
            "current": current,
        }
        return json.dumps(payload, ensure_ascii=False)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        if urlparse(self.path).path == "/deploy":
            self._send(301, "", extra_headers=[("Location", "/deploy/")])
            return
        path, prefixed = self._route()
        if path == "/" and not prefixed:
            self._send(200, HOME_HTML, "text/html; charset=utf-8")
            return
        if path in ("/", "/index.html") and prefixed:
            self._send(200, DEPLOY_HTML, "text/html; charset=utf-8")
            return
        if path == "/healthz":
            self._send(200, json.dumps({"ok": True}))
            return
        if path == "/api/session":
            _token, record = self._session_record()
            if not record:
                self._send(401, json.dumps({"ok": False, "message": "未登录"}, ensure_ascii=False))
                return
            self._send(
                200,
                json.dumps(
                    {"ok": True, "user": record["user"], "csrf": record["csrf"]},
                    ensure_ascii=False,
                ),
            )
            return
        if path == "/api/status":
            if not self._require_session()[1]:
                return
            self._send(200, self._status_payload())
            return
        if path == "/api/log":
            if not self._require_session()[1]:
                return
            lines = _tail_text(LOG_FILE).splitlines()[-LOG_LINES:]
            self._send(200, "\n".join(lines), "text/plain; charset=utf-8")
            return
        self._send(404, json.dumps({"ok": False, "message": "接口不存在"}, ensure_ascii=False))

    def do_POST(self):
        global _last_deploy_request_at

        path, _prefixed = self._route()
        if path == "/api/login":
            self._handle_login()
            return
        if path == "/api/logout":
            self._handle_logout()
            return
        if path == "/api/account/password":
            self._handle_password()
            return
        if path == "/api/deploy":
            self._handle_deploy()
            return
        self._send(404, json.dumps({"ok": False, "message": "接口不存在"}, ensure_ascii=False))

    def _handle_login(self):
        data = self._read_json()
        if data is None:
            return
        username = str(data.get("username") or "").strip()
        password = str(data.get("password") or "")
        if not username or not password or len(username) > 64 or len(password) > 256:
            self._send(400, json.dumps({"ok": False, "message": "用户名或密码不正确"}, ensure_ascii=False))
            return
        ip = self._client_ip()
        if not _login_rate_ok(ip):
            time.sleep(0.5)
            self._send(429, json.dumps({"ok": False, "message": "尝试过于频繁，请稍后再试"}, ensure_ascii=False))
            return
        users = load_accounts()
        record = users.get(username)
        valid = bool(record) and verify_password(record, password)
        if not valid:
            if not record:
                # 对不存在的用户也做一次哈希，减少用户名枚举的时序差异
                hashlib.pbkdf2_hmac(
                    "sha256", password.encode("utf-8"), b"\x00" * 16, PBKDF2_ITERATIONS
                )
            _login_failed(ip)
            time.sleep(0.4)
            self._send(401, json.dumps({"ok": False, "message": "用户名或密码不正确"}, ensure_ascii=False))
            return
        _login_succeeded(ip)
        token, csrf = create_session(username)
        self._send(
            200,
            json.dumps({"ok": True, "user": username, "csrf": csrf}, ensure_ascii=False),
            extra_headers=[self._session_cookie(token)],
        )

    def _handle_logout(self):
        token, record = self._require_session()
        if not record:
            return
        if not self._require_csrf(record):
            return
        drop_session(token)
        self._send(
            200,
            json.dumps({"ok": True}, ensure_ascii=False),
            extra_headers=[self._expired_cookie()],
        )

    def _handle_password(self):
        token, record = self._require_session()
        if not record:
            return
        if not self._require_csrf(record):
            return
        data = self._read_json()
        if data is None:
            return
        current = str(data.get("currentPassword") or "")
        new_password = str(data.get("newPassword") or "")
        if len(new_password) < MIN_PASSWORD_LEN or len(new_password) > MAX_PASSWORD_LEN:
            self._send(
                400,
                json.dumps(
                    {"ok": False, "message": "新密码长度需为 %d-%d 位" % (MIN_PASSWORD_LEN, MAX_PASSWORD_LEN)},
                    ensure_ascii=False,
                ),
            )
            return
        with _accounts_lock:
            users = load_accounts()
            saved = users.get(record["user"])
            if not saved or not verify_password(saved, current):
                time.sleep(0.4)
                self._send(400, json.dumps({"ok": False, "message": "当前密码不正确"}, ensure_ascii=False))
                return
            users[record["user"]] = make_record(new_password)
            try:
                save_accounts(users)
            except OSError as exc:
                self._send(
                    500,
                    json.dumps({"ok": False, "message": "保存密码失败：%s" % exc}, ensure_ascii=False),
                )
                return
        # 让该账号的其他登录会话失效，保留当前会话
        drop_user_sessions(record["user"], keep=token)
        self._send(200, json.dumps({"ok": True, "message": "密码已更新"}, ensure_ascii=False))

    def _handle_deploy(self):
        global _last_deploy_request_at

        _token, record = self._require_session()
        if not record:
            return
        if not self._require_csrf(record):
            return
        data = self._read_json()
        if data is None:
            return
        if data:
            self._send(400, json.dumps({"ok": False, "message": "不支持参数"}, ensure_ascii=False))
            return

        with _deploy_lock:
            now = time.time()
            if now - _last_deploy_request_at < MIN_DEPLOY_INTERVAL_SECONDS:
                self._send(429, json.dumps({"ok": False, "message": "操作过于频繁，请稍后再试"}, ensure_ascii=False))
                return
            last = _read_json(STATUS_FILE)
            if last and last.get("state") == "running":
                try:
                    started = int(last.get("startedAt") or 0)
                except (TypeError, ValueError):
                    started = 0
                if _now_ms() - started < RUNNING_STALE_MS:
                    self._send(409, json.dumps({"ok": False, "message": "已有部署任务正在运行"}, ensure_ascii=False))
                    return
            tmp = None
            try:
                QUEUE_DIR.mkdir(parents=True, exist_ok=True)
                REQUEST_FILE.unlink(missing_ok=True)
                tmp = QUEUE_DIR / ("deploy-%d-%s.tmp" % (os.getpid(), secrets.token_hex(4)))
                tmp.write_text(
                    json.dumps(
                        {
                            "requestedAt": _now_ms(),
                            "client": self._client_ip(),
                            "by": record["user"],
                        },
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
                os.replace(tmp, REQUEST_FILE)
            except OSError as exc:
                if tmp is not None:
                    try:
                        tmp.unlink(missing_ok=True)
                    except OSError:
                        pass
                self._send(
                    500,
                    json.dumps({"ok": False, "message": "写入部署请求失败：%s" % exc}, ensure_ascii=False),
                )
                return
            _last_deploy_request_at = now

        self._send(202, json.dumps({"ok": True, "message": "已提交部署请求"}, ensure_ascii=False))


def main():
    if not ACCOUNTS_FILE.exists():
        print("警告：账号文件不存在：%s" % ACCOUNTS_FILE, flush=True)
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    server.daemon_threads = True
    print("汇英斋部署面板已启动：http://127.0.0.1:%d" % PORT, flush=True)
    print("个人主页：/ ；部署控制台：/deploy/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
