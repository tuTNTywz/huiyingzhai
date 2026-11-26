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
import http.client
import json
import os
import secrets
import threading
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

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
PAGES_DIR = QUEUE_DIR / "pages"
MAX_PAGE_BYTES = 2 * 1024 * 1024

APP_URL = (os.environ.get("PANEL_APP_URL") or "http://127.0.0.1:3000").rstrip("/")
APP_SECRET = (os.environ.get("PANEL_APP_SECRET") or "").strip()

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


# ---------- 页面内容（项目介绍 / 使用说明书） ----------


def page_path(slug: str) -> Path:
    return PAGES_DIR / (slug + ".html")


def load_page_bytes(slug: str) -> bytes:
    try:
        return page_path(slug).read_bytes()
    except OSError:
        builtin = INTRO_HTML if slug == "intro" else MANUAL_HTML
        return builtin.encode("utf-8")


def save_page_bytes(slug: str, data: bytes) -> None:
    PAGES_DIR.mkdir(parents=True, exist_ok=True)
    tmp = PAGES_DIR / (slug + ".html.tmp")
    with tmp.open("wb") as fh:
        fh.write(data)
    os.chmod(tmp, 0o644)
    os.replace(tmp, page_path(slug))


# ---------- 小程序网页版 ----------

WEB_HTML_PATH = Path(__file__).resolve().with_name("huiyingzhai_web.html")


def load_web_bytes() -> bytes:
    try:
        return WEB_HTML_PATH.read_bytes()
    except OSError:
        return (
            "<!doctype html><meta charset='utf-8'>"
            "<title>汇英斋</title>"
            "<p style='font:16px sans-serif;padding:24px'>"
            "网页版文件缺失，请重新运行 server/deploy/install.sh。</p>"
        ).encode("utf-8")


PAGE_TOOLBAR = """<div id="hyz-page-tool" class="hyz-page-tool">
  <div class="hyz-page-tool-title">页面编辑 · __LABEL__</div>
  <div class="hyz-page-tool-note">下载当前页面，在电脑上改好后上传，页面会立即更新。</div>
  <div class="hyz-page-tool-row">
    <a class="hyz-page-tool-btn secondary" href="/me/api/pages/download?page=__PAGE__">下载当前页面</a>
    <label class="hyz-page-tool-btn" for="hyz-page-file">上传新版本</label>
    <input id="hyz-page-file" class="hyz-page-tool-file" type="file" accept=".html,.htm,text/html">
  </div>
  <div id="hyz-page-msg" class="hyz-page-tool-msg"></div>
</div>
<style>
  .hyz-page-tool { position: fixed; right: 18px; bottom: 18px; z-index: 2147483000; width: min(340px, calc(100vw - 36px)); padding: 14px; border-radius: 14px; background: rgba(255, 250, 242, 0.97); border: 1px solid #e0cfb8; box-shadow: 0 18px 42px rgba(60, 40, 20, 0.2); font: 13px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", "Noto Sans CJK SC", "Microsoft YaHei", sans-serif; color: #3f3a32; text-align: left; }
  .hyz-page-tool-title { font-weight: 700; font-size: 14px; margin-bottom: 3px; }
  .hyz-page-tool-note { color: #8a7a68; font-size: 12px; margin-bottom: 10px; }
  .hyz-page-tool-row { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
  .hyz-page-tool-btn { display: inline-block; padding: 7px 12px; border-radius: 9px; background: #9c6b4a; color: #fff; text-decoration: none; font-size: 12px; cursor: pointer; border: 0; }
  .hyz-page-tool-btn.secondary { background: #f0e4d4; color: #5f4a37; }
  .hyz-page-tool-file { display: none; }
  .hyz-page-tool-msg { margin-top: 8px; min-height: 1.2em; color: #8a7a68; font-size: 12px; }
  @media (prefers-color-scheme: dark) {
    .hyz-page-tool { background: rgba(29, 23, 18, 0.97); border-color: #3a2d22; color: #ece4d8; }
    .hyz-page-tool-note, .hyz-page-tool-msg { color: #b8aa99; }
    .hyz-page-tool-btn.secondary { background: #33261c; color: #d9c8b3; }
  }
</style>
<script>
(function () {
  var page = '__PAGE__';
  var input = document.getElementById('hyz-page-file');
  var msg = document.getElementById('hyz-page-msg');
  var csrf = '';

  async function getCsrf() {
    if (csrf) return csrf;
    try {
      var resp = await fetch('/me/api/session', { credentials: 'same-origin', cache: 'no-store' });
      if (resp.ok) {
        var data = await resp.json();
        csrf = data.csrf || '';
      }
    } catch (err) { /* ignore */ }
    return csrf;
  }

  input.addEventListener('change', async function () {
    var file = input.files && input.files[0];
    if (!file) return;
    var lower = (file.name || '').toLowerCase();
    if (!lower.endsWith('.html') && !lower.endsWith('.htm')) {
      msg.textContent = '请选择 .html 文件';
      return;
    }
    if (file.size > 2 * 1024 * 1024) {
      msg.textContent = '页面文件不能超过 2MB';
      return;
    }
    var token = await getCsrf();
    if (!token) {
      msg.textContent = '登录已过期，请重新登录';
      return;
    }
    var form = new FormData();
    form.append('file', file);
    msg.textContent = '上传中…';
    try {
      var resp = await fetch('/me/api/pages?page=' + encodeURIComponent(page), {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'X-CSRF-Token': token },
        body: form
      });
      var data = await resp.json().catch(function () { return {}; });
      if (resp.status === 401) {
        location.href = '/me/?next=' + encodeURIComponent(location.pathname);
        return;
      }
      if (!resp.ok) {
        msg.textContent = data.message || ('上传失败（HTTP ' + resp.status + '）');
        input.value = '';
        return;
      }
      msg.textContent = '已更新，正在刷新…';
      location.reload();
    } catch (err) {
      msg.textContent = '网络错误，请重试';
      input.value = '';
    }
  });
})();
</script>"""


def page_toolbar(slug: str) -> str:
    label = "项目介绍" if slug == "intro" else "使用说明书"
    return PAGE_TOOLBAR.replace("__PAGE__", slug).replace("__LABEL__", label)


def inject_page_toolbar(content: bytes, slug: str) -> bytes:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return content
    toolbar = page_toolbar(slug)
    index = text.rfind("</body>")
    if index >= 0:
        text = text[:index] + toolbar + text[index:]
    else:
        text = text + toolbar
    return text.encode("utf-8")


