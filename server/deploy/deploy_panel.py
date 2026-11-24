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

import warnings

with warnings.catch_warnings():
    warnings.simplefilter("ignore", DeprecationWarning)
    import cgi

import hashlib
import hmac
import json
import os
import secrets
import shutil
import threading
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

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
FILES_DIR = QUEUE_DIR / "files"
FILES_INDEX = QUEUE_DIR / "files.json"
MAX_UPLOAD_BYTES = 20 * 1024 * 1024

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
_files_lock = threading.RLock()
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


DEFAULT_ALLOWED_USERS = ("tutnt", "zhl")


def allowed_users() -> set[str]:
    raw = os.environ.get("PANEL_ALLOWED_USERS") or ",".join(DEFAULT_ALLOWED_USERS)
    return {part.strip() for part in raw.replace(" ", ",").split(",") if part.strip()}


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
        "perms": list(DEFAULT_PERMS),
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


DEFAULT_PERMS = ("upload", "download")


def user_perms(username: str) -> list[str]:
    if username not in allowed_users():
        return []
    record = load_accounts().get(username) or {}
    perms = record.get("perms")
    if isinstance(perms, list):
        return [str(p) for p in perms if p in DEFAULT_PERMS]
    # 兼容旧账号文件：默认开放上传和下载
    return list(DEFAULT_PERMS)


def has_perm(username: str, perm: str) -> bool:
    return perm in user_perms(username)


# ---------- 共享文件 ----------


def load_file_entries() -> list[dict]:
    data = _read_json(FILES_INDEX)
    entries = data.get("files") if data else None
    if not isinstance(entries, list):
        return []
    return [entry for entry in entries if isinstance(entry, dict)]


