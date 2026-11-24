#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""汇英斋部署面板。

运行在 127.0.0.1:3999，由 Nginx 反向代理到 /deploy/。
本进程没有 root 权限：点击“部署”时只写入
/var/lib/huiyingzhai-panel/deploy.request，
由 root 的 systemd path 单元触发 /usr/local/sbin/huiyingzhai-deploy 执行实际部署。

只用 Python 标准库，Ubuntu 22.04 自带的 python3 即可运行。
"""
from __future__ import annotations

import hmac
import json
import os
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

PORT = int(os.environ.get("PANEL_PORT") or "3999")
TOKEN = (os.environ.get("PANEL_TOKEN") or "").strip()
QUEUE_DIR = Path(os.environ.get("PANEL_QUEUE_DIR") or "/var/lib/huiyingzhai-panel")
STATUS_FILE = Path(
    os.environ.get("PANEL_STATUS_FILE") or "/var/lib/huiyingzhai-deploy/status.json"
)
CURRENT_FILE = Path(
    os.environ.get("PANEL_CURRENT_FILE") or "/var/lib/huiyingzhai-deploy/current.json"
)
LOG_FILE = Path(os.environ.get("PANEL_LOG_FILE") or "/var/log/huiyingzhai-deploy.log")
REQUEST_FILE = QUEUE_DIR / "deploy.request"

MAX_LOG_BYTES = 64 * 1024
LOG_LINES = 300
RUNNING_STALE_MS = 30 * 60 * 1000
MIN_INTERVAL_SECONDS = 5

CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
    "connect-src 'self'; img-src 'self' data:; base-uri 'none'; "
    "form-action 'none'; frame-ancestors 'none'"
)

_lock = threading.Lock()
_last_request_at = 0.0


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


HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>汇英斋 · 服务端部署</title>
<style>
  :root { color-scheme: light dark; }
  * { box-sizing: border-box; }
  body { margin: 0; font: 15px/1.6 -apple-system, BlinkMacSystemFont, "Segoe UI", "Noto Sans CJK SC", "Microsoft YaHei", sans-serif; background: #f4f6f8; color: #1f2937; }
  main { max-width: 880px; margin: 0 auto; padding: 24px 16px 64px; }
  h1 { font-size: 20px; margin: 0 0 4px; }
  .sub { color: #6b7280; font-size: 13px; margin-bottom: 20px; }
  .card { background: #fff; border: 1px solid #e5e7eb; border-radius: 12px; padding: 16px; margin-bottom: 16px; }
  .row { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; }
  button { font: inherit; border: 0; border-radius: 8px; padding: 9px 16px; cursor: pointer; }
  button.primary { background: #2563eb; color: #fff; }
  button.danger { background: #dc2626; color: #fff; }
  button.secondary { background: #eef2f7; color: #1f2937; }
  button:disabled { opacity: .5; cursor: not-allowed; }
  input { font: inherit; border: 1px solid #d1d5db; border-radius: 8px; padding: 9px 12px; width: 100%; max-width: 360px; background: #fff; }
  label { font-size: 13px; color: #6b7280; display: block; margin-bottom: 6px; }
  .badge { display: inline-block; font-size: 12px; padding: 2px 8px; border-radius: 999px; background: #e5e7eb; color: #374151; }
  .badge.running { background: #fef3c7; color: #92400e; }
  .badge.success { background: #dcfce7; color: #166534; }
  .badge.failed { background: #fee2e2; color: #991b1b; }
  pre { background: #f9fafb; border: 1px solid #e5e7eb; border-radius: 8px; padding: 12px; overflow: auto; max-height: 360px; font: 12px/1.5 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; white-space: pre-wrap; word-break: break-word; }
  .kv { display: grid; grid-template-columns: 96px 1fr; gap: 4px 12px; font-size: 13px; }
  .kv div:nth-child(odd) { color: #6b7280; }
  .msg { margin-top: 10px; font-size: 13px; color: #6b7280; min-height: 1.4em; }
  .hidden { display: none !important; }
  code { font-size: 12px; background: #f3f4f6; padding: 1px 5px; border-radius: 4px; }
  @media (prefers-color-scheme: dark) {
    body { background: #0f172a; color: #e5e7eb; }
    .card { background: #111c33; border-color: #23314f; }
    input, pre, code { background: #0b1324; color: #e5e7eb; border-color: #23314f; }
    button.secondary { background: #1f2c47; color: #e5e7eb; }
    .sub, label, .msg, .kv div:nth-child(odd) { color: #94a3b8; }
  }
</style>
</head>
<body>
<main>
  <h1>汇英斋 · 服务端部署</h1>
  <div class="sub">从 GitHub 拉取最新 <code>server/</code> 代码并重启服务；<code>.env</code>、<code>data/</code>、<code>uploads/</code>、<code>.venv</code> 会保留，失败自动回滚。</div>

  <section id="login" class="card hidden">
    <label for="token">部署口令（仅保存在本浏览器会话）</label>
    <div class="row">
      <input id="token" type="password" autocomplete="off" placeholder="PANEL_TOKEN">
      <button id="save-token" class="primary">进入</button>
    </div>
    <div id="login-msg" class="msg"></div>
  </section>

  <section id="panel" class="hidden">
    <div class="card">
      <div class="row" style="justify-content: space-between">
        <div><span id="state-badge" class="badge">加载中</span></div>
        <button id="refresh" class="secondary">刷新</button>
      </div>
      <div class="kv" style="margin-top: 14px">
        <div>当前版本</div><div id="current">—</div>
        <div>上次部署</div><div id="last">—</div>
      </div>
      <div class="row" style="margin-top: 16px">
        <button id="deploy" class="danger">部署最新代码</button>
        <button id="logout" class="secondary">退出</button>
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
  const storeKey = 'hyz-deploy-token';
  let token = sessionStorage.getItem(storeKey) || '';
  let pollTimer = null;
  let forcePollUntil = 0;
  let busy = false;
  let lastLog = '';

  function api(path, options) {
    options = options || {};
    const headers = Object.assign({}, options.headers || {});
    if (token) headers['X-Deploy-Token'] = token;
    return fetch(path, Object.assign({ credentials: 'same-origin', cache: 'no-store' }, options, { headers }));
  }

  function showLogin(message) {
    $('login').classList.remove('hidden');
    $('panel').classList.add('hidden');
    $('login-msg').textContent = message || '';
  }

  function showPanel() {
    $('login').classList.add('hidden');
    $('panel').classList.remove('hidden');
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
    if (!token) return;
    let resp;
    try {
      resp = await api('api/status');
    } catch (err) {
      $('current').textContent = '无法连接部署面板';
      return;
    }
    if (resp.status === 401) {
      sessionStorage.removeItem(storeKey);
      token = '';
      stopPolling();
      showLogin('口令不正确或已失效');
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
        sessionStorage.removeItem(storeKey);
        token = '';
        showLogin('口令已失效');
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

  $('save-token').addEventListener('click', () => {
    const value = $('token').value.trim();
    if (!value) {
      $('login-msg').textContent = '请输入部署口令';
      return;
    }
    token = value;
    sessionStorage.setItem(storeKey, token);
    showPanel();
    loadStatus();
  });
  $('token').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') $('save-token').click();
  });
  $('refresh').addEventListener('click', loadStatus);
  $('deploy').addEventListener('click', deploy);
  $('logout').addEventListener('click', () => {
    sessionStorage.removeItem(storeKey);
    token = '';
    stopPolling();
    showLogin('');
  });
  $('toggle-log').addEventListener('click', () => {
    const el = $('log');
    el.classList.toggle('hidden');
    $('toggle-log').textContent = el.classList.contains('hidden') ? '显示' : '隐藏';
  });

  if (token) {
    showPanel();
    loadStatus();
  } else {
    showLogin('');
  }
})();
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "huiyingzhai-panel"
    sys_version = ""

    def log_message(self, fmt, *args):
        # 只记录访问行，不记录请求头（口令在请求头里）
        print("%s %s" % (self.address_string(), fmt % args), flush=True)

    def _route(self) -> str:
        path = urlparse(self.path).path
        if path == "/deploy":
            return "/"
        if path.startswith("/deploy/"):
            return path[len("/deploy"):]
        return path

    def _send(self, code, body, content_type="application/json; charset=utf-8"):
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
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _authorized(self) -> bool:
        if not TOKEN:
            self._send(
                503,
                json.dumps(
                    {"ok": False, "message": "面板未配置 PANEL_TOKEN"}, ensure_ascii=False
                ),
            )
            return False
        supplied = self.headers.get("X-Deploy-Token", "")
        try:
            valid = hmac.compare_digest(supplied, TOKEN)
        except TypeError:
            # 请求头里可能有非 ASCII 字符，直接判定为失败
            valid = False
        if valid:
            return True
        time.sleep(0.4)
        self._send(
            401, json.dumps({"ok": False, "message": "口令不正确"}, ensure_ascii=False)
        )
        return False

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
        path = self._route()
        if path in ("/", "/index.html"):
            self._send(200, HTML, "text/html; charset=utf-8")
            return
        if path == "/healthz":
            self._send(200, json.dumps({"ok": True}))
            return
        if path == "/api/status":
            if not self._authorized():
                return
            self._send(200, self._status_payload())
            return
        if path == "/api/log":
            if not self._authorized():
                return
            lines = _tail_text(LOG_FILE).splitlines()[-LOG_LINES:]
            self._send(200, "\n".join(lines), "text/plain; charset=utf-8")
            return
        self._send(404, json.dumps({"ok": False, "message": "接口不存在"}, ensure_ascii=False))

    def do_POST(self):
        global _last_request_at

        path = self._route()
        if path != "/api/deploy":
            self._send(404, json.dumps({"ok": False, "message": "接口不存在"}, ensure_ascii=False))
            return
        if not self._authorized():
            return

        # 只接受 JSON 空对象；自定义请求头 + JSON Content-Type 同时抵御跨站表单请求
        content_type = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if content_type not in ("", "application/json"):
            self._send(415, json.dumps({"ok": False, "message": "不支持的请求类型"}, ensure_ascii=False))
            return
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            length = 0
        if length > 4096:
            self.close_connection = True
            self._send(413, json.dumps({"ok": False, "message": "请求体过大"}, ensure_ascii=False))
            return
        if length:
            body = self.rfile.read(length)
            if body.strip() not in (b"", b"{}"):
                self._send(400, json.dumps({"ok": False, "message": "不支持参数"}, ensure_ascii=False))
                return

        with _lock:
            now = time.time()
            if now - _last_request_at < MIN_INTERVAL_SECONDS:
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
                    json.dumps({"requestedAt": _now_ms(), "client": self.client_address[0]}),
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
                    json.dumps(
                        {"ok": False, "message": "写入部署请求失败：%s" % exc},
                        ensure_ascii=False,
                    ),
                )
                return
            _last_request_at = now

        self._send(202, json.dumps({"ok": True, "message": "已提交部署请求"}, ensure_ascii=False))


def main():
    if not TOKEN:
        print("警告：PANEL_TOKEN 为空，面板会拒绝所有 API 请求", flush=True)
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    server.daemon_threads = True
    print("汇英斋部署面板已启动：http://127.0.0.1:%d" % PORT, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