# ---------- 小程序后端 ----------


def _app_conn(timeout: int = 20):
    parsed = urlparse(APP_URL)
    return http.client.HTTPConnection(
        parsed.hostname or "127.0.0.1", parsed.port or 3000, timeout=timeout
    )


def app_json(path: str, payload=None, token: str = "", method: str = ""):
    conn = _app_conn()
    headers = {"Content-Type": "application/json"}
    if APP_SECRET:
        headers["X-Panel-Secret"] = APP_SECRET
    if token:
        headers["Authorization"] = "Bearer " + token
    body = (
        json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if payload is not None
        else None
    )
    try:
        conn.request(method or ("POST" if body is not None else "GET"), path, body=body, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        status = resp.status
    except OSError as exc:
        return 502, {"ok": False, "msg": "无法连接小程序后端：%s" % exc}
    finally:
        conn.close()
    try:
        data = json.loads(raw.decode("utf-8")) if raw else {}
    except (ValueError, UnicodeDecodeError):
        data = {"ok": False, "msg": "后端返回格式异常"}
    return status, data


def app_upload(raw: bytes, content_type: str, token: str):
    conn = _app_conn(timeout=120)
    headers = {
        "Content-Type": content_type,
        "Content-Length": str(len(raw)),
        "Authorization": "Bearer " + token,
    }
    try:
        conn.request("POST", "/api/upload", body=raw, headers=headers)
        resp = conn.getresponse()
        body = resp.read()
        status = resp.status
    except OSError as exc:
        return 502, {"ok": False, "msg": "无法连接小程序后端：%s" % exc}
    finally:
        conn.close()
    try:
        data = json.loads(body.decode("utf-8")) if body else {}
    except (ValueError, UnicodeDecodeError):
        data = {"ok": False, "msg": "后端返回格式异常"}
    return status, data


def identity_from_app(data: dict) -> dict:
    user = data.get("user") or {}
    roster = data.get("roster") or {}
    rep = data.get("repSubjects") or roster.get("repSubjects") or []
    return {
        "studentId": str(user.get("studentId") or roster.get("studentId") or ""),
        "name": user.get("name") or roster.get("name") or "",
        "className": user.get("className") or roster.get("className") or "",
        "position": user.get("position") or roster.get("position") or "",
        "role": user.get("role") or "",
        "repSubjects": rep if isinstance(rep, list) else [],
        "subjects": data.get("subjects") or [],
        "registered": bool(user.get("registered")),
    }


def fetch_app_identity(student_id: str):
    status, data = app_json("/api/web/token", {"studentId": student_id})
    if status == 200 and data.get("ok"):
        return data, ""
    return None, data.get("msg") or "无法同步小程序身份（检查共享密钥）"


def fetch_app_committee():
    status, data = app_json("/api/web/committee", method="GET")
    if status == 200 and data.get("ok"):
        return data.get("list") or [], data.get("subjects") or [], ""
    return [], [], data.get("msg") or "无法读取班委/课代表名单"


# ---------- 会话 ----------


def create_session(username: str, app_token: str = "", identity=None) -> tuple[str, str]:
    token = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(32)
    now = time.time()
    with _sessions_lock:
        _sessions[token] = {
            "user": username,
            "csrf": csrf,
            "app_token": app_token,
            "identity": identity,
            "expires": now + SESSION_TTL,
            "created": now,
        }
    return token, csrf


def refresh_app_token(record: dict) -> str:
    account = load_accounts().get(record.get("user") or "") or {}
    student_id = str(account.get("studentId") or "").strip()
    if not student_id:
        return ""
    data, _err = fetch_app_identity(student_id)
    if not data:
        return ""
    record["app_token"] = str(data.get("token") or "")
    record["identity"] = identity_from_app(data)
    return record["app_token"]


def ensure_app_token(record: dict) -> str:
    token = str(record.get("app_token") or "")
    if token:
        return token
    return refresh_app_token(record)


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
    <a class="btn primary" href="/huiyingzhai/">进入汇英斋</a>
    <a class="btn" href="/intro/">汇英斋项目介绍</a>
    <a class="btn" href="/manual/">汇英斋使用说明书</a>
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
<title>Deploy · 汇英斋</title>
<link rel="icon" href="data:,">
<style>
  body { margin: 0; font: 15px/1.7 -apple-system, BlinkMacSystemFont, "Segoe UI", "Noto Sans CJK SC", "Microsoft YaHei", sans-serif; background: #f5f1e8; color: #2f2a24; }
  main { max-width: 720px; margin: 0 auto; padding: 40px 18px; }
  .home { color: #8a7a68; text-decoration: none; font-size: 13px; }
  h1 { font-family: "Songti SC", "Noto Serif CJK SC", serif; font-size: 28px; margin: 14px 0 8px; }
  .card { background: #fffaf2; border: 1px solid #e8ddcc; border-radius: 16px; padding: 22px; margin-top: 18px; }
  .card p { margin: 0 0 8px; }
  .card p:last-child { margin-bottom: 0; }
  @media (prefers-color-scheme: dark) { body { background: #14100d; color: #ece4d8; } .card { background: #1d1712; border-color: #33281f; } .home { color: #a99c8b; } }
</style>
</head>
<body>
<main>
  <a class="home" href="/">← 汇英斋</a>
  <h1>Deploy</h1>
  <div class="card">
    <p>部署控制已合并到 <b>微信小程序「我的」→ 部署控制台</b>。</p>
    <p>网页端不再单独提供部署入口。</p>
  </div>
</main>
</body>
</html>"""

INTRO_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>汇英斋项目介绍</title>
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
  @keyframes rise { from { opacity: 0; transform: translateY(16px); } to { opacity: 1; transform: none; } }
  @keyframes float { 0%, 100% { transform: translateY(0); } 50% { transform: translateY(-8px); } }
  @media (prefers-color-scheme: dark) {
    body { color: #ece4d8; background: radial-gradient(circle at 50% 15%, #221a14 0%, #171310 55%, #100d0a 100%); }
    .back { color: #a99c8b; }
    .back:hover { color: #d9b28e; border-color: #d9b28e; }
  }
  @media (prefers-reduced-motion: reduce) { main, .seal { animation: none; } }
</style>
</head>
<body>
<main>
  <div class="seal">斋</div>
  <h1>汇英斋项目介绍</h1>
  <a class="back" href="/">← 返回主页</a>
</main>
</body>
</html>"""

MANUAL_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>汇英斋使用说明书</title>
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
    <h1>汇英斋 · 使用说明书</h1>
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
  <a href="#faq">常见问题</a>
</nav>

<main>
  <section id="start" class="section reveal">
    <div class="sec-head"><span class="sec-num">01</span><h2>快速开始</h2></div>
    <div class="steps">
      <div class="step"><span class="step-num">1</span><div><b>进入小程序</b><p>在微信中打开「汇英斋」小程序（老师或班委发的入口 / 二维码）。</p></div></div>
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
    <p>除了小程序，班级还提供网页入口，用于查看项目介绍和使用说明书。</p>
    <div class="steps">
      <div class="step"><span class="step-num">1</span><div><b>主页</b><p>打开班级域名，可看到「汇英斋项目介绍」「汇英斋使用说明书」「我的」以及部署入口。</p></div></div>
      <div class="step"><span class="step-num">2</span><div><b>我的</b><p>使用班级账号登录后可以修改密码；项目介绍和使用说明书的编辑入口在对应页面，仅 tutnt / zhl 登录后可见。</p></div></div>
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

  <section id="faq" class="section reveal">
    <div class="sec-head"><span class="sec-num">12</span><h2>常见问题</h2></div>
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

<footer>汇英斋 · 使用说明书 · <a href="/">返回主页</a></footer>

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

</body>
</html>
"""

UPLOAD_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>汇英斋数据上传</title>
<link rel="icon" href="data:,">
<style>
  :root { color-scheme: light dark; }
  * { box-sizing: border-box; }
  body { margin: 0; font: 15px/1.6 -apple-system, BlinkMacSystemFont, "Segoe UI", "Noto Sans CJK SC", "Microsoft YaHei", sans-serif; background: #f5f1e8; color: #2f2a24; }
  main { max-width: 960px; margin: 0 auto; padding: 26px 16px 64px; }
  h1 { font-size: 23px; margin: 0 0 4px; font-family: "Songti SC", "Noto Serif CJK SC", serif; }
  h2 { font-size: 16px; margin: 0 0 12px; }
  .sub { color: #8a7a68; font-size: 13px; margin: 0 0 18px; }
  .card { background: #fffaf2; border: 1px solid #e8ddcc; border-radius: 16px; padding: 18px; margin-bottom: 16px; box-shadow: 0 10px 30px rgba(120,95,65,.05); }
  .row { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; }
  .space { justify-content: space-between; }
  button { font: inherit; border: 0; border-radius: 10px; padding: 9px 16px; cursor: pointer; }
  button.primary { background: #9c6b4a; color: #fff; }
  button.secondary { background: #f0e6d8; color: #5f4a37; }
  button.small { padding: 5px 12px; font-size: 12px; border-radius: 999px; }
  button:disabled { opacity: .5; cursor: not-allowed; }
  input, textarea, select { font: inherit; border: 1px solid #dfcfba; border-radius: 10px; padding: 9px 12px; width: 100%; max-width: 380px; background: #fffdf9; color: inherit; }
  textarea { max-width: 100%; min-height: 76px; resize: vertical; }
  label { font-size: 13px; color: #8a7a68; display: block; margin: 10px 0 6px; }
  .msg { margin-top: 10px; font-size: 13px; color: #8a7a68; min-height: 1.4em; }
  .hidden { display: none !important; }
  .kv { display: grid; grid-template-columns: 88px 1fr; gap: 4px 12px; font-size: 13px; }
  .kv div:nth-child(odd) { color: #8a7a68; }
  .person { display: flex; flex-wrap: wrap; gap: 10px; justify-content: space-between; align-items: center; padding: 11px 13px; border: 1px solid #eadfd0; border-radius: 12px; background: #fffdf9; margin-bottom: 8px; }
  .person-main { min-width: 220px; }
  .person-name { font-size: 14px; font-weight: 700; }
  .person-meta { font-size: 12px; color: #8a7a68; }
  .tag { display: inline-block; font-size: 11px; padding: 2px 8px; border-radius: 999px; background: #f2e6d7; color: #7d5a40; margin-left: 5px; }
  .tag.current { background: #9c6b4a; color: #fff; }
  .form-card { display: grid; gap: 0; }
  .form-actions { margin-top: 14px; }
  .role-note { margin-top: 8px; font-size: 12px; color: #8a7a68; }
  @media (max-width: 640px) {
    .person { align-items: flex-start; }
    input { max-width: 100%; }
  }
  @media (prefers-color-scheme: dark) {
    body { background: #14100d; color: #ece4d8; }
    .card { background: #1d1712; border-color: #33281f; }
    input, textarea, select, .person { background: #241c16; border-color: #3a2d22; color: #ece4d8; }
    button.secondary { background: #33261c; color: #d9c8b3; }
    .sub, label, .msg, .kv div:nth-child(odd), .person-meta, .role-note { color: #a99c8b; }
    .tag { background: #33261c; color: #dcb894; }
    .tag.current { background: #9c6b4a; color: #fff; }
  }
</style>
</head>
<body>
<main>
  <h1>汇英斋数据上传</h1>
  <div class="sub">在这里发布作业、活动和学科任务；身份、权限与小程序完全同步。</div>

  <section id="login" class="card hidden">
    <h2>用小程序身份登录</h2>
    <div class="sub" style="margin-bottom: 10px">填写小程序「我的」里登记的姓名和学号。</div>
    <label for="name">姓名</label>
    <input id="name" autocomplete="name">
    <label for="studentId">学号</label>
    <input id="studentId" inputmode="numeric" placeholder="小程序登记的学号">
    <div class="row" style="margin-top: 14px"><button id="login-btn" class="primary">进入</button></div>
    <div id="login-msg" class="msg"></div>
  </section>

  <section id="dashboard" class="hidden">
    <div class="card">
      <div class="row space">
        <div>当前登录：<b id="who"></b><span id="identity-tag" class="tag hidden"></span></div>
        <button id="logout" class="secondary">退出</button>
      </div>
      <div id="identity" class="kv hidden" style="margin-top: 12px">
        <div>姓名</div><div id="id-name">—</div>
        <div>学号</div><div id="id-sid">—</div>
        <div>班级</div><div id="id-class">—</div>
        <div>职位</div><div id="id-position">—</div>
        <div>身份</div><div id="id-role">—</div>
        <div>负责科目</div><div id="id-reps">—</div>
      </div>
      <div id="app-msg" class="msg"></div>
    </div>

    <div id="forms" class="hidden">
      <div id="homework-card" class="card form-card hidden">
        <h2>发布作业</h2>
        <label for="hw-title">作业标题</label>
        <input id="hw-title" placeholder="如：《论语》第三篇诵读">
        <label for="hw-desc">作业说明（选填）</label>
        <textarea id="hw-desc" placeholder="简述作业要求…"></textarea>
        <label for="hw-deadline">截止时间</label>
        <input id="hw-deadline" type="datetime-local">
        <label for="hw-files">附件（最多 5 个，单个 ≤20MB）</label>
        <input id="hw-files" type="file" multiple>
        <div class="form-actions"><button id="hw-submit" class="primary">发布作业</button></div>
        <div id="hw-msg" class="msg"></div>
      </div>

      <div id="activity-card" class="card form-card hidden">
        <h2>发布活动</h2>
        <label for="act-title">活动名称</label>
        <input id="act-title" placeholder="如：春日雅集 · 诗会">
        <label for="act-time">活动时间</label>
        <input id="act-time" type="datetime-local">
        <label for="act-place">活动地点</label>
        <input id="act-place" placeholder="如：教学楼 305">
        <label for="act-content">活动说明（选填）</label>
        <textarea id="act-content" placeholder="简述活动安排与注意事项…"></textarea>
        <div class="form-actions"><button id="act-submit" class="primary">发布活动</button></div>
        <div id="act-msg" class="msg"></div>
      </div>

      <div id="subject-card" class="card form-card hidden">
        <h2>发布学科任务</h2>
        <label for="sub-subject">科目</label>
        <select id="sub-subject"></select>
        <label for="sub-title">标题</label>
        <input id="sub-title" placeholder="如：第三单元作业提交提醒">
        <label for="sub-content">内容</label>
        <textarea id="sub-content" placeholder="填写任务要求、提交方式等…"></textarea>
        <label for="sub-deadline">截止时间（选填）</label>
        <input id="sub-deadline" type="datetime-local">
        <label for="sub-files">附件（最多 5 个，单个 ≤20MB）</label>
        <input id="sub-files" type="file" multiple>
        <div class="form-actions"><button id="sub-submit" class="primary">发布学科任务</button></div>
        <div id="sub-msg" class="msg"></div>
      </div>
    </div>
  </section>
</main>
<script>
(() => {
  const $ = (id) => document.getElementById(id);
  let csrf = '';
  let user = '';
  let identity = null;
  let subjects = [];

  function api(path, options) {
    options = options || {};
    const headers = Object.assign({}, options.headers || {});
    if (csrf) headers['X-CSRF-Token'] = csrf;
    return fetch(path, Object.assign({ credentials: 'same-origin', cache: 'no-store' }, options, { headers }));
  }

  function setMsg(id, text, isError) {
    const el = $(id);
    el.textContent = text || '';
    el.style.color = isError ? '#b4453a' : '';
  }

  function showLogin(message) {
    csrf = ''; user = ''; identity = null;
    $('dashboard').classList.add('hidden');
    $('login').classList.remove('hidden');
    $('login-msg').textContent = message || '';
    $('studentId').value = '';
    setTimeout(() => $('name').focus(), 30);
  }

  function showDashboard() {
    $('login').classList.add('hidden');
    $('dashboard').classList.remove('hidden');
  }

  function applySession(data) {
    csrf = data.csrf || '';
    user = data.user || '';
    identity = data.identity || null;
    if (identity && Array.isArray(identity.subjects) && identity.subjects.length) {
      subjects = identity.subjects;
    }
    if (identity && identity.name && (!user || user.indexOf('mp:') === 0 || user.indexOf('web:') === 0)) {
      user = identity.name;
    }
    $('who').textContent = user;
  }

  function subjectName(key) {
    const hit = subjects.find((item) => item.key === key);
    return hit ? hit.name : key;
  }

  function repSubjects() {
    return identity && Array.isArray(identity.repSubjects) ? identity.repSubjects : [];
  }

  function renderIdentity(appError) {
    if (!identity) { showLogin('请用姓名和学号登录'); return; }
    $('identity').classList.remove('hidden');
    $('identity-tag').classList.remove('hidden');
    $('forms').classList.remove('hidden');
    $('id-name').textContent = identity.name || '—';
    $('id-sid').textContent = identity.studentId || '—';
    $('id-class').textContent = identity.className || '—';
    $('id-position').textContent = identity.position || '—';
    const roleText = identity.role === 'admin' ? '管理员' : identity.role === 'committee' ? '班委' : identity.role === 'student' ? '普通同学' : '—';
    $('id-role').textContent = roleText;
    $('id-reps').textContent = repSubjects().length ? repSubjects().map(subjectName).join('、') : '—';
    $('identity-tag').textContent = roleText;
    if (appError) setMsg('app-msg', appError, true);
    else setMsg('app-msg', '');

    const admin = identity.role === 'admin';
    const committee = identity.role === 'committee' || admin;
    const study = admin || identity.position === '学习委员';
    const reps = repSubjects();
    const canSub = admin || reps.length > 0;
    $('homework-card').classList.toggle('hidden', !study);
    $('activity-card').classList.toggle('hidden', !committee);
    $('subject-card').classList.toggle('hidden', !canSub);
    const select = $('sub-subject');
    select.textContent = '';
    const allowed = admin ? subjects : subjects.filter((item) => reps.indexOf(item.key) >= 0);
    allowed.forEach((item) => {
      const option = document.createElement('option');
      option.value = item.key;
      option.textContent = item.name;
      select.appendChild(option);
    });
    if (!study && !committee && !canSub) {
      setMsg('app-msg', '当前身份没有上传权限；仅班委/课代表可上传', true);
    }
  }

  async function checkSession() {
    try {
      const resp = await fetch('/me/api/session', { credentials: 'same-origin', cache: 'no-store' });
      if (!resp.ok) { showLogin(''); return; }
      const data = await resp.json();
      applySession(data);
      showDashboard();
      renderIdentity('');
    } catch (err) {
      showLogin('网络错误，请重试');
    }
  }

  async function login() {
    const name = $('name').value.trim();
    const studentId = $('studentId').value.trim();
    if (!name || !studentId) { $('login-msg').textContent = '请输入姓名和学号'; return; }
    $('login-btn').disabled = true;
    $('login-msg').textContent = '登录中…';
    try {
      const resp = await fetch('/me/api/login', {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: name, studentId: studentId })
      });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) { $('login-msg').textContent = data.message || ('登录失败（HTTP ' + resp.status + '）'); return; }
      applySession(data);
      showDashboard();
      renderIdentity('');
    } catch (err) {
      $('login-msg').textContent = '网络错误，请重试';
    } finally {
      $('login-btn').disabled = false;
    }
  }

  async function logout() {
    try {
      await api('/me/api/logout', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: '{}'
      });
    } catch (err) { /* ignore */ }
    showLogin('');
  }

  function toMs(value) {
    if (!value) return 0;
    const time = new Date(value).getTime();
    return Number.isFinite(time) ? time : 0;
  }

  async function uploadFiles(input, dir) {
    const files = Array.from(input.files || []);
    if (files.length > 5) throw new Error('最多 5 个附件');
    const uploaded = [];
    for (const file of files) {
      if (file.size > 20 * 1024 * 1024) throw new Error('单个附件不能超过 20MB');
      const form = new FormData();
      form.append('file', file);
      form.append('dir', dir);
      const resp = await api('/me/api/app/upload', { method: 'POST', body: form });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok || !data.ok) throw new Error(data.msg || data.message || '附件上传失败');
      uploaded.push({ fileID: data.fileID, name: data.name, size: data.size, type: data.type });
    }
    return uploaded;
  }

  async function publishAction(action, data, msgId) {
    const resp = await api('/me/api/app/action', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action: action, data: data })
    });
    const result = await resp.json().catch(() => ({}));
    if (!resp.ok || !result.ok) throw new Error(result.msg || result.message || '发布失败');
    return result;
  }

  async function submitHomework() {
    const title = $('hw-title').value.trim();
    const deadline = toMs($('hw-deadline').value);
    if (!title || !deadline) { setMsg('hw-msg', '请填写标题和截止时间', true); return; }
    const button = $('hw-submit');
    button.disabled = true;
    try {
      setMsg('hw-msg', '正在上传附件…');
      const attachments = await uploadFiles($('hw-files'), 'homework');
      setMsg('hw-msg', '正在发布…');
      await publishAction('publishHomework', {
        title: title,
        desc: $('hw-desc').value.trim(),
        deadline: deadline,
        attachments: attachments
      }, 'hw-msg');
      setMsg('hw-msg', '作业已发布');
      $('hw-title').value = '';
      $('hw-desc').value = '';
      $('hw-deadline').value = '';
      $('hw-files').value = '';
    } catch (err) {
      setMsg('hw-msg', err.message || '发布失败', true);
    } finally {
      button.disabled = false;
    }
  }

  async function submitActivity() {
    const title = $('act-title').value.trim();
    const activityTime = toMs($('act-time').value);
    if (!title || !activityTime) { setMsg('act-msg', '请填写活动名称和时间', true); return; }
    const button = $('act-submit');
    button.disabled = true;
    try {
      setMsg('act-msg', '正在发布…');
      await publishAction('publishActivity', {
        title: title,
        activityTime: activityTime,
        place: $('act-place').value.trim() || '待定',
        content: $('act-content').value.trim()
      }, 'act-msg');
      setMsg('act-msg', '活动已发布');
      $('act-title').value = '';
      $('act-time').value = '';
      $('act-place').value = '';
      $('act-content').value = '';
    } catch (err) {
      setMsg('act-msg', err.message || '发布失败', true);
    } finally {
      button.disabled = false;
    }
  }

  async function submitSubject() {
    const subject = $('sub-subject').value;
    const title = $('sub-title').value.trim();
    if (!subject || !title) { setMsg('sub-msg', '请填写科目和标题', true); return; }
    const button = $('sub-submit');
    button.disabled = true;
    try {
      setMsg('sub-msg', '正在上传附件…');
      const attachments = await uploadFiles($('sub-files'), 'subject');
      setMsg('sub-msg', '正在发布…');
      await publishAction('publishSubjectTask', {
        subject: subject,
        title: title,
        content: $('sub-content').value.trim(),
        deadline: toMs($('sub-deadline').value),
        attachments: attachments
      }, 'sub-msg');
      setMsg('sub-msg', '学科任务已发布');
      $('sub-title').value = '';
      $('sub-content').value = '';
      $('sub-deadline').value = '';
      $('sub-files').value = '';
    } catch (err) {
      setMsg('sub-msg', err.message || '发布失败', true);
    } finally {
      button.disabled = false;
    }
  }

  async function ticketLogin(ticket) {
    try {
      const resp = await fetch('/me/api/ticket-login', {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ticket: ticket })
      });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok || !data.ok) {
        showLogin(data.message || '小程序登录已失效，请返回小程序重新进入');
        return;
      }
      applySession(data);
      showDashboard();
      renderIdentity('');
    } catch (err) {
      showLogin('网络错误，请重试');
    }
  }

  $('login-btn').addEventListener('click', login);
  $('studentId').addEventListener('keydown', (e) => { if (e.key === 'Enter') login(); });
  $('name').addEventListener('keydown', (e) => { if (e.key === 'Enter') $('studentId').focus(); });
  $('logout').addEventListener('click', logout);
  $('hw-submit').addEventListener('click', submitHomework);
  $('act-submit').addEventListener('click', submitActivity);
  $('sub-submit').addEventListener('click', submitSubject);

  let ticket = '';
  try {
    const hash = location.hash || '';
    const match = hash.match(/(?:^#|&)ticket=([^&]+)/);
    if (match) {
      ticket = decodeURIComponent(match[1]);
      history.replaceState(null, '', location.pathname + location.search);
    }
  } catch (err) { /* ignore */ }
  if (ticket) ticketLogin(ticket); else checkSession();
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
<title>我的 · 汇英斋</title>
<link rel="icon" href="data:,">
<style>
  body { margin: 0; font: 15px/1.7 -apple-system, BlinkMacSystemFont, "Segoe UI", "Noto Sans CJK SC", "Microsoft YaHei", sans-serif; background: #f5f1e8; color: #2f2a24; }
  main { max-width: 720px; margin: 0 auto; padding: 40px 18px; }
  .home { color: #8a7a68; text-decoration: none; font-size: 13px; }
  h1 { font-family: "Songti SC", "Noto Serif CJK SC", serif; font-size: 28px; margin: 14px 0 8px; }
  .card { background: #fffaf2; border: 1px solid #e8ddcc; border-radius: 16px; padding: 22px; margin-top: 18px; }
  .card p { margin: 0 0 10px; }
  .card p:last-child { margin-bottom: 0; }
  .links { margin-top: 18px; }
  .links a { display: inline-block; margin: 0 8px 8px 0; padding: 9px 16px; border-radius: 10px; background: #9c6b4a; color: #fff; text-decoration: none; font-size: 14px; }
  @media (prefers-color-scheme: dark) { body { background: #14100d; color: #ece4d8; } .card { background: #1d1712; border-color: #33281f; } .home { color: #a99c8b; } }
</style>
</head>
<body>
<main>
  <a class="home" href="/">← 汇英斋</a>
  <h1>我的</h1>
  <div class="card">
    <p>账号资料、数据上传和部署控制统一在 <b>微信小程序「我的」</b>。</p>
    <p>网页数据上传：在小程序「我的」复制上传链接，在浏览器打开后用 <b>姓名 + 学号</b> 登录。</p>
  </div>
  <div class="links">
    <a href="/intro/">项目介绍</a>
    <a href="/manual/">使用说明书</a>
  </div>
</main>
</body>
</html>"""


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

    def _page_editor(self):
        """仅管理员（姓名 + 学号登录）注入页面编辑工具条。"""
        _token, record = self._session_record()
        if not record:
            return None
        identity = record.get("identity") or {}
        if identity.get("role") != "admin":
            return None
        return record

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
        for prefix in ("/deploy", "/me", "/intro", "/manual", "/upload", "/huiyingzhai"):
            if raw_path == prefix:
                self._send(301, "", extra_headers=[("Location", prefix + "/")])
                return

        area, path = self._route()
        if area == "" and path == "/":
            self._send(200, HOME_HTML, "text/html; charset=utf-8")
            return
        if area == "" and path in ("/intro", "/intro/", "/intro/index.html"):
            content = load_page_bytes("intro")
            if self._page_editor():
                content = inject_page_toolbar(content, "intro")
            self._send(200, content, "text/html; charset=utf-8")
            return
        if area == "" and path in ("/manual", "/manual/", "/manual/index.html"):
            content = load_page_bytes("manual")
            if self._page_editor():
                content = inject_page_toolbar(content, "manual")
            self._send(200, content, "text/html; charset=utf-8")
            return
        if area == "/deploy" and path in ("/", "/index.html"):
            self._send(200, DEPLOY_HTML, "text/html; charset=utf-8")
            return
        if area == "/me" and path in ("/", "/index.html"):
            self._send(200, MY_HTML, "text/html; charset=utf-8")
            return
        if area == "" and path in ("/upload", "/upload/", "/upload/index.html"):
            self._send(200, UPLOAD_HTML, "text/html; charset=utf-8")
            return
        if area == "" and path in ("/huiyingzhai", "/huiyingzhai/", "/huiyingzhai/index.html"):
            self._send(200, load_web_bytes(), "text/html; charset=utf-8")
            return
        if path == "/healthz":
            self._send(200, json.dumps({"ok": True}))
            return
        if path == "/api/session":
            _token, record = self._session_record()
            if not record:
                self._send(401, json.dumps({"ok": False, "message": "未登录"}, ensure_ascii=False))
                return
            account = load_accounts().get(record["user"]) or {}
            student_id = str(account.get("studentId") or "").strip()
            self._send(
                200,
                json.dumps(
                    {
                        "ok": True,
                        "user": record["user"],
                        "csrf": record["csrf"],
                        "perms": user_perms(record["user"]),
                        "identity": record.get("identity"),
                        "studentId": student_id,
                        "bound": bool(student_id),
                        "appReady": bool(APP_SECRET),
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
        if path == "/api/pages/download":
            self._handle_page_download()
            return
        if path == "/api/app/committee":
            self._handle_app_committee()
            return
        if path == "/api/app/deploy/status":
            self._handle_app_deploy_status()
            return
        self._send(404, json.dumps({"ok": False, "message": "接口不存在"}, ensure_ascii=False))

    def do_POST(self):
        global _last_deploy_request_at

        _area, path = self._route()
        if path == "/api/login":
            self._handle_login()
            return
        if path == "/api/ticket-login":
            self._handle_ticket_login()
            return
        if path == "/api/logout":
            self._handle_logout()
            return
        if path == "/api/account/password":
            self._handle_password()
            return
        if path == "/api/account/bind":
            self._handle_bind()
            return
        if path == "/api/app/upload":
            self._handle_app_upload()
            return
        if path == "/api/app/action":
            self._handle_app_action()
            return
        if path == "/api/app/deploy":
            self._handle_app_deploy()
            return
        if path == "/api/pages":
            self._handle_page_upload()
            return
        self._send(404, json.dumps({"ok": False, "message": "接口不存在"}, ensure_ascii=False))

    def _handle_login(self):
        data = self._read_json()
        if data is None:
            return
        name = str(data.get("name") or "").strip()
        student_id = str(data.get("studentId") or "").strip()
        if not name or not student_id or len(name) > 40 or len(student_id) > 30:
            self._send(400, json.dumps({"ok": False, "message": "请填写姓名和学号"}, ensure_ascii=False))
            return
        ip = self._client_ip()
        if not _login_rate_ok(ip):
            time.sleep(0.5)
            self._send(429, json.dumps({"ok": False, "message": "尝试过于频繁，请稍后再试"}, ensure_ascii=False))
            return
        if not APP_SECRET:
            self._send(503, json.dumps({"ok": False, "message": "网页登录未启用（缺少共享密钥）"}, ensure_ascii=False))
            return
        app_data, error = fetch_app_identity(student_id)
        if not app_data:
            _login_failed(ip)
            time.sleep(0.4)
            self._send(401, json.dumps({"ok": False, "message": error or "学号尚未在小程序登记"}, ensure_ascii=False))
            return
        identity = identity_from_app(app_data)
        if (identity.get("name") or "").strip() != name:
            _login_failed(ip)
            time.sleep(0.4)
            self._send(401, json.dumps({"ok": False, "message": "姓名与学号不匹配"}, ensure_ascii=False))
            return
        _login_succeeded(ip)
        app_token = str(app_data.get("token") or "")
        token, csrf = create_session("web:" + student_id, app_token, identity)
        self._send(
            200,
            json.dumps(
                {
                    "ok": True,
                    "user": name,
                    "csrf": csrf,
                    "perms": ["upload", "download"],
                    "identity": identity,
                    "studentId": student_id,
                    "bound": True,
                    "appReady": True,
                    "nameLogin": True,
                },
                ensure_ascii=False,
            ),
            extra_headers=[self._session_cookie(token), self._legacy_expired_cookie()],
        )

    def _handle_ticket_login(self):
        data = self._read_json()
        if data is None:
            return
        ticket = str(data.get("ticket") or "").strip()
        if not ticket:
            self._send(400, json.dumps({"ok": False, "message": "缺少小程序登录凭证"}, ensure_ascii=False))
            return
        status, app_data = app_json("/api/web/me", token=ticket, method="GET")
        if status != 200 or not app_data.get("ok"):
            self._send(
                401,
                json.dumps(
                    {"ok": False, "message": app_data.get("msg") or "小程序登录已失效，请返回小程序重新进入"},
                    ensure_ascii=False,
                ),
            )
            return
        identity = identity_from_app(app_data)
        session_user = "mp:" + (identity.get("studentId") or "user")
        token, csrf = create_session(session_user, ticket, identity)
        self._send(
            200,
            json.dumps(
                {
                    "ok": True,
                    "user": identity.get("name") or "小程序用户",
                    "csrf": csrf,
                    "identity": identity,
                    "perms": ["upload", "download"],
                    "bound": True,
                    "appReady": bool(APP_SECRET),
                    "ticket": True,
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
            if "perms" in saved:
                updated["perms"] = saved["perms"]
            if "studentId" in saved:
                updated["studentId"] = saved["studentId"]
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

    def _app_token_for(self, record: dict) -> str:
        token = str(record.get("app_token") or "")
        if token:
            return token
        return refresh_app_token(record)

    def _handle_bind(self):
        _token, record = self._require_session()
        if not record:
            return
        if not self._require_csrf(record):
            return
        data = self._read_json()
        if data is None:
            return
        student_id = str(data.get("studentId") or "").strip()
        if not student_id or len(student_id) > 30:
            self._send(400, json.dumps({"ok": False, "message": "请填写学号"}, ensure_ascii=False))
            return
        if not APP_SECRET:
            self._send(503, json.dumps({"ok": False, "message": "网页上传未启用（缺少共享密钥）"}, ensure_ascii=False))
            return
        app_data, error = fetch_app_identity(student_id)
        if not app_data:
            self._send(400, json.dumps({"ok": False, "message": error}, ensure_ascii=False))
            return
        with _accounts_lock:
            users = load_accounts()
            account = users.get(record["user"])
            if not account:
                self._send(400, json.dumps({"ok": False, "message": "账号不存在"}, ensure_ascii=False))
                return
            account["studentId"] = student_id
            try:
                save_accounts(users)
            except OSError as exc:
                self._send(500, json.dumps({"ok": False, "message": "保存绑定失败：%s" % exc}, ensure_ascii=False))
                return
        identity = identity_from_app(app_data)
        record["app_token"] = str(app_data.get("token") or "")
        record["identity"] = identity
        self._send(
            200,
            json.dumps(
                {"ok": True, "identity": identity, "studentId": student_id, "appReady": True},
                ensure_ascii=False,
            ),
        )

    def _handle_app_committee(self):
        _token, record = self._require_session()
        if not record:
            return
        rows, subjects, error = fetch_app_committee()
        if error:
            self._send(502, json.dumps({"ok": False, "message": error}, ensure_ascii=False))
            return
        bindings: dict[str, list[str]] = {}
        for name, account in load_accounts().items():
            student_id = str(account.get("studentId") or "").strip()
            if student_id:
                bindings.setdefault(student_id, []).append(name)
        for row in rows:
            student_id = str(row.get("studentId") or "")
            row["webAccounts"] = sorted(bindings.get(student_id, []))
        current = (record.get("identity") or {}).get("studentId") or ""
        self._send(
            200,
            json.dumps(
                {
                    "ok": True,
                    "list": rows,
                    "subjects": subjects,
                    "bindings": bindings,
                    "currentStudentId": current,
                },
                ensure_ascii=False,
            ),
        )

    def _handle_app_upload(self):
        _token, record = self._require_session()
        if not record:
            return
        if not self._require_csrf(record):
            return
        app_token = self._app_token_for(record)
        if not app_token:
            self._send(400, json.dumps({"ok": False, "message": "请先绑定学号并同步小程序身份"}, ensure_ascii=False))
            return
        content_type = self.headers.get("Content-Type") or ""
        if not content_type.lower().startswith("multipart/form-data"):
            self._send(415, json.dumps({"ok": False, "message": "请使用表单上传附件"}, ensure_ascii=False))
            return
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            length = 0
        if length <= 0:
            self._send(400, json.dumps({"ok": False, "message": "没有选择文件"}, ensure_ascii=False))
            return
        if length > 25 * 1024 * 1024:
            self.close_connection = True
            self._send(413, json.dumps({"ok": False, "message": "附件超过 20MB 限制"}, ensure_ascii=False))
            return
        raw = self.rfile.read(length)
        status, data = app_upload(raw, content_type, app_token)
        if status == 401:
            new_token = refresh_app_token(record)
            if new_token:
                status, data = app_upload(raw, content_type, new_token)
        self._send(status, json.dumps(data, ensure_ascii=False))

    def _handle_app_action(self):
        _token, record = self._require_session()
        if not record:
            return
        if not self._require_csrf(record):
            return
        data = self._read_json()
        if data is None:
            return
        action = str(data.get("action") or "")
        allowed = {
            "listHomeworks", "getHomework",
            "listActivities", "getActivity",
            "listPosts", "getPost", "listReplies",
            "fetchHomeworkNotices",
            "listSubjectTasks", "getSubjectSettings", "getSubjectRep",
            "createPost", "createReply",
            "publishHomework", "publishActivity", "publishSubjectTask",
        }
        if action not in allowed:
            self._send(400, json.dumps({"ok": False, "message": "不支持的操作"}, ensure_ascii=False))
            return
        app_token = self._app_token_for(record)
        if not app_token:
            self._send(400, json.dumps({"ok": False, "message": "请先绑定学号并同步小程序身份"}, ensure_ascii=False))
            return
        payload = {
            "action": action,
            "data": data.get("data") if isinstance(data.get("data"), dict) else {},
        }
        identity = record.get("identity") or {}
        role = identity.get("role")
        position = identity.get("position")
        reps = identity.get("repSubjects") if isinstance(identity.get("repSubjects"), list) else []
        if action == "publishHomework" and not (role == "admin" or position == "学习委员"):
            self._send(403, json.dumps({"ok": False, "message": "仅学习委员或管理员可发布作业"}, ensure_ascii=False))
            return
        if action == "publishActivity" and role not in ("committee", "admin"):
            self._send(403, json.dumps({"ok": False, "message": "仅班委或管理员可发布活动"}, ensure_ascii=False))
            return
        if action == "publishSubjectTask":
            subject = str(payload["data"].get("subject") or "")
            if role != "admin" and subject not in reps:
                self._send(403, json.dumps({"ok": False, "message": "只能发布你负责的科目"}, ensure_ascii=False))
                return
        status, result = app_json("/api/action", payload, token=app_token)
        if status == 401:
            new_token = refresh_app_token(record)
            if new_token:
                status, result = app_json("/api/action", payload, token=new_token)
        self._send(status, json.dumps(result, ensure_ascii=False))

    def _handle_page_download(self):
        _token, record = self._require_session()
        if not record:
            return
        identity = record.get("identity") or {}
        if identity.get("role") != "admin":
            self._send(403, json.dumps({"ok": False, "message": "仅管理员可下载页面"}, ensure_ascii=False))
            return
        query = parse_qs(urlparse(self.path).query)
        slug = (query.get("page") or [""])[0].strip()
        if slug not in ("intro", "manual"):
            self._send(400, json.dumps({"ok": False, "message": "页面不正确"}, ensure_ascii=False))
            return
        data = load_page_bytes(slug)
        self._send(
            200,
            data,
            "text/html; charset=utf-8",
            extra_headers=[("Content-Disposition", 'attachment; filename="%s.html"' % slug)],
        )

    def _handle_page_upload(self):
        _token, record = self._require_session()
        if not record:
            return
        if not self._require_csrf(record):
            return
        identity = record.get("identity") or {}
        if identity.get("role") != "admin":
            self._send(403, json.dumps({"ok": False, "message": "仅管理员可编辑页面"}, ensure_ascii=False))
            return
        query = parse_qs(urlparse(self.path).query)
        slug = (query.get("page") or [""])[0].strip()
        if slug not in ("intro", "manual"):
            self._send(400, json.dumps({"ok": False, "message": "页面不正确"}, ensure_ascii=False))
            return

        content_type = self.headers.get("Content-Type") or ""
        if not content_type.lower().startswith("multipart/form-data"):
            self._send(415, json.dumps({"ok": False, "message": "请使用表单上传页面"}, ensure_ascii=False))
            return
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            length = 0
        if length <= 0:
            self._send(400, json.dumps({"ok": False, "message": "没有选择文件"}, ensure_ascii=False))
            return
        if length > MAX_PAGE_BYTES + 1024 * 1024:
            self.close_connection = True
            self._send(413, json.dumps({"ok": False, "message": "页面文件超过 2MB 限制"}, ensure_ascii=False))
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

        data = bytearray()
        try:
            while True:
                chunk = item.file.read(256 * 1024)
                if not chunk:
                    break
                data.extend(chunk)
                if len(data) > MAX_PAGE_BYTES:
                    raise ValueError("too large")
        except ValueError:
            self._send(413, json.dumps({"ok": False, "message": "页面文件超过 2MB 限制"}, ensure_ascii=False))
            return
        except OSError as exc:
            self._send(500, json.dumps({"ok": False, "message": "读取上传文件失败：%s" % exc}, ensure_ascii=False))
            return

        if not data:
            self._send(400, json.dumps({"ok": False, "message": "页面文件为空"}, ensure_ascii=False))
            return
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            self._send(400, json.dumps({"ok": False, "message": "页面必须是 UTF-8 编码的 HTML"}, ensure_ascii=False))
            return
        lowered = text.lower()
        if "<html" not in lowered and "<body" not in lowered:
            self._send(400, json.dumps({"ok": False, "message": "请上传完整的 HTML 页面文件"}, ensure_ascii=False))
            return

        try:
            save_page_bytes(slug, bytes(data))
        except OSError as exc:
            self._send(500, json.dumps({"ok": False, "message": "保存页面失败：%s" % exc}, ensure_ascii=False))
            return
        self._send(
            200,
            json.dumps({"ok": True, "page": slug, "size": len(data)}, ensure_ascii=False),
        )

    def _app_identity_from_header(self):
        auth = str(self.headers.get("Authorization") or "")
        token = auth[7:].strip() if auth.startswith("Bearer ") else ""
        if not token:
            return None, "缺少小程序登录凭证", 401
        status, data = app_json("/api/web/me", token=token, method="GET")
        if status != 200 or not data.get("ok"):
            return None, data.get("msg") or "小程序身份校验失败", 401
        identity = identity_from_app(data)
        if identity.get("role") != "admin":
            return None, "仅管理员可执行部署", 403
        return identity, "", 200

    def _handle_app_deploy(self):
        global _last_deploy_request_at

        identity, error, code = self._app_identity_from_header()
        if error:
            self._send(code, json.dumps({"ok": False, "message": error}, ensure_ascii=False))
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
                            "client": "miniprogram",
                            "by": identity.get("name") or "admin",
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
                self._send(500, json.dumps({"ok": False, "message": "写入部署请求失败：%s" % exc}, ensure_ascii=False))
                return
            _last_deploy_request_at = now
        self._send(202, json.dumps({"ok": True, "message": "已提交部署请求"}, ensure_ascii=False))

    def _handle_app_deploy_status(self):
        identity, error, code = self._app_identity_from_header()
        if error:
            self._send(code, json.dumps({"ok": False, "message": error}, ensure_ascii=False))
            return
        status = _read_json(STATUS_FILE)
        current = _read_json(CURRENT_FILE)
        lines = _tail_text(LOG_FILE).splitlines()[-LOG_LINES:]
        self._send(
            200,
            json.dumps(
                {
                    "ok": True,
                    "status": status,
                    "current": current,
                    "log": "\n".join(lines),
                },
                ensure_ascii=False,
            ),
        )


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