def save_file_entries(entries: list[dict]) -> None:
    FILES_INDEX.parent.mkdir(parents=True, exist_ok=True)
    tmp = FILES_INDEX.with_name(FILES_INDEX.name + ".tmp")
    with _files_lock:
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump({"version": 1, "files": entries}, fh, ensure_ascii=False, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, FILES_INDEX)


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
  main { padding: 24px; text-align: center; animation: rise 0.55s cubic-bezier(0.2, 0.7, 0.2, 1) both; }
  h1 {
    margin: 0 0 30px;
    font-size: clamp(34px, 7vw, 58px);
    line-height: 1.1;
    font-weight: 700;
    letter-spacing: -0.035em;
  }
  .actions { display: grid; gap: 12px; justify-items: center; }
  .btn {
    display: inline-block;
    min-width: 250px;
    max-width: 82vw;
    padding: 13px 28px;
    border-radius: 16px;
    background: rgba(255, 255, 255, 0.72);
    border: 1px solid rgba(15, 23, 42, 0.1);
    color: #0f172a;
    text-decoration: none;
    font-size: 15px;
    letter-spacing: 0.02em;
    backdrop-filter: blur(8px);
    box-shadow: 0 8px 24px rgba(15, 23, 42, 0.08);
    transition: transform 0.16s ease, box-shadow 0.16s ease, background 0.16s ease;
    animation: rise 0.5s cubic-bezier(0.2, 0.7, 0.2, 1) both;
  }
  .btn:nth-child(2) { animation-delay: 0.06s; }
  .btn:nth-child(3) { animation-delay: 0.12s; }
  .btn:nth-child(4) { animation-delay: 0.18s; }
  .btn:hover {
    transform: translateY(-2px);
    background: #ffffff;
    box-shadow: 0 14px 34px rgba(15, 23, 42, 0.14);
  }
  .btn.primary {
    background: #111827;
    border-color: #111827;
    color: #ffffff;
    box-shadow: 0 10px 30px rgba(15, 23, 42, 0.22);
  }
  .btn.primary:hover { background: #1f2937; }
  @keyframes rise {
    from { opacity: 0; transform: translateY(16px); }
    to { opacity: 1; transform: none; }
  }
  @media (prefers-reduced-motion: reduce) {
    main, .btn { animation: none; }
    .btn { transition: none; }
  }
  @media (prefers-color-scheme: dark) {
    body {
      color: #e5e7eb;
      background: radial-gradient(circle at 50% 18%, #111c33 0%, #0b1220 55%, #070b13 100%);
    }
    .btn { background: rgba(17, 28, 51, 0.72); border-color: #23314f; color: #e5e7eb; }
    .btn:hover { background: #17233d; }
    .btn.primary { background: #e5e7eb; border-color: #e5e7eb; color: #0b1220; }
    .btn.primary:hover { background: #ffffff; }
  }
</style>
</head>
<body>
<main>
  <h1>tuTNT's home</h1>
  <div class="actions">
    <a class="btn primary" href="/deploy/">Deploy</a>
    <a class="btn" href="/intro/">慧英斋项目介绍</a>
    <a class="btn" href="/manual/">慧英斋使用说明书</a>
    <a class="btn" href="/me/">我的</a>
  </div>
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
  .top { display: flex; justify-content: space-between; gap: 12px; margin-bottom: 16px; }
  .home { color: #6b7280; text-decoration: none; font-size: 13px; }
  .button-link {
    display: inline-block;
    padding: 9px 16px;
    border-radius: 8px;
    background: #2563eb;
    color: #fff;
    text-decoration: none;
    font-size: 15px;
  }
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
  <div class="top"><a class="home" href="/">← tuTNT's home</a><a class="home" href="/me/">我的</a></div>
  <h1>部署控制台</h1>
  <div class="sub">登录后可查看部署状态、更新服务端代码。登录与修改密码请到「我的」。</div>

  <section id="need-login" class="card hidden">
    <h2>请先登录</h2>
    <div class="sub" style="margin: 0 0 14px">部署控制台需要登录后才能使用。</div>
    <a class="button-link" href="/me/?next=/deploy/">前往「我的」登录</a>
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
      </div>
      <div id="msg" class="msg"></div>
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

  function showNeedLogin() {
    csrf = '';
    currentUser = '';
    stopPolling();
    $('app').classList.add('hidden');
    $('need-login').classList.remove('hidden');
  }

  async function showApp(user) {
    currentUser = user || '';
    $('who').textContent = currentUser;
    $('need-login').classList.add('hidden');
    $('app').classList.remove('hidden');
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
    showNeedLogin();
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
      showNeedLogin();
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
        showNeedLogin();
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
        showNeedLogin();
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

  $('refresh').addEventListener('click', loadStatus);
  $('deploy').addEventListener('click', deploy);
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

INTRO_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>慧英斋项目介绍</title>
<link rel="icon" href="data:,">
<style>
  :root { color-scheme: light dark; }
  * { box-sizing: border-box; }
  html, body { min-height: 100%; }
  body {
    margin: 0;
    min-height: 100vh;
    display: grid;
    place-items: center;
    font: 16px/1.7 -apple-system, BlinkMacSystemFont, "Segoe UI", "Noto Sans CJK SC", "Microsoft YaHei", sans-serif;
    color: #3f3a32;
    background: radial-gradient(circle at 50% 15%, #fbf7ef 0%, #f5f1e8 55%, #eee7da 100%);
  }
  main { padding: 24px; text-align: center; animation: rise 0.7s cubic-bezier(0.2, 0.7, 0.2, 1) both; }
  .seal {
    width: 78px; height: 78px; border-radius: 50%;
    display: grid; place-items: center; margin: 0 auto 26px;
    background: linear-gradient(145deg, #ad7a55, #8f5f3f);
    color: #fff; font-size: 34px; font-weight: 700;
    box-shadow: 0 14px 34px rgba(156, 107, 74, 0.28);
    animation: float 4.5s ease-in-out infinite;
  }
  h1 { margin: 0 0 28px; font-size: clamp(30px, 6vw, 48px); letter-spacing: -0.02em; font-family: "Songti SC", "Noto Serif CJK SC", serif; }
  .back { color: #8a8478; text-decoration: none; font-size: 14px; border-bottom: 1px solid transparent; transition: color .2s, border-color .2s; }
  .back:hover { color: #9c6b4a; border-color: #9c6b4a; }
  .files-card {
    max-width: 680px; margin: 34px auto 0; padding: 20px; text-align: left;
    background: rgba(255, 250, 242, 0.9); border: 1px solid #e8ddcc; border-radius: 18px;
    box-shadow: 0 14px 36px rgba(120, 95, 65, 0.08);
  }
  .files-card h2 { margin: 0 0 4px; font-size: 17px; font-family: inherit; }
  .files-sub { margin: 0 0 14px; color: #8a7a68; font-size: 13px; }
  .files-login { padding: 12px 14px; border: 1px dashed #d8c9b5; border-radius: 12px; background: #fffdf9; color: #8a7a68; font-size: 14px; }
  .files-login a { color: #9c6b4a; }
  .upload-row { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; margin-bottom: 12px; }
  .upload-row input[type=file] { border: 1px solid #dfcfba; border-radius: 10px; padding: 8px; background: #fffdf9; color: inherit; max-width: 100%; }
  .upload-row button { border: 0; border-radius: 10px; padding: 9px 18px; background: #9c6b4a; color: #fff; cursor: pointer; font: inherit; }
  .upload-row button:disabled { opacity: .5; cursor: not-allowed; }
  .upload-msg { font-size: 13px; color: #8a7a68; }
  .file-list { display: grid; gap: 8px; }
  .file-row { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; justify-content: space-between; padding: 10px 12px; border: 1px solid #eadfd0; border-radius: 11px; background: #fffdf9; }
  .file-name { font-size: 14px; word-break: break-all; }
  .file-meta { font-size: 12px; color: #8a7a68; }
  .file-dl { color: #9c6b4a; text-decoration: none; font-size: 13px; border: 1px solid #d8c4ac; border-radius: 999px; padding: 4px 12px; }
  .file-dl:hover { background: #f2e6d7; }
  .empty { color: #a99c8b; font-size: 13px; padding: 8px 0; }
  .hidden { display: none !important; }
  @keyframes rise { from { opacity: 0; transform: translateY(16px); } to { opacity: 1; transform: none; } }
  @keyframes float { 0%, 100% { transform: translateY(0); } 50% { transform: translateY(-8px); } }
  @media (prefers-color-scheme: dark) {
    body { color: #ece4d8; background: radial-gradient(circle at 50% 15%, #221a14 0%, #171310 55%, #100d0a 100%); }
    .back { color: #a99c8b; }
    .back:hover { color: #d9b28e; border-color: #d9b28e; }
    .files-card { background: rgba(29, 23, 18, 0.92); border-color: #33281f; }
    .files-login, .upload-row input[type=file], .file-row { background: #241c16; border-color: #3a2d22; color: #ece4d8; }
    .files-sub, .upload-msg, .file-meta, .empty { color: #a99c8b; }
    .file-dl { color: #dcb894; border-color: #4a3829; }
  }
  @media (prefers-reduced-motion: reduce) { main, .seal { animation: none; } }
</style>
</head>
<body>
<main>
  <div class="seal">斋</div>
  <h1>慧英斋项目介绍</h1>
  <a class="back" href="/">← 返回主页</a>

  <section class="files-card">
    <h2>项目介绍资料</h2>
    <div class="files-sub">仅 tutnt / zhl 登录后可上传和下载；单个文件 ≤ 20MB。</div>
    <div id="files-login" class="files-login">登录后可上传/下载 · <a href="/me/?next=/intro/">去登录</a></div>
    <div id="files-main" class="hidden">
      <div id="upload-row" class="upload-row">
        <input id="file-input" type="file">
        <button id="upload-btn">上传</button>
        <span id="upload-msg" class="upload-msg"></span>
      </div>
      <div id="file-list" class="file-list"><div class="empty">加载中…</div></div>
    </div>
  </section>
</main>
<script>
(() => {
  const CATEGORY = 'intro';
  const $ = (id) => document.getElementById(id);
  let csrf = '';
  let perms = [];

  function api(path, options) {
    options = options || {};
    const headers = Object.assign({}, options.headers || {});
    if (csrf) headers['X-CSRF-Token'] = csrf;
    return fetch(path, Object.assign({ credentials: 'same-origin', cache: 'no-store' }, options, { headers }));
  }

  function humanSize(bytes) {
    const n = Number(bytes) || 0;
    if (n < 1024) return n + ' B';
    if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB';
    return (n / 1024 / 1024).toFixed(1) + ' MB';
  }

  function fmtTime(ms) {
    if (!ms) return '';
    try { return new Date(ms).toLocaleString('zh-CN', { hour12: false }); }
    catch (err) { return ''; }
  }

  function render(files) {
    const box = $('file-list');
    box.textContent = '';
    if (!files.length) {
      const empty = document.createElement('div');
      empty.className = 'empty';
      empty.textContent = '还没有资料';
      box.appendChild(empty);
      return;
    }
    files.forEach((file) => {
      const row = document.createElement('div');
      row.className = 'file-row';
      const info = document.createElement('div');
      const name = document.createElement('div');
      name.className = 'file-name';
      name.textContent = file.name || '未命名文件';
      const meta = document.createElement('div');
      meta.className = 'file-meta';
      meta.textContent = humanSize(file.size) + ' · ' + (file.uploadedBy || '') + ' · ' + fmtTime(file.uploadedAt);
      info.appendChild(name);
      info.appendChild(meta);
      row.appendChild(info);
      if (perms.indexOf('download') >= 0) {
        const link = document.createElement('a');
        link.className = 'file-dl';
        link.textContent = '下载';
        link.href = '/me/api/files/download?id=' + encodeURIComponent(file.id || '');
        row.appendChild(link);
      }
      box.appendChild(row);
    });
  }

  async function loadFiles() {
    const box = $('file-list');
    let resp;
    try {
      resp = await api('/me/api/files?category=' + CATEGORY);
    } catch (err) {
      box.textContent = '读取资料失败';
      return;
    }
    if (resp.status === 401) { location.reload(); return; }
    if (!resp.ok) { box.textContent = '读取资料失败'; return; }
    const data = await resp.json();
    perms = Array.isArray(data.perms) ? data.perms : perms;
    $('upload-row').classList.toggle('hidden', perms.indexOf('upload') < 0);
    render(Array.isArray(data.files) ? data.files : []);
  }

  async function upload() {
    const input = $('file-input');
    const file = input.files && input.files[0];
    const msg = $('upload-msg');
    msg.textContent = '';
    if (!file) { msg.textContent = '请选择文件'; return; }
    if (file.size > 20 * 1024 * 1024) { msg.textContent = '文件超过 20MB 限制'; return; }
    const form = new FormData();
    form.append('file', file);
    $('upload-btn').disabled = true;
    msg.textContent = '上传中…';
    try {
      const resp = await api('/me/api/files?category=' + CATEGORY, { method: 'POST', body: form });
      if (resp.status === 401) { location.reload(); return; }
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) {
        msg.textContent = data.message || ('上传失败（HTTP ' + resp.status + '）');
        return;
      }
      msg.textContent = '上传成功';
      input.value = '';
      await loadFiles();
    } catch (err) {
      msg.textContent = '网络错误，请重试';
    } finally {
      $('upload-btn').disabled = false;
    }
  }

  async function init() {
    try {
      const resp = await fetch('/me/api/session', { credentials: 'same-origin', cache: 'no-store' });
      if (!resp.ok) return;
      const data = await resp.json();
      csrf = data.csrf || '';
      perms = Array.isArray(data.perms) ? data.perms : [];
      $('files-login').classList.add('hidden');
      $('files-main').classList.remove('hidden');
      if (perms.indexOf('upload') < 0) $('upload-row').classList.add('hidden');
      await loadFiles();
    } catch (err) { /* ignore */ }
  }

  $('upload-btn').addEventListener('click', upload);
  init();
})();
</script>
</body>
</html>
"""

MANUAL_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>慧英斋使用说明书</title>
<link rel="icon" href="data:,">
<style>
  :root { color-scheme: light dark; --brown: #9c6b4a; --ink: #3f3a32; --muted: #8a8478; --paper: #f5f1e8; --card: #fffaf2; --line: #e8ddcc; }
  * { box-sizing: border-box; }
  html { scroll-behavior: smooth; }
  body { margin: 0; font: 16px/1.75 -apple-system, BlinkMacSystemFont, "Segoe UI", "Noto Sans CJK SC", "Microsoft YaHei", sans-serif; color: var(--ink); background: var(--paper); }
  .hero { position: relative; min-height: 62vh; display: grid; place-items: center; overflow: hidden; text-align: center; padding: 70px 20px 90px; background: radial-gradient(circle at 50% 20%, #fffdf8 0%, #f7f1e6 48%, #efe6d7 100%); }
  .orb { position: absolute; border-radius: 50%; filter: blur(46px); opacity: .55; pointer-events: none; }
  .orb-a { width: 300px; height: 300px; background: #e8c9a8; top: -60px; left: -50px; animation: drift 11s ease-in-out infinite; }
  .orb-b { width: 260px; height: 260px; background: #d9e3d0; bottom: -70px; right: -40px; animation: drift 13s ease-in-out infinite reverse; }
  .hero-inner { position: relative; z-index: 1; animation: rise .9s cubic-bezier(.2,.7,.2,1) both; }
  .seal { width: 74px; height: 74px; border-radius: 50%; display: grid; place-items: center; margin: 0 auto 24px; background: linear-gradient(145deg, #ad7a55, #8f5f3f); color: #fff; font-size: 32px; font-weight: 700; box-shadow: 0 14px 34px rgba(156,107,74,.3); animation: float 4.5s ease-in-out infinite; }
  .hero h1 { margin: 0 0 10px; font-size: clamp(30px, 6vw, 50px); letter-spacing: -.02em; font-family: "Songti SC", "Noto Serif CJK SC", serif; }
  .hero-sub { margin: 0 0 26px; color: var(--muted); letter-spacing: .12em; font-size: 14px; }
  .hero-back { display: inline-block; padding: 10px 22px; border-radius: 999px; border: 1px solid #d8c9b5; color: #6f6153; text-decoration: none; font-size: 14px; transition: .2s; }
  .hero-back:hover { background: #fff; border-color: var(--brown); color: var(--brown); transform: translateY(-2px); }
  .scroll-hint { margin-top: 26px; color: #a99c8b; font-size: 12px; letter-spacing: .2em; animation: breathe 2.4s ease-in-out infinite; }
  .toc { position: sticky; top: 0; z-index: 10; display: flex; gap: 8px; overflow-x: auto; padding: 12px 18px; background: rgba(245,241,232,.9); backdrop-filter: blur(10px); border-bottom: 1px solid var(--line); scrollbar-width: none; }
  .toc::-webkit-scrollbar { display: none; }
  .toc a { flex: none; padding: 6px 14px; border-radius: 999px; background: #fffaf2; border: 1px solid var(--line); color: #6f6153; text-decoration: none; font-size: 13px; white-space: nowrap; transition: .2s; }
  .toc a:hover { background: var(--brown); border-color: var(--brown); color: #fff; }
  main { max-width: 980px; margin: 0 auto; padding: 38px 18px 70px; }
  .section { background: var(--card); border: 1px solid var(--line); border-radius: 20px; padding: 26px clamp(18px, 4vw, 38px); margin: 0 0 22px; box-shadow: 0 12px 36px rgba(120, 95, 65, .06); }
  .reveal { opacity: 0; transform: translateY(26px); transition: opacity .7s cubic-bezier(.2,.7,.2,1), transform .7s cubic-bezier(.2,.7,.2,1); }
  .reveal.in { opacity: 1; transform: none; }
  .sec-head { display: flex; align-items: center; gap: 14px; margin-bottom: 16px; }
  .sec-num { flex: none; width: 42px; height: 42px; border-radius: 14px; display: grid; place-items: center; background: #f2e6d7; color: var(--brown); font-weight: 700; font-size: 14px; letter-spacing: .04em; }
  .sec-head h2 { margin: 0; font-size: 21px; letter-spacing: -.01em; font-family: "Songti SC", "Noto Serif CJK SC", serif; }
  .section p { margin: 0 0 12px; }
  .section p:last-child { margin-bottom: 0; }
  .steps { display: grid; gap: 14px; margin: 6px 0 4px; }
  .step { display: grid; grid-template-columns: 32px 1fr; gap: 12px; align-items: start; }
  .step-num { width: 28px; height: 28px; border-radius: 50%; display: grid; place-items: center; background: #fff; border: 1px solid #dfcfba; color: var(--brown); font-size: 13px; font-weight: 700; }
  .step b { display: block; margin-bottom: 2px; font-size: 15px; }
  .step p { margin: 0; color: #6f675d; font-size: 14px; }
  .tip { margin-top: 16px; padding: 12px 15px; background: #f7efe2; border-left: 3px solid #c1936c; border-radius: 0 10px 10px 0; color: #6f5a45; font-size: 14px; }
  .roles { display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; margin-top: 6px; }
  .role { padding: 16px; border-radius: 14px; background: #fff; border: 1px solid var(--line); transition: transform .2s, box-shadow .2s; }
  .role:hover { transform: translateY(-3px); box-shadow: 0 12px 26px rgba(120,95,65,.1); }
  .role h3 { margin: 0 0 6px; font-size: 15px; color: var(--brown); }
  .role p { margin: 0; font-size: 13px; color: #6f675d; }
  .tags { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 10px; }
  .tag { font-size: 12px; padding: 3px 10px; border-radius: 999px; background: #f2e6d7; color: #7d5a40; }
  .faq-item { border-bottom: 1px dashed #e2d5c2; }
  .faq-item:last-child { border-bottom: 0; }
  .faq-q { width: 100%; display: flex; justify-content: space-between; gap: 14px; align-items: center; padding: 14px 0; border: 0; background: transparent; color: inherit; font: inherit; text-align: left; cursor: pointer; }
  .faq-q span { color: var(--brown); font-size: 18px; transition: transform .3s; }
  .faq-a { max-height: 0; overflow: hidden; transition: max-height .38s ease, padding .38s ease; color: #6f675d; font-size: 14px; }
  .faq-item.open .faq-a { max-height: 600px; padding-bottom: 12px; }
  .faq-item.open .faq-q span { transform: rotate(45deg); }
  .hidden { display: none !important; }
  .files-login { margin-top: 10px; padding: 12px 14px; border: 1px dashed #d8c9b5; border-radius: 12px; background: #fffdf9; color: #6f675d; font-size: 14px; }
  .files-login a { color: var(--brown); }
  .upload-row { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; margin: 6px 0 12px; }
  .upload-row input[type=file] { border: 1px solid #dfcfba; border-radius: 10px; padding: 8px; background: #fffdf9; color: inherit; max-width: 100%; }
  .upload-row button { border: 0; border-radius: 10px; padding: 9px 18px; background: var(--brown); color: #fff; cursor: pointer; font: inherit; }
  .upload-row button:disabled { opacity: .5; cursor: not-allowed; }
  .upload-msg { font-size: 13px; color: var(--muted); }
  .file-list { display: grid; gap: 8px; }
  .file-row { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; justify-content: space-between; padding: 10px 12px; border: 1px solid #eadfd0; border-radius: 11px; background: #fffdf9; }
  .file-name { font-size: 14px; word-break: break-all; }
  .file-meta { font-size: 12px; color: var(--muted); }
  .file-dl { color: var(--brown); text-decoration: none; font-size: 13px; border: 1px solid #d8c4ac; border-radius: 999px; padding: 4px 12px; }
  .file-dl:hover { background: #f2e6d7; }
  .empty { color: var(--muted); font-size: 13px; padding: 8px 0; }
  footer { text-align: center; padding: 30px 18px 50px; color: #9a8f80; font-size: 12px; letter-spacing: .08em; }
  footer a { color: var(--brown); text-decoration: none; }
  @keyframes rise { from { opacity: 0; transform: translateY(20px); } to { opacity: 1; transform: none; } }
  @keyframes float { 0%, 100% { transform: translateY(0); } 50% { transform: translateY(-8px); } }
  @keyframes drift { 0%, 100% { transform: translate(0, 0) scale(1); } 50% { transform: translate(26px, 18px) scale(1.08); } }
  @keyframes breathe { 0%, 100% { opacity: .45; } 50% { opacity: 1; } }
  @media (max-width: 700px) {
    .roles { grid-template-columns: 1fr; }
    .hero { min-height: 54vh; padding: 54px 18px 70px; }
    .section { padding: 22px 18px; border-radius: 16px; }
  }
  @media (prefers-reduced-motion: reduce) {
    html { scroll-behavior: auto; }
    *, *::before, *::after { animation: none !important; transition: none !important; }
    .reveal { opacity: 1; transform: none; }
  }
  @media (prefers-color-scheme: dark) {
    :root { --ink: #ece4d8; --muted: #a99c8b; --paper: #14100d; --card: #1d1712; --line: #33281f; }
    .hero { background: radial-gradient(circle at 50% 20%, #241b14 0%, #191410 48%, #100d0a 100%); }
    .orb-a { background: #5a3f2b; } .orb-b { background: #33402f; }
    .toc { background: rgba(20,16,13,.9); }
    .toc a { background: #1d1712; color: #c9b9a6; }
    .toc a:hover { background: var(--brown); color: #fff; }
    .role { background: #241c16; }
    .sec-num, .tag { background: #33261c; color: #dcb894; }
    .step-num { background: #241c16; border-color: #4a3829; }
    .step p, .role p, .faq-a { color: #b8aa99; }
    .tip { background: #2a2119; color: #d9c2a8; border-left-color: #a97a54; }
    .faq-item { border-bottom-color: #3a2d22; }
    .files-login, .upload-row input[type=file], .file-row { background: #241c16; border-color: #3a2d22; color: #ece4d8; }
    .file-dl { color: #dcb894; border-color: #4a3829; }
    .hero-back { color: #c9b9a6; border-color: #4a3829; }
    .hero-back:hover { background: #241c16; color: #e0b993; border-color: var(--brown); }
  }
</style>
<noscript><style>.reveal { opacity: 1; transform: none; } .faq-a { max-height: none; padding-bottom: 12px; }</style></noscript>
</head>
<body>
<header class="hero">
  <div class="orb orb-a"></div>
  <div class="orb orb-b"></div>
  <div class="hero-inner">
    <div class="seal">斋</div>
    <h1>慧英斋 · 使用说明书</h1>
    <p class="hero-sub">作业 · 活动 · 交流 · 班务，一册在手</p>
    <a class="hero-back" href="/">← 返回主页</a>
    <div class="scroll-hint">向下滑动阅读 ↓</div>
  </div>
</header>

<nav class="toc" aria-label="目录">
  <a href="#start">快速开始</a>
  <a href="#register">登记与身份</a>
  <a href="#homework">作业簿</a>
  <a href="#publish">发布作业</a>
  <a href="#activity">活动</a>
  <a href="#forum">交流</a>
  <a href="#subject">学科板块</a>
  <a href="#notice">通知与提醒</a>
  <a href="#mine">我的</a>
  <a href="#web">网页端</a>
  <a href="#attach">附件规则</a>
  <a href="#files">共享资料</a>
  <a href="#faq">常见问题</a>
</nav>

<main>
  <section id="start" class="section reveal">
    <div class="sec-head"><span class="sec-num">01</span><h2>快速开始</h2></div>
    <div class="steps">
      <div class="step"><span class="step-num">1</span><div><b>进入小程序</b><p>在微信中打开「慧英斋」小程序（老师或班委发的入口 / 二维码）。</p></div></div>
      <div class="step"><span class="step-num">2</span><div><b>微信登录</b><p>首次进入会自动完成微信登录，无需注册账号密码。</p></div></div>
      <div class="step"><span class="step-num">3</span><div><b>入斋登记</b><p>进入底部「我的」→「立即登记」，填写真实姓名和学号。班级会自动带出，职位按班级名单自动匹配。</p></div></div>
      <div class="step"><span class="step-num">4</span><div><b>开始使用</b><p>登记完成后即可查看作业、活动、交流，并在「通知」里开启作业提醒。</p></div></div>
    </div>
    <div class="tip">学号与微信账号一一对应，请填写本人真实学号，不要冒用他人学籍。资料填错可随时在「我的」里修改。</div>
  </section>

  <section id="register" class="section reveal">
    <div class="sec-head"><span class="sec-num">02</span><h2>登记与身份</h2></div>
    <p>登记只需要姓名和学号；班级由系统带出，职位由班级名单（roster）自动匹配。身份决定你能做什么：</p>
    <div class="roles">
      <div class="role"><h3>普通同学</h3><p>查看作业与活动、参与交流、接收通知、登记/修改个人资料。</p></div>
      <div class="role"><h3>班委 / 管理员</h3><p>发布和删除作业、活动；管理员还可以统一编辑各科目的课程设置。</p></div>
      <div class="role"><h3>课代表</h3><p>由名单指定，可在对应学科板块发布任务通知、管理本科目任务与附件。</p></div>
    </div>
    <div class="tags"><span class="tag">姓名</span><span class="tag">学号</span><span class="tag">班级自动</span><span class="tag">职位自动</span></div>
  </section>

  <section id="homework" class="section reveal">
    <div class="sec-head"><span class="sec-num">03</span><h2>作业簿</h2></div>
    <p>底部「作业」页分为「学科板块」和「班级作业」两部分。学科板块按科目进入，班级作业汇总所有班级作业。</p>
    <div class="steps">
      <div class="step"><span class="step-num">1</span><div><b>看状态</b><p>「进行中」表示还没到截止时间；「已截止」表示已过期。卡片会显示布置人、截止时间和附件数量。</p></div></div>
      <div class="step"><span class="step-num">2</span><div><b>看详情</b><p>点击作业进入详情，查看完整说明和附件。图片可预览，文档可查看/下载。</p></div></div>
      <div class="step"><span class="step-num">3</span><div><b>收提醒</b><p>右上角铃铛进入「通知」，开启「作业提醒」后，新作业会通过微信订阅消息提醒你。</p></div></div>
    </div>
    <div class="tip">班委或管理员可以长按作业卡片删除作业；普通同学长按无效。</div>
  </section>

  <section id="publish" class="section reveal">
    <div class="sec-head"><span class="sec-num">04</span><h2>发布作业</h2></div>
    <p>只有班委或管理员能看到「＋ 发布作业」按钮。</p>
    <div class="steps">
      <div class="step"><span class="step-num">1</span><div><b>填标题和说明</b><p>标题必填；说明选填，最多 500 字，建议写清提交方式和注意事项。</p></div></div>
      <div class="step"><span class="step-num">2</span><div><b>选截止时间</b><p>选择日期和时间。截止后作业会显示「已截止」，但仍可查看详情和附件。</p></div></div>
      <div class="step"><span class="step-num">3</span><div><b>加附件</b><p>最多 5 个，单个不超过 20MB。文档从微信聊天记录选择；图片可从相册或拍照添加。</p></div></div>
      <div class="step"><span class="step-num">4</span><div><b>发布</b><p>发布后所有同学可见，并会生成站内通知；配置了订阅消息模板时还会推送微信提醒。</p></div></div>
    </div>
  </section>

  <section id="activity" class="section reveal">
    <div class="sec-head"><span class="sec-num">05</span><h2>活动</h2></div>
    <p>底部「活动」汇总班级活动。卡片展示活动名称、时间、地点和发布人；未开始显示「待举办」，过期显示「已结束」。</p>
    <div class="steps">
      <div class="step"><span class="step-num">1</span><div><b>查看活动</b><p>点击卡片查看完整活动说明、时间、地点和发布人。</p></div></div>
      <div class="step"><span class="step-num">2</span><div><b>发布活动</b><p>班委或管理员点击「＋ 发布活动」，填写名称、时间、地点和说明后发布。</p></div></div>
      <div class="step"><span class="step-num">3</span><div><b>管理活动</b><p>班委或管理员长按活动卡片，或在详情页点击「删除活动」即可删除。</p></div></div>
    </div>
  </section>

  <section id="forum" class="section reveal">
    <div class="sec-head"><span class="sec-num">06</span><h2>交流</h2></div>
    <p>底部「交流」是班级自由讨论区，所有登记同学都可以发帖和回复。</p>
    <div class="steps">
      <div class="step"><span class="step-num">1</span><div><b>发帖</b><p>输入内容，最多 1000 字；可附文件或照片（最多 5 个、单个不超过 20MB）。</p></div></div>
      <div class="step"><span class="step-num">2</span><div><b>回复</b><p>点进帖子，在底部输入回复；回复也可以带附件。</p></div></div>
      <div class="step"><span class="step-num">3</span><div><b>删除</b><p>长按自己的帖子可删除；班委和管理员可以管理所有人的帖子。</p></div></div>
    </div>
    <div class="tip">交流区适合讨论题目、分享资料、约活动；重要通知仍以「作业」和「通知」为准。</div>
  </section>

  <section id="subject" class="section reveal">
    <div class="sec-head"><span class="sec-num">07</span><h2>学科板块</h2></div>
    <p>在「作业」页点击任一学科卡片，进入该科目的专属板块。</p>
    <div class="steps">
      <div class="step"><span class="step-num">1</span><div><b>课程设置</b><p>展示任课老师、学分和课程安排；管理员可以点「编辑课程设置」统一修改。</p></div></div>
      <div class="step"><span class="step-num">2</span><div><b>任务通知</b><p>课代表可以发布本科目任务通知，支持截止时间和附件；内容可展开/收起。</p></div></div>
      <div class="step"><span class="step-num">3</span><div><b>管理任务</b><p>任务发布者、本科目课代表或班委/管理员可以长按删除任务通知。</p></div></div>
    </div>
    <div class="tip">课代表由班级名单指定；如需调整，请联系管理员修改名单。</div>
  </section>

  <section id="notice" class="section reveal">
    <div class="sec-head"><span class="sec-num">08</span><h2>通知与提醒</h2></div>
    <p>「作业」页右上角的铃铛是通知中心，红点/数字表示未读。</p>
    <div class="steps">
      <div class="step"><span class="step-num">1</span><div><b>开启提醒</b><p>进入「通知」→ 打开「作业提醒」开关。微信会请求订阅授权，建议勾选「总是保持以上选择」。</p></div></div>
      <div class="step"><span class="step-num">2</span><div><b>自动续订</b><p>提醒开启后会持续生效；额度不足时会自动续订，尽量做到全程无感。</p></div></div>
      <div class="step"><span class="step-num">3</span><div><b>查看通知</b><p>点击通知条目直接跳到对应作业或学科任务；未读条目会高亮显示。</p></div></div>
    </div>
    <div class="tip">如果收不到微信提醒，请检查是否授权了订阅消息；站内通知不受影响。</div>
  </section>

  <section id="mine" class="section reveal">
    <div class="sec-head"><span class="sec-num">09</span><h2>我的</h2></div>
    <p>底部「我的」展示个人信息和身份：姓名、学号、班级、职位、身份和负责科目。</p>
    <div class="steps">
      <div class="step"><span class="step-num">1</span><div><b>修改资料</b><p>点击「登记 / 修改资料」可以更新姓名、学号；班级和职位会按名单重新匹配。</p></div></div>
      <div class="step"><span class="step-num">2</span><div><b>查看身份</b><p>身份标签会显示「普通同学 / 班委 / 管理员」以及是否已登记。</p></div></div>
      <div class="step"><span class="step-num">3</span><div><b>负责科目</b><p>如果被名单指定为课代表，「负责科目」会列出你负责的科目。</p></div></div>
    </div>
  </section>

  <section id="web" class="section reveal">
    <div class="sec-head"><span class="sec-num">10</span><h2>网页端</h2></div>
    <p>除了小程序，班级还提供网页入口，用于查看说明和班级共享文件。</p>
    <div class="steps">
      <div class="step"><span class="step-num">1</span><div><b>主页</b><p>打开班级域名，可看到「慧英斋项目介绍」「慧英斋使用说明书」「我的」以及部署入口。</p></div></div>
      <div class="step"><span class="step-num">2</span><div><b>我的</b><p>使用班级账号登录后可以修改密码；共享资料在「项目介绍」和「使用说明书」页面，登录后可上传/下载（单个 ≤ 20MB）。</p></div></div>
      <div class="step"><span class="step-num">3</span><div><b>账号</b><p>网页账号由管理员分配；忘记密码可在登录后自行修改，或联系管理员重置。</p></div></div>
    </div>
    <div class="tip">网页「我的」与小程序「我的」不是同一个登录体系：小程序用微信登录，网页用班级账号。</div>
  </section>

  <section id="attach" class="section reveal">
    <div class="sec-head"><span class="sec-num">11</span><h2>附件规则</h2></div>
    <div class="steps">
      <div class="step"><span class="step-num">1</span><div><b>数量与大小</b><p>作业、活动和交流附件最多 5 个，单个不超过 20MB。</p></div></div>
      <div class="step"><span class="step-num">2</span><div><b>文件类型</b><p>文档支持 PDF / Word / Excel / PPT / TXT；图片支持相册选择或直接拍照。</p></div></div>
      <div class="step"><span class="step-num">3</span><div><b>来源</b><p>文档从微信聊天记录选择，请先把文件发到任意聊天（如文件传输助手）再选择。</p></div></div>
    </div>
    <div class="tip">上传前注意网络环境；大文件建议先压缩，避免超限或等待过久。</div>
  </section>

  <section id="files" class="section reveal">
    <div class="sec-head"><span class="sec-num">12</span><h2>共享资料</h2></div>
    <p>仅 tutnt / zhl 登录后可上传和下载本页资料；单个文件 ≤ 20MB。项目介绍页也有独立的资料区。</p>
    <div id="files-login" class="files-login">登录后可上传/下载 · <a href="/me/?next=/manual/">去登录</a></div>
    <div id="files-main" class="hidden">
      <div id="upload-row" class="upload-row">
        <input id="file-input" type="file">
        <button id="upload-btn">上传</button>
        <span id="upload-msg" class="upload-msg"></span>
      </div>
      <div id="file-list" class="file-list"><div class="empty">加载中…</div></div>
    </div>
  </section>

  <section id="faq" class="section reveal">
    <div class="sec-head"><span class="sec-num">13</span><h2>常见问题</h2></div>
    <div class="faq-item">
      <button class="faq-q">登记后职位不对怎么办？<span>＋</span></button>
      <div class="faq-a"><p>职位由班级名单按学号匹配。请确认学号填写正确；仍有问题请联系管理员更新名单。</p></div>
    </div>
    <div class="faq-item">
      <button class="faq-q">为什么看不到「发布作业」？<span>＋</span></button>
      <div class="faq-a"><p>发布作业仅对班委和管理员开放。普通同学可以查看作业、下载附件和参与交流。</p></div>
    </div>
    <div class="faq-item">
      <button class="faq-q">收不到作业提醒？<span>＋</span></button>
      <div class="faq-a"><p>在「通知」页确认「作业提醒」已开启，并允许微信订阅消息。站内通知始终可用。</p></div>
    </div>
    <div class="faq-item">
      <button class="faq-q">附件打不开或下载失败？<span>＋</span></button>
      <div class="faq-a"><p>确认文件没有超过 20MB，且网络正常。文档类附件会调用微信的文件查看/下载流程。</p></div>
    </div>
    <div class="faq-item">
      <button class="faq-q">网页登录忘记密码？<span>＋</span></button>
      <div class="faq-a"><p>已登录可在「我的」里修改密码；忘记密码请联系管理员重置，管理员可单独重置账号密码。</p></div>
    </div>
  </section>
</main>

<footer>慧英斋 · 使用说明书 · <a href="/">返回主页</a></footer>

<script>
(() => {
  const reveals = Array.from(document.querySelectorAll('.reveal'));
  reveals.forEach((el, index) => {
    el.style.transitionDelay = Math.min(index, 6) * 55 + 'ms';
  });
  if ('IntersectionObserver' in window) {
    const observer = new IntersectionObserver((entries) => {
      entries.forEach((entry) => {
        if (entry.isIntersecting) {
          entry.target.classList.add('in');
          observer.unobserve(entry.target);
        }
      });
    }, { threshold: 0.12 });
    reveals.forEach((el) => observer.observe(el));
  } else {
    reveals.forEach((el) => el.classList.add('in'));
  }

  document.querySelectorAll('.faq-q').forEach((button) => {
    button.addEventListener('click', () => {
      button.closest('.faq-item').classList.toggle('open');
    });
  });
})();
</script>
<script>
(() => {
  const CATEGORY = 'manual';
  const $ = (id) => document.getElementById(id);
  let csrf = '';
  let perms = [];

  function api(path, options) {
    options = options || {};
    const headers = Object.assign({}, options.headers || {});
    if (csrf) headers['X-CSRF-Token'] = csrf;
    return fetch(path, Object.assign({ credentials: 'same-origin', cache: 'no-store' }, options, { headers }));
  }

  function humanSize(bytes) {
    const n = Number(bytes) || 0;
    if (n < 1024) return n + ' B';
    if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB';
    return (n / 1024 / 1024).toFixed(1) + ' MB';
  }

  function fmtTime(ms) {
    if (!ms) return '';
    try { return new Date(ms).toLocaleString('zh-CN', { hour12: false }); }
    catch (err) { return ''; }
  }

  function render(files) {
    const box = $('file-list');
    box.textContent = '';
    if (!files.length) {
      const empty = document.createElement('div');
      empty.className = 'empty';
      empty.textContent = '还没有资料';
      box.appendChild(empty);
      return;
    }
    files.forEach((file) => {
      const row = document.createElement('div');
      row.className = 'file-row';
      const info = document.createElement('div');
      const name = document.createElement('div');
      name.className = 'file-name';
      name.textContent = file.name || '未命名文件';
      const meta = document.createElement('div');
      meta.className = 'file-meta';
      meta.textContent = humanSize(file.size) + ' · ' + (file.uploadedBy || '') + ' · ' + fmtTime(file.uploadedAt);
      info.appendChild(name);
      info.appendChild(meta);
      row.appendChild(info);
      if (perms.indexOf('download') >= 0) {
        const link = document.createElement('a');
        link.className = 'file-dl';
        link.textContent = '下载';
        link.href = '/me/api/files/download?id=' + encodeURIComponent(file.id || '');
        row.appendChild(link);
      }
      box.appendChild(row);
    });
  }

  async function loadFiles() {
    const box = $('file-list');
    let resp;
    try {
      resp = await api('/me/api/files?category=' + CATEGORY);
    } catch (err) {
      box.textContent = '读取资料失败';
      return;
    }
    if (resp.status === 401) { location.reload(); return; }
    if (!resp.ok) { box.textContent = '读取资料失败'; return; }
    const data = await resp.json();
    perms = Array.isArray(data.perms) ? data.perms : perms;
    $('upload-row').classList.toggle('hidden', perms.indexOf('upload') < 0);
    render(Array.isArray(data.files) ? data.files : []);
  }

  async function upload() {
    const input = $('file-input');
    const file = input.files && input.files[0];
    const msg = $('upload-msg');
    msg.textContent = '';
    if (!file) { msg.textContent = '请选择文件'; return; }
    if (file.size > 20 * 1024 * 1024) { msg.textContent = '文件超过 20MB 限制'; return; }
    const form = new FormData();
    form.append('file', file);
    $('upload-btn').disabled = true;
    msg.textContent = '上传中…';
    try {
      const resp = await api('/me/api/files?category=' + CATEGORY, { method: 'POST', body: form });
      if (resp.status === 401) { location.reload(); return; }
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) {
        msg.textContent = data.message || ('上传失败（HTTP ' + resp.status + '）');
        return;
      }
      msg.textContent = '上传成功';
      input.value = '';
      await loadFiles();
    } catch (err) {
      msg.textContent = '网络错误，请重试';
    } finally {
      $('upload-btn').disabled = false;
    }
  }

  async function init() {
    try {
      const resp = await fetch('/me/api/session', { credentials: 'same-origin', cache: 'no-store' });
      if (!resp.ok) return;
      const data = await resp.json();
      csrf = data.csrf || '';
      perms = Array.isArray(data.perms) ? data.perms : [];
      $('files-login').classList.add('hidden');
      $('files-main').classList.remove('hidden');
      if (perms.indexOf('upload') < 0) $('upload-row').classList.add('hidden');
      await loadFiles();
    } catch (err) { /* ignore */ }
  }

  $('upload-btn').addEventListener('click', upload);
  init();
})();
</script>
</body>
</html>
"""

MY_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>我的 · tuTNT's home</title>
<link rel="icon" href="data:,">
<style>
  :root { color-scheme: light dark; }
  * { box-sizing: border-box; }
  body { margin: 0; font: 15px/1.6 -apple-system, BlinkMacSystemFont, "Segoe UI", "Noto Sans CJK SC", "Microsoft YaHei", sans-serif; background: #f5f1e8; color: #2f2a24; }
  main { max-width: 880px; margin: 0 auto; padding: 24px 16px 64px; }
  .top { display: flex; justify-content: space-between; gap: 12px; margin-bottom: 16px; }
  .home { color: #8a7a68; text-decoration: none; font-size: 13px; }
  .home:hover { color: #9c6b4a; }
  h1 { font-size: 22px; margin: 0 0 4px; font-family: "Songti SC", "Noto Serif CJK SC", serif; }
  h2 { font-size: 16px; margin: 0 0 14px; }
  .sub { color: #8a7a68; font-size: 13px; margin-bottom: 20px; }
  .card { background: #fffaf2; border: 1px solid #e8ddcc; border-radius: 16px; padding: 18px; margin-bottom: 16px; box-shadow: 0 10px 30px rgba(120,95,65,.05); }
  .row { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; }
  button { font: inherit; border: 0; border-radius: 10px; padding: 9px 16px; cursor: pointer; }
  button.primary { background: #9c6b4a; color: #fff; }
  button.secondary { background: #f0e6d8; color: #5f4a37; }
  button.danger { background: #b4453a; color: #fff; }
  button:disabled { opacity: .5; cursor: not-allowed; }
  input { font: inherit; border: 1px solid #dfcfba; border-radius: 10px; padding: 9px 12px; width: 100%; max-width: 360px; background: #fffdf9; color: inherit; }
  label { font-size: 13px; color: #8a7a68; display: block; margin: 10px 0 6px; }
  .msg { margin-top: 10px; font-size: 13px; color: #8a7a68; min-height: 1.4em; }
  .who { font-size: 13px; color: #8a7a68; }
  .hidden { display: none !important; }
  .kv { display: grid; grid-template-columns: 86px 1fr; gap: 4px 12px; font-size: 13px; }
  .kv div:nth-child(odd) { color: #8a7a68; }
  .sub a { color: #9c6b4a; }
  .sub a:hover { text-decoration: underline; }
  @media (prefers-color-scheme: dark) {
    body { background: #14100d; color: #ece4d8; }
    .card { background: #1d1712; border-color: #33281f; }
    input { background: #241c16; border-color: #3a2d22; color: #ece4d8; }
    button.secondary { background: #33261c; color: #d9c8b3; }
    .home, .sub, label, .msg, .who, .kv div:nth-child(odd) { color: #a99c8b; }
    .sub a { color: #dcb894; }
  }
</style>
</head>
<body>
<main>
  <div class="top"><a class="home" href="/">← tuTNT's home</a><a class="home" href="/deploy/">部署控制台 →</a></div>
  <h1>我的</h1>
  <div class="sub">登录后可修改密码；共享资料请到「项目介绍」和「使用说明书」页面。</div>

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

  <section id="account" class="hidden">
    <div class="card">
      <div class="row" style="justify-content: space-between">
        <div><span class="who">已登录：</span><b id="who"></b></div>
        <button id="logout" class="secondary">退出</button>
      </div>
      <div class="kv" style="margin-top: 14px">
        <div>账号</div><div id="acc-user">—</div>
      </div>
    </div>

    <div class="card">
      <h2>共享资料</h2>
      <div class="sub" style="margin: 0">
        上传与下载已移到 <a href="/intro/">项目介绍</a> 和 <a href="/manual/">使用说明书</a> 页面，
        仅 tutnt / zhl 登录后可用。
      </div>
    </div>

    <div class="card">
      <h2>修改密码</h2>
      <label for="pwd-current">当前密码</label>
      <input id="pwd-current" type="password" autocomplete="current-password">
      <label for="pwd-new">新密码（6-128 位）</label>
      <input id="pwd-new" type="password" autocomplete="new-password">
      <label for="pwd-confirm">确认新密码</label>
      <input id="pwd-confirm" type="password" autocomplete="new-password">
      <div class="row" style="margin-top: 14px">
        <button id="pwd-save" class="primary">保存</button>
      </div>
      <div id="pwd-msg" class="msg"></div>
    </div>
  </section>
</main>
<script>
(() => {
  const $ = (id) => document.getElementById(id);
  let csrf = '';
  let user = '';
  const next = (() => {
    const value = new URLSearchParams(location.search).get('next') || '';
    return value.startsWith('/') && !value.startsWith('//') ? value : '';
  })();

  function api(path, options) {
    options = options || {};
    const headers = Object.assign({}, options.headers || {});
    if (csrf) headers['X-CSRF-Token'] = csrf;
    return fetch(path, Object.assign({ credentials: 'same-origin', cache: 'no-store' }, options, { headers }));
  }

  function showLogin(message) {
    csrf = ''; user = '';
    $('account').classList.add('hidden');
    $('login').classList.remove('hidden');
    $('login-msg').textContent = message || '';
    $('password').value = '';
    setTimeout(() => $('username').focus(), 30);
  }

  function showAccount(name) {
    user = name || '';
    $('who').textContent = user;
    $('acc-user').textContent = user;
    $('login').classList.add('hidden');
    $('account').classList.remove('hidden');
    $('pwd-msg').textContent = '';
  }

  async function checkSession() {
    try {
      const resp = await fetch('api/session', { credentials: 'same-origin', cache: 'no-store' });
      if (resp.ok) {
        const data = await resp.json();
        csrf = data.csrf || '';
        if (next) { location.href = next; return; }
        showAccount(data.user || '');
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
      if (next) { location.href = next; return; }
      showAccount(data.user || username);
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

  async function savePassword() {
    const current = $('pwd-current').value;
    const value = $('pwd-new').value;
    const confirmValue = $('pwd-confirm').value;
    const msg = $('pwd-msg');
    msg.style.color = '';
    if (!current || !value) { msg.textContent = '请填写当前密码和新密码'; return; }
    if (value !== confirmValue) { msg.textContent = '两次输入的新密码不一致'; return; }
    $('pwd-save').disabled = true;
    msg.textContent = '保存中…';
    try {
      const resp = await api('api/account/password', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ currentPassword: current, newPassword: value })
      });
      if (resp.status === 401) { showLogin('登录已过期，请重新登录'); return; }
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) {
        msg.style.color = '#b4453a';
        msg.textContent = data.message || ('修改失败（HTTP ' + resp.status + '）');
        return;
      }
      msg.textContent = '密码已更新';
      $('pwd-current').value = '';
      $('pwd-new').value = '';
      $('pwd-confirm').value = '';
    } catch (err) {
      msg.style.color = '#b4453a';
      msg.textContent = '网络错误，请重试';
    } finally {
      $('pwd-save').disabled = false;
    }
  }

  $('login-btn').addEventListener('click', login);
  $('password').addEventListener('keydown', (e) => { if (e.key === 'Enter') login(); });
  $('username').addEventListener('keydown', (e) => { if (e.key === 'Enter') $('password').focus(); });
  $('logout').addEventListener('click', logout);
  $('pwd-save').addEventListener('click', savePassword);

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

    def _route(self) -> tuple[str, str]:
        """返回 (区域, 面板内部路径)。区域为 '' / '/deploy' / '/me'。"""
        raw = urlparse(self.path).path
        for prefix in ("/deploy", "/me"):
            if raw == prefix or raw.startswith(prefix + "/"):
                return prefix, raw[len(prefix):] or "/"
        return "", raw

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
            "Path=/",
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
            "%s=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0" % SESSION_COOKIE,
        )

    @staticmethod
    def _legacy_expired_cookie() -> tuple[str, str]:
        # 清掉旧版本挂在 /deploy 路径上的同名 Cookie，避免与新 Cookie 冲突
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
        raw_path = urlparse(self.path).path
        for prefix in ("/deploy", "/me", "/intro", "/manual"):
            if raw_path == prefix:
                self._send(301, "", extra_headers=[("Location", prefix + "/")])
                return

        area, path = self._route()
        if area == "" and path == "/":
            self._send(200, HOME_HTML, "text/html; charset=utf-8")
            return
        if area == "" and path in ("/intro", "/intro/", "/intro/index.html"):
            self._send(200, INTRO_HTML, "text/html; charset=utf-8")
            return
        if area == "" and path in ("/manual", "/manual/", "/manual/index.html"):
            self._send(200, MANUAL_HTML, "text/html; charset=utf-8")
            return
        if area == "/deploy" and path in ("/", "/index.html"):
            self._send(200, DEPLOY_HTML, "text/html; charset=utf-8")
            return
        if area == "/me" and path in ("/", "/index.html"):
            self._send(200, MY_HTML, "text/html; charset=utf-8")
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
                    {
                        "ok": True,
                        "user": record["user"],
                        "csrf": record["csrf"],
                        "perms": user_perms(record["user"]),
                    },
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
        if path == "/api/files":
            self._handle_files_list()
            return
        if path == "/api/files/download":
            self._handle_file_download()
            return
        self._send(404, json.dumps({"ok": False, "message": "接口不存在"}, ensure_ascii=False))

    def do_POST(self):
        global _last_deploy_request_at

        _area, path = self._route()
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
        if path == "/api/files":
            self._handle_file_upload()
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
        record = users.get(username) if username in allowed_users() else None
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
            json.dumps(
                {
                    "ok": True,
                    "user": username,
                    "csrf": csrf,
                    "perms": user_perms(username),
                },
                ensure_ascii=False,
            ),
            extra_headers=[self._session_cookie(token), self._legacy_expired_cookie()],
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
            extra_headers=[self._expired_cookie(), self._legacy_expired_cookie()],
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
            updated = make_record(new_password)
            updated["perms"] = saved.get("perms") or list(DEFAULT_PERMS)
            users[record["user"]] = updated
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

    def _handle_files_list(self):
        _token, record = self._require_session()
        if not record:
            return
        user = record["user"]
        perms = user_perms(user)
        if "download" not in perms and "upload" not in perms:
            self._send(403, json.dumps({"ok": False, "message": "没有文件权限"}, ensure_ascii=False))
            return
        query = parse_qs(urlparse(self.path).query)
        category = (query.get("category") or [""])[0].strip()
        if category and category not in ("intro", "manual"):
            self._send(400, json.dumps({"ok": False, "message": "资料分类不正确"}, ensure_ascii=False))
            return
        entries = [
            entry
            for entry in load_file_entries()
            if (not category or entry.get("category") == category)
            and (FILES_DIR / str(entry.get("id", ""))).is_file()
        ]
        entries.sort(key=lambda entry: int(entry.get("uploadedAt") or 0), reverse=True)
        self._send(
            200,
            json.dumps({"ok": True, "files": entries, "perms": perms}, ensure_ascii=False),
        )

    def _handle_file_upload(self):
        _token, record = self._require_session()
        if not record:
            return
        if not self._require_csrf(record):
            return
        user = record["user"]
        if not has_perm(user, "upload"):
            self._send(403, json.dumps({"ok": False, "message": "没有上传权限"}, ensure_ascii=False))
            return

        query = parse_qs(urlparse(self.path).query)
        category = (query.get("category") or [""])[0].strip()
        if category not in ("intro", "manual"):
            self._send(400, json.dumps({"ok": False, "message": "缺少资料分类"}, ensure_ascii=False))
            return

        content_type = self.headers.get("Content-Type") or ""
        if not content_type.lower().startswith("multipart/form-data"):
            self._send(415, json.dumps({"ok": False, "message": "请使用表单上传文件"}, ensure_ascii=False))
            return
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            length = 0
        if length <= 0:
            self._send(400, json.dumps({"ok": False, "message": "没有选择文件"}, ensure_ascii=False))
            return
        if length > MAX_UPLOAD_BYTES + 1024 * 1024:
            self.close_connection = True
            self._send(413, json.dumps({"ok": False, "message": "文件超过 20MB 限制"}, ensure_ascii=False))
            return

        environ = {
            "REQUEST_METHOD": "POST",
            "CONTENT_TYPE": content_type,
            "CONTENT_LENGTH": str(length),
        }
        try:
            form = cgi.FieldStorage(fp=self.rfile, headers=self.headers, environ=environ)
        except Exception:
            self._send(400, json.dumps({"ok": False, "message": "上传内容解析失败"}, ensure_ascii=False))
            return
        item = form["file"] if "file" in form else None
        if item is None or not getattr(item, "file", None):
            self._send(400, json.dumps({"ok": False, "message": "没有选择文件"}, ensure_ascii=False))
            return

        original = os.path.basename(str(getattr(item, "filename", "") or "file"))
        # 浏览器通常用 UTF-8 发送 filename，但 HTTP 头按 latin-1 解码，
        # 这里尝试还原；失败则保留原值。
        try:
            recovered = original.encode("latin-1").decode("utf-8")
            if recovered:
                original = recovered
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
        original = "".join(
            ch for ch in original if ch.isprintable() and ch not in "\r\n/\\"
        )[:200] or "file"

        file_id = secrets.token_hex(16)
        target = FILES_DIR / file_id
        size = 0
        try:
            FILES_DIR.mkdir(parents=True, exist_ok=True)
            with target.open("wb") as out:
                while True:
                    chunk = item.file.read(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > MAX_UPLOAD_BYTES:
                        raise ValueError("too large")
                    out.write(chunk)
        except ValueError:
            target.unlink(missing_ok=True)
            self._send(413, json.dumps({"ok": False, "message": "文件超过 20MB 限制"}, ensure_ascii=False))
            return
        except OSError as exc:
            target.unlink(missing_ok=True)
            self._send(500, json.dumps({"ok": False, "message": "保存文件失败：%s" % exc}, ensure_ascii=False))
            return

        if size == 0:
            target.unlink(missing_ok=True)
            self._send(400, json.dumps({"ok": False, "message": "文件为空"}, ensure_ascii=False))
            return

        entry = {
            "id": file_id,
            "name": original,
            "size": size,
            "category": category,
            "uploadedBy": user,
            "uploadedAt": _now_ms(),
        }
        try:
            with _files_lock:
                entries = load_file_entries()
                entries.append(entry)
                save_file_entries(entries)
        except OSError as exc:
            target.unlink(missing_ok=True)
            self._send(500, json.dumps({"ok": False, "message": "写入文件索引失败：%s" % exc}, ensure_ascii=False))
            return
        self._send(201, json.dumps({"ok": True, "file": entry}, ensure_ascii=False))

    def _handle_file_download(self):
        _token, record = self._require_session()
        if not record:
            return
        user = record["user"]
        if not has_perm(user, "download"):
            self._send(403, json.dumps({"ok": False, "message": "没有下载权限"}, ensure_ascii=False))
            return
        query = parse_qs(urlparse(self.path).query)
        file_id = (query.get("id") or [""])[0].strip()
        if len(file_id) != 32 or any(ch not in "0123456789abcdef" for ch in file_id):
            self._send(404, json.dumps({"ok": False, "message": "文件不存在"}, ensure_ascii=False))
            return
        entry = next(
            (item for item in load_file_entries() if item.get("id") == file_id), None
        )
        target = FILES_DIR / file_id
        if not entry or not target.is_file():
            self._send(404, json.dumps({"ok": False, "message": "文件不存在"}, ensure_ascii=False))
            return
        self._send_file(target, str(entry.get("name") or "download"))

    def _send_file(self, path: Path, name: str) -> None:
        try:
            size = path.stat().st_size
        except OSError:
            self._send(404, json.dumps({"ok": False, "message": "文件不存在"}, ensure_ascii=False))
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(size))
        self.send_header(
            "Content-Disposition",
            "attachment; filename=\"download\"; filename*=UTF-8''%s" % quote(name, safe=""),
        )
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        if self.command == "HEAD":
            return
        with path.open("rb") as fh:
            shutil.copyfileobj(fh, self.wfile, 64 * 1024)

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
