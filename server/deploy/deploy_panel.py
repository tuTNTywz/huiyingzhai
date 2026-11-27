#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""汇英斋部署面板 + 个人主页。

运行在 127.0.0.1:3999，由 Nginx 反向代理：
  /          → 个人主页（tuTNT's home）
  /deploy/   → 登录 + 部署控制台

本进程没有 root 权限：点击部署时只写入
/var/lib/huiyingzhai-panel/deploy.request，
由 root 的 systemd path 单元触发 /usr/local/sbin/huiyingzhai-deploy 执行实际部署。

"""
from __future__ import annotations

import warnings

with warnings.catch_warnings():
    warnings.simplefilter("ignore", DeprecationWarning)
    import cgi

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


# ---------- 页面内容（项目介绍 / 使用说明书） ----------


def page_path(slug: str) -> Path:
    return PAGES_DIR / (slug + ".html")


def load_page_bytes(slug: str) -> bytes:
    try:
        return page_path(slug).read_bytes()
    except OSError:
        builtin = INTRO_HTML if slug == "intro" else MANUAL_HTML
        return builtin.encode("utf-8")


ICP_NUMBER = "浙ICP备2026103012号"
ICP_FOOTER = (
    '<footer style="text-align:center;padding:24px 12px 36px;font-size:12px;'
    'color:inherit;opacity:.65">'
    '<a href="https://beian.miit.gov.cn/" target="_blank" rel="noopener" '
    'style="color:inherit;text-decoration:none">%s</a></footer>' % ICP_NUMBER
)


def inject_icp_footer(content: bytes) -> bytes:
    """在页面 </body> 前补备案号；已有则不重复添加。"""
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return content
    if ICP_NUMBER in text:
        return content
    index = text.lower().rfind("</body>")
    if index < 0:
        return content
    return (text[:index] + ICP_FOOTER + "\n" + text[index:]).encode("utf-8")


def save_page_bytes(slug: str, data: bytes) -> None:
    PAGES_DIR.mkdir(parents=True, exist_ok=True)
    tmp = PAGES_DIR / (slug + ".html.tmp")
    with tmp.open("wb") as fh:
        fh.write(data)
    os.chmod(tmp, 0o644)
    os.replace(tmp, page_path(slug))


# ---------- 网页版 ----------

WEB_HTML_PATH = Path(__file__).resolve().with_name("huiyingzhai_web.html")


def load_web_bytes() -> bytes:
    try:
        return WEB_HTML_PATH.read_bytes()
    except OSError:
        return (
            "<!doctype html><meta charset='utf-8'>"
            "<title>汇英斋</title>"
            "<p style='font:16px sans-serif;padding:24px'>"
            "页面文件缺失，请重新运行 server/deploy/install.sh。</p>"
        ).encode("utf-8")


DEPLOY_PAGE_PATH = Path(__file__).resolve().with_name("deploy_page.html")
GUIDE_DIR = Path(__file__).resolve().with_name("wechat-guide")
GUIDE_FILES = {"follow.jpg", "settings-menu.jpg", "mute.jpg"}


def load_deploy_bytes() -> bytes:
    try:
        return DEPLOY_PAGE_PATH.read_bytes()
    except OSError:
        return DEPLOY_HTML.encode("utf-8")


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


# ---------- 后端接口 ----------


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
        return 502, {"ok": False, "msg": "无法连接后端：%s" % exc}
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
        return 502, {"ok": False, "msg": "无法连接后端：%s" % exc}
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
    return None, data.get("msg") or "无法同步班级身份（检查共享密钥）"


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
    identity = record.get("identity") or {}
    student_id = str(identity.get("studentId") or "").strip()
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
    <a class="btn" href="/deploy/">Deploy</a>
    <a class="btn" href="/intro/">汇英斋项目介绍</a>
    <a class="btn" href="/manual/">汇英斋使用说明书</a>
  </div>
  <footer style="text-align:center;margin-top:34px;font-size:12px;color:inherit;opacity:.6">
    <a href="https://beian.miit.gov.cn/" target="_blank" rel="noopener" style="color:inherit;text-decoration:none">浙ICP备2026103012号</a>
  </footer>
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
    <p>部署控制台页面文件缺失，请重新运行 <b>server/deploy/install.sh</b>。</p>
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
  :root { color-scheme: light dark; --brown:#9c6b4a; --ink:#3f3a32; --muted:#8a8478; --paper:#f5f1e8; --card:#fffaf2; --line:#e8ddcc; }
  * { box-sizing: border-box; }
  html { scroll-behavior: smooth; }
  body { margin:0; font:16px/1.75 -apple-system,BlinkMacSystemFont,"Segoe UI","Noto Sans CJK SC","Microsoft YaHei",sans-serif; color:var(--ink); background:var(--paper); }
  body.cover-locked { overflow:hidden; height:100vh; }
  .hero { position:relative; min-height:100vh; display:grid; place-items:center; overflow:hidden; text-align:center; padding:64px 20px 80px; cursor:pointer; background:radial-gradient(circle at 50% 20%, #fffdf8 0%, #f7f1e6 48%, #efe6d7 100%); }
  .hero:focus-visible { outline:3px solid #c1936c; outline-offset:-8px; }
  .orb { position:absolute; border-radius:50%; filter:blur(46px); opacity:.55; pointer-events:none; }
  .orb-a { width:300px; height:300px; background:#e8c9a8; top:-60px; left:-50px; animation:drift 11s ease-in-out infinite; }
  .orb-b { width:260px; height:260px; background:#d9e3d0; bottom:-70px; right:-40px; animation:drift 13s ease-in-out infinite reverse; }
  .hero-inner { position:relative; z-index:1; animation:rise .9s cubic-bezier(.2,.7,.2,1) both; }
  .seal { width:74px; height:74px; border-radius:50%; display:grid; place-items:center; margin:0 auto 24px; background:linear-gradient(145deg,#ad7a55,#8f5f3f); color:#fff; font-size:32px; font-weight:700; box-shadow:0 14px 34px rgba(156,107,74,.3); animation:float 4.5s ease-in-out infinite; }
  .hero h1 { margin:0 0 10px; font-size:clamp(30px,6vw,50px); letter-spacing:-.02em; font-family:"Songti SC","Noto Serif CJK SC",serif; }
  .hero-sub { margin:0 0 26px; color:var(--muted); letter-spacing:.12em; font-size:14px; }
  .hero-back { display:inline-block; padding:10px 22px; border-radius:999px; border:1px solid #d8c9b5; color:#6f6153; text-decoration:none; font-size:14px; transition:.2s; }
  .hero-back:hover { background:#fff; border-color:var(--brown); color:var(--brown); transform:translateY(-2px); }
  .scroll-hint { margin-top:26px; color:#a99c8b; font-size:12px; letter-spacing:.2em; animation:breathe 2.4s ease-in-out infinite; }
  .toc { position:sticky; top:0; z-index:10; display:flex; gap:8px; overflow-x:auto; padding:12px 18px; background:rgba(245,241,232,.9); backdrop-filter:blur(10px); border-bottom:1px solid var(--line); scrollbar-width:none; }
  .toc::-webkit-scrollbar { display:none; }
  .toc a { flex:none; padding:6px 14px; border-radius:999px; background:#fffaf2; border:1px solid var(--line); color:#6f6153; text-decoration:none; font-size:13px; white-space:nowrap; transition:.2s; }
  .toc a:hover { background:var(--brown); border-color:var(--brown); color:#fff; }
  main { max-width:980px; margin:0 auto; padding:38px 18px 20px; }
  .section { background:var(--card); border:1px solid var(--line); border-radius:20px; padding:26px clamp(18px,4vw,38px); margin:0 0 22px; box-shadow:0 12px 36px rgba(120,95,65,.06); }
  .reveal { opacity:0; transform:translateY(26px); transition:opacity .7s cubic-bezier(.2,.7,.2,1), transform .7s cubic-bezier(.2,.7,.2,1); }
  .reveal.in { opacity:1; transform:none; }
  .sec-head { display:flex; align-items:center; gap:14px; margin-bottom:16px; }
  .sec-num { flex:none; width:42px; height:42px; border-radius:14px; display:grid; place-items:center; background:#f2e6d7; color:var(--brown); font-weight:700; font-size:14px; letter-spacing:.04em; }
  .sec-head h2 { margin:0; font-size:21px; letter-spacing:-.01em; font-family:"Songti SC","Noto Serif CJK SC",serif; }
  .section p { margin:0 0 12px; }
  .section p:last-child { margin-bottom:0; }
  .steps { display:grid; gap:14px; margin:6px 0 4px; }
  .step { display:grid; grid-template-columns:32px 1fr; gap:12px; align-items:start; }
  .step-num { width:28px; height:28px; border-radius:50%; display:grid; place-items:center; background:#fff; border:1px solid #dfcfba; color:var(--brown); font-size:13px; font-weight:700; }
  .step b { display:block; margin-bottom:2px; font-size:15px; }
  .step p { margin:0; color:#6f675d; font-size:14px; }
  .tip { margin-top:16px; padding:12px 15px; background:#f7efe2; border-left:3px solid #c1936c; border-radius:0 10px 10px 0; color:#6f5a45; font-size:14px; }
  .roles { display:grid; grid-template-columns:repeat(3,1fr); gap:12px; margin-top:6px; }
  .role { padding:16px; border-radius:14px; background:#fff; border:1px solid var(--line); transition:transform .2s, box-shadow .2s; }
  .role:hover { transform:translateY(-3px); box-shadow:0 12px 26px rgba(120,95,65,.1); }
  .role h3 { margin:0 0 6px; font-size:15px; color:var(--brown); }
  .role p { margin:0; font-size:13px; color:#6f675d; }
  .tags { display:flex; flex-wrap:wrap; gap:8px; margin-top:10px; }
  .tag { font-size:12px; padding:3px 10px; border-radius:999px; background:#f2e6d7; color:#7d5a40; }
  table { width:100%; border-collapse:collapse; margin:8px 0 4px; font-size:14px; }
  th, td { text-align:left; padding:9px 8px; border-bottom:1px dashed #e2d5c2; vertical-align:top; }
  th { color:var(--brown); font-weight:600; }
  .faq-item { border-bottom:1px dashed #e2d5c2; }
  .faq-item:last-child { border-bottom:0; }
  .faq-q { width:100%; display:flex; justify-content:space-between; gap:14px; align-items:center; padding:14px 0; border:0; background:transparent; color:inherit; font:inherit; text-align:left; cursor:pointer; }
  .faq-q span { color:var(--brown); font-size:18px; transition:transform .3s; }
  .faq-a { max-height:0; overflow:hidden; transition:max-height .38s ease, padding .38s ease; color:#6f675d; font-size:14px; }
  .faq-item.open .faq-a { max-height:600px; padding-bottom:12px; }
  .faq-item.open .faq-q span { transform:rotate(45deg); }
  .site-footer { text-align:center; padding:10px 18px 48px; color:#9a8f80; font-size:12px; letter-spacing:.06em; }
  .site-footer a { color:var(--brown); text-decoration:none; }
  .wechat-guide { margin:14px 0 2px; padding:14px; border:1px solid var(--line); border-radius:16px; background:#fff; }
  .guide-stage { display:flex; gap:16px; align-items:center; }
  .guide-phone { position:relative; flex:none; width:250px; height:280px; border-radius:16px; border:1px solid var(--line); background:#f7f2e9; overflow:hidden; box-shadow:0 12px 26px rgba(60,45,30,.12); }
  .guide-slide { position:absolute; inset:12px; display:flex; align-items:center; justify-content:center; opacity:0; transform:translateX(10px); transition:opacity .45s ease, transform .45s ease; }
  .guide-slide.active { opacity:1; transform:none; }
  .guide-slide img { max-width:100%; max-height:100%; width:auto; height:auto; display:block; border-radius:9px; box-shadow:0 6px 18px rgba(60,45,30,.16); }
  .guide-qr { width:150px; height:150px; margin:0 auto; border-radius:12px; background:
    repeating-linear-gradient(90deg,#2e2a25 0 6px,transparent 6px 12px),
    repeating-linear-gradient(0deg,#2e2a25 0 6px,transparent 6px 12px);
    background-size:24px 24px; position:relative; box-shadow:0 0 0 8px #fff,0 0 0 9px #e2d5c2; }
  .guide-qr::after { content:""; position:absolute; left:8px; right:8px; top:12px; height:3px; border-radius:2px; background:#c1936c; box-shadow:0 0 10px #c1936c; animation:scan 2.2s ease-in-out infinite; }
  .guide-qr-note { text-align:center; color:#8a8478; font-size:11px; margin-top:14px; }
  .guide-main { flex:1; min-width:0; }
  .guide-step { font-size:12px; letter-spacing:.12em; color:var(--brown); margin-bottom:6px; }
  .guide-title { font-size:16px; font-weight:700; margin-bottom:4px; }
  .guide-desc { font-size:13px; color:#6f675d; min-height:40px; }
  .guide-dots { display:flex; gap:7px; margin-top:12px; }
  .guide-dots button { width:9px; height:9px; padding:0; border:0; border-radius:50%; background:#e2d5c2; cursor:pointer; }
  .guide-dots button.active { background:var(--brown); transform:scale(1.15); }
  .guide-hint { margin-top:10px; font-size:11px; color:#a99c8b; }
  @keyframes scan { 0%,100% { top:12px; } 50% { top:132px; } }
  @keyframes rise { from { opacity:0; transform:translateY(20px); } to { opacity:1; transform:none; } }
  @keyframes float { 0%,100% { transform:translateY(0); } 50% { transform:translateY(-8px); } }
  @keyframes drift { 0%,100% { transform:translate(0,0) scale(1); } 50% { transform:translate(26px,18px) scale(1.08); } }
  @keyframes breathe { 0%,100% { opacity:.45; } 50% { opacity:1; } }
  @media (max-width:700px) {
    .roles { grid-template-columns:1fr; }
    .hero { min-height:100vh; padding:54px 18px 70px; }
    .section { padding:22px 18px; border-radius:16px; }
    .guide-stage { flex-direction:column; align-items:stretch; }
    .guide-phone { width:230px; height:260px; margin:0 auto; }
    .guide-desc { min-height:0; }
  }
  @media (prefers-reduced-motion:reduce) { html { scroll-behavior:auto; } *,*::before,*::after { animation:none !important; transition:none !important; } .reveal { opacity:1; transform:none; } }
  @media (prefers-color-scheme:dark) {
    :root { --ink:#ece4d8; --muted:#a99c8b; --paper:#14100d; --card:#1d1712; --line:#33281f; }
    .hero { background:radial-gradient(circle at 50% 20%, #241b14 0%, #191410 48%, #100d0a 100%); }
    .orb-a { background:#5a3f2b; } .orb-b { background:#33402f; }
    .toc { background:rgba(20,16,13,.9); }
    .toc a { background:#1d1712; color:#c9b9a6; }
    .toc a:hover { background:var(--brown); color:#fff; }
    .role { background:#241c16; }
    .sec-num, .tag { background:#33261c; color:#dcb894; }
    .step-num { background:#241c16; border-color:#4a3829; }
    .step p, .role p, .faq-a, td, .guide-desc { color:#b8aa99; }
    .wechat-guide { background:#241c16; }
    .tip { background:#2a2119; color:#d9c2a8; border-left-color:#a97a54; }
    .faq-item, th, td { border-bottom-color:#3a2d22; }
    .hero-back { color:#c9b9a6; border-color:#4a3829; }
    .hero-back:hover { background:#241c16; color:#e0b993; border-color:var(--brown); }
  }
</style>
<noscript><style>.reveal { opacity:1; transform:none; } .faq-a { max-height:none; padding-bottom:12px; }</style></noscript>
</head>
<body>
<header class="hero" id="cover" role="button" tabindex="0" aria-label="点击进入说明书">
  <div class="orb orb-a"></div>
  <div class="orb orb-b"></div>
  <div class="hero-inner">
    <div class="seal">斋</div>
    <h1>汇英斋 · 使用说明书</h1>
    <p class="hero-sub">作业 · 活动 · 志愿 · 交流，一册在手</p>
    <a class="hero-back" href="/">← 返回主页</a>
    <div class="scroll-hint">点击进入 · 向下滚动阅读 ↓</div>
  </div>
</header>

<nav class="toc" aria-label="目录">
  <a href="#start">快速开始</a>
  <a href="#login">登录与身份</a>
  <a href="#homework">作业</a>
  <a href="#activity">活动</a>
  <a href="#volunteer">志愿活动</a>
  <a href="#forum">交流</a>
  <a href="#notice">通知</a>
  <a href="#mine">我的</a>
  <a href="#publish">发布内容</a>
  <a href="#attach">附件规则</a>
  <a href="#faq">常见问题</a>
</nav>

<main>
  <section id="start" class="section reveal">
    <div class="sec-head"><span class="sec-num">01</span><h2>快速开始</h2></div>
    <div class="steps">
      <div class="step"><span class="step-num">1</span><div><b>打开网页版</b><p>浏览器访问 <b>https://tutnt.com/huiyingzhai/</b>（或班级公布的域名）。</p></div></div>
      <div class="step"><span class="step-num">2</span><div><b>姓名 + 学号登录</b><p>填写班级名单中的真实姓名和学号。名单内的学号首次登录会自动创建网页身份，无需单独注册。</p></div></div>
      <div class="step"><span class="step-num">3</span><div>
        <b>打开微信通知</b>
        <p>关注班级使用的服务号并关闭「消息免打扰」，发布作业/活动时能及时收到提醒；不需要可以跳过。</p>
        <div class="wechat-guide" id="wechat-guide">
          <div class="guide-stage">
            <div class="guide-phone">
              <div class="guide-slide active" data-index="0">
                <div>
                  <div class="guide-qr"></div>
                  <div class="guide-qr-note">扫描班级群二维码</div>
                </div>
              </div>
              <div class="guide-slide" data-index="1"><img src="/manual/guide/follow.jpg" alt="关注服务号" loading="lazy"></div>
              <div class="guide-slide" data-index="2"><img src="/manual/guide/settings-menu.jpg" alt="打开设置" loading="lazy"></div>
              <div class="guide-slide" data-index="3"><img src="/manual/guide/mute.jpg" alt="关闭消息免打扰" loading="lazy"></div>
            </div>
            <div class="guide-main">
              <div class="guide-step" id="guide-step">第 1 步 / 共 4 步</div>
              <div class="guide-title" id="guide-title">扫描二维码</div>
              <div class="guide-desc" id="guide-desc">用微信扫描班级群里的服务号二维码。</div>
              <div class="guide-dots" id="guide-dots" aria-label="微信通知步骤">
                <button class="active" data-slide="0" aria-label="第 1 步"></button>
                <button data-slide="1" aria-label="第 2 步"></button>
                <button data-slide="2" aria-label="第 3 步"></button>
                <button data-slide="3" aria-label="第 4 步"></button>
              </div>
              <div class="guide-hint">动画自动播放，也可点击圆点切换</div>
            </div>
          </div>
        </div>
      </div></div>
      <div class="step"><span class="step-num">4</span><div><b>开始使用</b><p>顶部切换作业、活动、志愿活动、交流、通知和我的；手机和电脑浏览器都可以。</p></div></div>
    </div>
    <div class="tip">学号必须与班级名单一致；姓名不匹配会被拒绝登录。不要使用他人学号。</div>
  </section>

  <section id="login" class="section reveal">
    <div class="sec-head"><span class="sec-num">02</span><h2>登录与身份</h2></div>
    <p>登录只需要姓名和学号；班级、职位和负责科目由班级名单自动带出。身份决定你能查看和发布什么：</p>
    <div class="roles">
      <div class="role"><h3>普通同学</h3><p>查看作业、活动、志愿活动，参与交流，接收通知。</p></div>
      <div class="role"><h3>班委</h3><p>发布活动、志愿活动；学习委员还可发布作业；可删除对应内容。</p></div>
      <div class="role"><h3>课代表 / 管理员</h3><p>课代表发布本人负责科目的任务；管理员拥有全部权限。</p></div>
    </div>
    <div class="tags"><span class="tag">姓名</span><span class="tag">学号</span><span class="tag">班级自动</span><span class="tag">职位自动</span><span class="tag">名单校验</span></div>
    <div class="tip">登录状态保存在浏览器 Cookie 中，有效期约 30 天；退出在「我的」页面。</div>
  </section>

  <section id="homework" class="section reveal">
    <div class="sec-head"><span class="sec-num">03</span><h2>作业</h2></div>
    <p>「作业」页分为学科板块和班级作业。学科板块按科目进入，班级作业汇总所有班级作业。</p>
    <div class="steps">
      <div class="step"><span class="step-num">1</span><div><b>看状态</b><p>「进行中」表示未到截止时间，「已截止」表示已过期；卡片显示布置人、截止时间和附件数量。</p></div></div>
      <div class="step"><span class="step-num">2</span><div><b>看详情</b><p>点击作业查看完整说明和附件；图片会直接显示缩略图，点击可查看大图；文档可下载。</p></div></div>
      <div class="step"><span class="step-num">3</span><div><b>标记完成</b><p>每份作业旁有「未完成 / 已完成」按钮，点击即可切换；完成状态只记录在自己的账号下。</p></div></div>
      <div class="step"><span class="step-num">4</span><div><b>删除</b><p>学习委员和管理员可在列表或详情看到「删除作业」按钮，删除会同时清理附件和通知。</p></div></div>
    </div>
  </section>

  <section id="activity" class="section reveal">
    <div class="sec-head"><span class="sec-num">04</span><h2>活动</h2></div>
    <p>「活动」汇总班级活动，卡片显示名称、时间、地点和发布人。</p>
    <div class="steps">
      <div class="step"><span class="step-num">1</span><div><b>查看详情</b><p>点击卡片查看完整活动说明、时间、地点和发布人。</p></div></div>
      <div class="step"><span class="step-num">2</span><div><b>发布与删除</b><p>班委或管理员在 <b>/upload/</b> 发布活动；可在列表或详情删除。</p></div></div>
    </div>
  </section>

  <section id="volunteer" class="section reveal">
    <div class="sec-head"><span class="sec-num">05</span><h2>志愿活动</h2></div>
    <p>「志愿活动」用于发布志愿服务招募，显示开始与结束时间和详细内容；地点、名额、联系方式、报名链接等可写进详细内容。</p>
    <div class="steps">
      <div class="step"><span class="step-num">1</span><div><b>查看与报名</b><p>详情页可查看完整活动说明；正文中的网址会自动变成可点击链接。</p></div></div>
      <div class="step"><span class="step-num">2</span><div><b>发布</b><p>班委或管理员在 <b>/upload/</b> 的「发布志愿活动」表单填写活动名称、开始和结束时间、详细内容并发布。</p></div></div>
      <div class="step"><span class="step-num">3</span><div><b>删除</b><p>班委或管理员可在列表或详情删除，通知会同步清理。</p></div></div>
    </div>
  </section>

  <section id="forum" class="section reveal">
    <div class="sec-head"><span class="sec-num">06</span><h2>交流</h2></div>
    <p>所有登录同学都可以发帖、回复和上传附件（图片、文档等）。</p>
    <div class="steps">
      <div class="step"><span class="step-num">1</span><div><b>发帖</b><p>填写内容或选择附件后点击「发帖」；帖子按时间倒序显示。</p></div></div>
      <div class="step"><span class="step-num">2</span><div><b>回复</b><p>点击帖子进入详情，在底部回复；附件会显示在正文下方。</p></div></div>
    </div>
    <div class="tip">附件链接是公开静态地址，拿到链接即可访问；请勿上传身份证、密码等敏感材料。</div>
  </section>

  <section id="notice" class="section reveal">
    <div class="sec-head"><span class="sec-num">07</span><h2>通知</h2></div>
    <p>「通知」只显示班委/管理员发布的班级通知，按发布时间从新到旧排列；超过 30 天自动删除。</p>
    <div class="steps">
      <div class="step"><span class="step-num">1</span><div><b>看全文</b><p>通知显示完整正文和附件；正文里的网址和附件都可直接打开。</p></div></div>
      <div class="step"><span class="step-num">2</span><div><b>发布通知</b><p>班委或管理员点击「发布通知」，填写标题、正文和附件。</p></div></div>
      <div class="step"><span class="step-num">3</span><div><b>自动清理</b><p>通知保留 30 天，过期后自动删除，附件一并清理。</p></div></div>
    </div>
    <div class="tip">班级还配置了微信/QQ 机器人通知；具体是否收到取决于班级管理员是否启用。</div>
  </section>

  <section id="mine" class="section reveal">
    <div class="sec-head"><span class="sec-num">08</span><h2>我的</h2></div>
    <p>「我的」显示姓名、学号、班级、职位、身份和负责科目。</p>
    <div class="steps">
      <div class="step"><span class="step-num">1</span><div><b>上传入口</b><p>班委和课代表会看到「发布内容」按钮，进入 <b>/upload/</b>。</p></div></div>
      <div class="step"><span class="step-num">2</span><div><b>使用说明书</b><p>可随时返回本页面查看操作说明。</p></div></div>
      <div class="step"><span class="step-num">3</span><div><b>退出</b><p>点击「退出」清除当前浏览器登录状态。</p></div></div>
    </div>
  </section>

  <section id="publish" class="section reveal">
    <div class="sec-head"><span class="sec-num">09</span><h2>发布内容（班委 / 课代表）</h2></div>
    <p>打开 <b>/upload/</b>，用姓名 + 学号登录后，按身份显示可发布的表单。</p>
    <table>
      <thead><tr><th>身份 / 职务</th><th>可发布</th></tr></thead>
      <tbody>
        <tr><td>管理员</td><td>作业、活动、志愿活动、通知、全部学科任务</td></tr>
        <tr><td>学习委员</td><td>作业、活动、志愿活动、通知、本人负责科目的任务</td></tr>
        <tr><td>其他班委</td><td>活动、志愿活动、通知、本人负责科目的任务</td></tr>
        <tr><td>课代表</td><td>本人负责科目的任务</td></tr>
        <tr><td>普通同学</td><td>不可发布，只能浏览和交流</td></tr>
      </tbody>
    </table>
    <div class="tip">发布后网页版立即显示，并按配置推送微信/QQ 机器人通知。</div>
  </section>

  <section id="attach" class="section reveal">
    <div class="sec-head"><span class="sec-num">10</span><h2>附件规则</h2></div>
    <p>作业、学科任务和论坛帖最多 <b>5 个附件</b>，单个不超过 <b>20MB</b>。支持常见文档和图片。</p>
    <p>附件上传后保存在服务器 <b>/uploads/</b>，通知和详情中可直接打开或下载；图片会直接显示为缩略图，点击查看原图。</p>
  </section>

  <section id="faq" class="section reveal">
    <div class="sec-head"><span class="sec-num">11</span><h2>常见问题</h2></div>
    <div class="faq-item">
      <button class="faq-q">登录提示“姓名与学号不匹配”？<span>＋</span></button>
      <div class="faq-a"><p>姓名必须与班级名单完全一致；确认学号没有输错。名单外的学号不能登录。</p></div>
    </div>
    <div class="faq-item">
      <button class="faq-q">为什么看不到发布按钮？<span>＋</span></button>
      <div class="faq-a"><p>发布只对班委、课代表和管理员开放；普通同学只能浏览、交流。</p></div>
    </div>
    <div class="faq-item">
      <button class="faq-q">通知里的链接打不开？<span>＋</span></button>
      <div class="faq-a"><p>先确认链接以 http/https 开头；内网或失效链接无法打开。附件链接需要登录后才能稳定访问。</p></div>
    </div>
    <div class="faq-item">
      <button class="faq-q">忘记登录状态 / 需要换账号？<span>＋</span></button>
      <div class="faq-a"><p>到「我的」点「退出」，再用另一个姓名 + 学号登录。</p></div>
    </div>
    <div class="faq-item">
      <button class="faq-q">网页打不开或样式错乱？<span>＋</span></button>
      <div class="faq-a"><p>尝试刷新或换浏览器；仍有问题请联系管理员检查服务器和 Nginx 状态。</p></div>
    </div>
  </section>
</main>

<footer class="site-footer">
  汇英斋 · <a href="https://beian.miit.gov.cn/" target="_blank" rel="noopener">浙ICP备2026103012号</a>
</footer>

<script>
(() => {
  // 封面：首屏锁定滚动，点击/回车进入后恢复滚轮阅读
  const cover = document.getElementById('cover');
  const start = document.getElementById('start');
  if (cover && start) {
    if (!location.hash) document.body.classList.add('cover-locked');
    const enter = () => {
      document.body.classList.remove('cover-locked');
      start.scrollIntoView({ behavior: 'smooth', block: 'start' });
    };
    cover.addEventListener('click', (event) => {
      if (event.target.closest('a')) return;
      enter();
    });
    cover.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        enter();
      }
    });
  }

  const items = document.querySelectorAll('.reveal');
  if ('IntersectionObserver' in window) {
    const io = new IntersectionObserver((entries) => {
      entries.forEach((entry) => {
        if (entry.isIntersecting) { entry.target.classList.add('in'); io.unobserve(entry.target); }
      });
    }, { threshold: 0.12 });
    items.forEach((item) => io.observe(item));
  } else {
    items.forEach((item) => item.classList.add('in'));
  }
  document.querySelectorAll('.faq-q').forEach((button) => {
    button.addEventListener('click', () => {
      button.parentElement.classList.toggle('open');
    });
  });

  // 微信通知步骤动画：二维码 → 关注服务号 → 设置 → 关闭免打扰
  const guide = document.getElementById('wechat-guide');
  if (guide) {
    const slides = Array.from(guide.querySelectorAll('.guide-slide'));
    const dots = Array.from(guide.querySelectorAll('.guide-dots button'));
    const stepLabel = document.getElementById('guide-step');
    const title = document.getElementById('guide-title');
    const desc = document.getElementById('guide-desc');
    const captions = [
      ['扫描二维码', '用微信扫描班级群里的服务号二维码。'],
      ['关注服务号', '进入服务号主页，点击「关注服务号」。'],
      ['打开设置', '点右上角「…」，在菜单中选择「设置」。'],
      ['关闭消息免打扰', '关闭「消息免打扰」，保留「接收文章推送」。'],
    ];
    let index = 0;
    let timer = null;
    const show = (next) => {
      index = (next + slides.length) % slides.length;
      slides.forEach((slide, i) => slide.classList.toggle('active', i === index));
      dots.forEach((dot, i) => dot.classList.toggle('active', i === index));
      stepLabel.textContent = '第 ' + (index + 1) + ' 步 / 共 ' + slides.length + ' 步';
      title.textContent = captions[index][0];
      desc.textContent = captions[index][1];
    };
    const stop = () => { if (timer) { clearInterval(timer); timer = null; } };
    const play = () => {
      stop();
      if (window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
      timer = setInterval(() => show(index + 1), 3200);
    };
    dots.forEach((dot) => dot.addEventListener('click', () => {
      show(Number(dot.dataset.slide) || 0);
      play();
    }));
    guide.addEventListener('mouseenter', stop);
    guide.addEventListener('mouseleave', play);
    show(0);
    play();
  }
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
  .row > .field { flex: 1 1 220px; min-width: 0; }
  .row > .field input { max-width: 100%; }
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
  .form-card { display: grid; gap: 0; scroll-margin-top: 18px; }
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
  <div class="sub">在这里发布作业、活动、志愿活动和学科任务；身份与权限按班级名单确定。</div>

  <section id="login" class="card hidden">
    <h2>用班级身份登录</h2>
    <div class="sub" style="margin-bottom: 10px">填写班级名单中的姓名和学号。</div>
    <label for="name">姓名</label>
    <input id="name" autocomplete="name">
    <label for="studentId">学号</label>
    <input id="studentId" inputmode="numeric" placeholder="班级名单中的学号">
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

      <div id="volunteer-card" class="card form-card hidden">
        <h2>发布志愿活动</h2>
        <label for="vol-title">活动名称</label>
        <input id="vol-title" placeholder="如：图书馆整理志愿服务">
        <div class="row">
          <div class="field">
            <label for="vol-start">开始时间</label>
            <input id="vol-start" type="datetime-local">
          </div>
          <div class="field">
            <label for="vol-end">结束时间</label>
            <input id="vol-end" type="datetime-local">
          </div>
        </div>
        <label for="vol-content">详细内容</label>
        <textarea id="vol-content" placeholder="填写服务内容、集合地点、组织单位、招募人数、联系方式、报名链接等…"></textarea>
        <div class="form-actions"><button id="vol-submit" class="primary">发布志愿活动</button></div>
        <div id="vol-msg" class="msg"></div>
      </div>

      <div id="notice-card" class="card form-card hidden">
        <h2>发布通知</h2>
        <label for="notice-title">通知标题</label>
        <input id="notice-title" placeholder="如：本周班会安排">
        <label for="notice-content">通知内容</label>
        <textarea id="notice-content" placeholder="填写通知正文…"></textarea>
        <label for="notice-files">附件（最多 5 个，单个 ≤20MB）</label>
        <input id="notice-files" type="file" multiple>
        <div class="form-actions"><button id="notice-submit" class="primary">发布通知</button></div>
        <div id="notice-msg" class="msg"></div>
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

  function scrollToTarget() {
    const target = (location.hash || '').replace('#', '');
    if (!target) return;
    const el = document.getElementById(target);
    if (!el || el.classList.contains('hidden')) {
      if (target.endsWith('-card')) setMsg('app-msg', '当前身份没有该发布权限', true);
      return;
    }
    el.scrollIntoView({ behavior: 'smooth', block: 'start' });
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
    $('volunteer-card').classList.toggle('hidden', !committee);
    $('notice-card').classList.toggle('hidden', !committee);
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
    // 从作业/活动/志愿活动/学科页面跳转过来时，滚动到对应表单
    setTimeout(scrollToTarget, 80);
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

  async function submitVolunteer() {
    const title = $('vol-title').value.trim();
    const activityTime = toMs($('vol-start').value);
    const endTime = toMs($('vol-end').value);
    if (!title || !activityTime) { setMsg('vol-msg', '请填写活动名称和开始时间', true); return; }
    if (endTime && endTime < activityTime) { setMsg('vol-msg', '结束时间不能早于开始时间', true); return; }
    const button = $('vol-submit');
    button.disabled = true;
    try {
      setMsg('vol-msg', '正在发布…');
      await publishAction('publishVolunteer', {
        title: title,
        activityTime: activityTime,
        endTime: endTime,
        content: $('vol-content').value.trim()
      }, 'vol-msg');
      setMsg('vol-msg', '志愿活动已发布');
      ['vol-title', 'vol-start', 'vol-end', 'vol-content'].forEach((id) => { $(id).value = ''; });
    } catch (err) {
      setMsg('vol-msg', err.message || '发布失败', true);
    } finally {
      button.disabled = false;
    }
  }

  async function submitNotice() {
    const title = $('notice-title').value.trim();
    if (!title) { setMsg('notice-msg', '请填写通知标题', true); return; }
    const button = $('notice-submit');
    button.disabled = true;
    try {
      setMsg('notice-msg', '正在上传附件…');
      const attachments = await uploadFiles($('notice-files'), 'notice');
      setMsg('notice-msg', '正在发布…');
      await publishAction('publishNotice', {
        title: title,
        content: $('notice-content').value.trim(),
        attachments: attachments
      }, 'notice-msg');
      setMsg('notice-msg', '通知已发布');
      $('notice-title').value = '';
      $('notice-content').value = '';
      $('notice-files').value = '';
    } catch (err) {
      setMsg('notice-msg', err.message || '发布失败', true);
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

  $('login-btn').addEventListener('click', login);
  $('studentId').addEventListener('keydown', (e) => { if (e.key === 'Enter') login(); });
  $('name').addEventListener('keydown', (e) => { if (e.key === 'Enter') $('studentId').focus(); });
  $('logout').addEventListener('click', logout);
  $('hw-submit').addEventListener('click', submitHomework);
  $('act-submit').addEventListener('click', submitActivity);
  $('vol-submit').addEventListener('click', submitVolunteer);
  $('notice-submit').addEventListener('click', submitNotice);
  $('sub-submit').addEventListener('click', submitSubject);

  checkSession();
})();
</script>
<footer style="text-align:center;padding:20px 12px 32px;font-size:12px;color:inherit;opacity:.65">
  <a href="https://beian.miit.gov.cn/" target="_blank" rel="noopener" style="color:inherit;text-decoration:none">浙ICP备2026103012号</a>
</footer>
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
    <p>网页数据上传：打开 <b>/upload/</b>，用班级名单中的 <b>姓名 + 学号</b> 登录后发布。</p>
    <p>部署控制：管理员打开 <b>/deploy/</b>，用姓名 + 学号登录后操作。</p>
  </div>
  <div class="links">
    <a href="/intro/">项目介绍</a>
    <a href="/manual/">使用说明书</a>
  </div>
  <footer style="text-align:center;padding:20px 12px 8px;font-size:12px;color:inherit;opacity:.65">
    <a href="https://beian.miit.gov.cn/" target="_blank" rel="noopener" style="color:inherit;text-decoration:none">浙ICP备2026103012号</a>
  </footer>
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

    def _require_admin_session(self):
        token, record = self._require_session()
        if not record:
            return None, None
        identity = record.get("identity") or {}
        if identity.get("role") != "admin":
            self._send(
                403,
                json.dumps({"ok": False, "message": "仅管理员可访问部署控制台"}, ensure_ascii=False),
            )
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
            content = inject_icp_footer(load_page_bytes("intro"))
            if self._page_editor():
                content = inject_page_toolbar(content, "intro")
            self._send(200, content, "text/html; charset=utf-8")
            return
        if area == "" and path.startswith("/manual/guide/"):
            name = path.rsplit("/", 1)[-1]
            if name not in GUIDE_FILES:
                self._send(404, json.dumps({"ok": False, "message": "图片不存在"}, ensure_ascii=False))
                return
            try:
                data = (GUIDE_DIR / name).read_bytes()
            except OSError:
                self._send(404, json.dumps({"ok": False, "message": "图片不存在"}, ensure_ascii=False))
                return
            self._send(200, data, "image/jpeg")
            return
        if area == "" and path in ("/manual", "/manual/", "/manual/index.html"):
            content = inject_icp_footer(load_page_bytes("manual"))
            if self._page_editor():
                content = inject_page_toolbar(content, "manual")
            self._send(200, content, "text/html; charset=utf-8")
            return
        if area == "/deploy" and path in ("/", "/index.html"):
            self._send(200, load_deploy_bytes(), "text/html; charset=utf-8")
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
            identity = record.get("identity") or {}
            student_id = str(identity.get("studentId") or "").strip()
            self._send(
                200,
                json.dumps(
                    {
                        "ok": True,
                        "user": identity.get("name") or record["user"],
                        "csrf": record["csrf"],
                        "perms": ["upload", "download"],
                        "identity": identity,
                        "studentId": student_id,
                        "appReady": bool(APP_SECRET),
                    },
                    ensure_ascii=False,
                ),
            )
            return
        if path == "/api/status":
            if not self._require_admin_session()[1]:
                return
            self._send(200, self._status_payload())
            return
        if path == "/api/log":
            if not self._require_admin_session()[1]:
                return
            lines = _tail_text(LOG_FILE).splitlines()[-LOG_LINES:]
            self._send(200, "\n".join(lines), "text/plain; charset=utf-8")
            return
        if path == "/api/pages/download":
            self._handle_page_download()
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
        if path == "/api/app/upload":
            self._handle_app_upload()
            return
        if path == "/api/app/action":
            self._handle_app_action()
            return
        if path == "/api/deploy":
            self._handle_web_deploy()
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
            self._send(401, json.dumps({"ok": False, "message": error or "学号不在班级名单"}, ensure_ascii=False))
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

    def _app_token_for(self, record: dict) -> str:
        token = str(record.get("app_token") or "")
        if token:
            return token
        return refresh_app_token(record)

    def _handle_app_upload(self):
        _token, record = self._require_session()
        if not record:
            return
        if not self._require_csrf(record):
            return
        app_token = self._app_token_for(record)
        if not app_token:
            self._send(400, json.dumps({"ok": False, "message": "请先登录"}, ensure_ascii=False))
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
            "listActivities", "getActivity", "listVolunteers", "publishVolunteer",
            "listPosts", "getPost", "listReplies",
            "fetchHomeworkNotices",
            "listSubjectTasks", "getSubjectSettings", "getSubjectRep",
            "createPost", "createReply",
            "publishHomework", "publishActivity", "publishSubjectTask",
            "publishNotice", "deleteNotice",
            "deleteHomework", "deleteActivity", "deleteSubjectTask",
            "setHomeworkStatus",
        }
        if action not in allowed:
            self._send(400, json.dumps({"ok": False, "message": "不支持的操作"}, ensure_ascii=False))
            return
        app_token = self._app_token_for(record)
        if not app_token:
            self._send(400, json.dumps({"ok": False, "message": "请先登录"}, ensure_ascii=False))
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
        if action == "publishVolunteer" and role not in ("committee", "admin"):
            self._send(403, json.dumps({"ok": False, "message": "仅班委或管理员可发布志愿活动"}, ensure_ascii=False))
            return
        if action == "publishNotice" and role not in ("committee", "admin"):
            self._send(403, json.dumps({"ok": False, "message": "仅班委或管理员可发布通知"}, ensure_ascii=False))
            return
        if action == "deleteNotice" and role not in ("committee", "admin"):
            self._send(403, json.dumps({"ok": False, "message": "仅班委或管理员可删除通知"}, ensure_ascii=False))
            return
        if action == "publishSubjectTask":
            subject = str(payload["data"].get("subject") or "")
            if role != "admin" and subject not in reps:
                self._send(403, json.dumps({"ok": False, "message": "只能发布你负责的科目"}, ensure_ascii=False))
                return
        if action == "deleteHomework" and not (role == "admin" or position == "学习委员"):
            self._send(403, json.dumps({"ok": False, "message": "仅学习委员或管理员可删除作业"}, ensure_ascii=False))
            return
        if action == "deleteActivity" and role not in ("committee", "admin"):
            self._send(403, json.dumps({"ok": False, "message": "仅班委或管理员可删除活动"}, ensure_ascii=False))
            return
        if action == "deleteSubjectTask" and role != "admin" and not reps:
            self._send(403, json.dumps({"ok": False, "message": "仅课代表或管理员可删除学科任务"}, ensure_ascii=False))
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

    def _handle_web_deploy(self):
        """网页部署控制台：管理员会话 + CSRF 后写入部署请求。"""
        global _last_deploy_request_at

        _token, record = self._require_admin_session()
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
                identity = record.get("identity") or {}
                tmp.write_text(
                    json.dumps(
                        {
                            "requestedAt": _now_ms(),
                            "client": self._client_ip(),
                            "by": identity.get("name") or record.get("user") or "admin",
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
