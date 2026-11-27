# 汇英斋自建后端入口：FastAPI + SQLite
from __future__ import annotations

import hmac
import json
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import auth, db as store, files, qqbot
from .api import SUBJECT_KEYS, SUBJECT_NAMES, cleanup_expired_notices, handle_action
from .config import config

INLINE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}


@asynccontextmanager
async def lifespan(_app: FastAPI):
    config.upload_dir.mkdir(parents=True, exist_ok=True)
    store.cleanup_sessions()
    try:
        await cleanup_expired_notices()
    except Exception as exc:
        print("清理过期通知失败", exc)
    print(f"汇英斋服务已启动：http://127.0.0.1:{config.port}")
    print(f"对外地址：{config.base_url or '未配置（请填写 BASE_URL）'}")
    print(f"数据目录：{config.data_dir}")
    print(f"附件目录：{config.upload_dir}")
    yield


app = FastAPI(
    title="汇英斋自建后端",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    return response


async def _json_body(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


def _bearer_token(request: Request) -> str:
    header = str(request.headers.get("authorization") or "")
    return header[7:].strip() if header.startswith("Bearer ") else ""


def _require_user(request: Request):
    openid, user = auth.resolve_user(_bearer_token(request))
    if not openid:
        return None, JSONResponse(
            {"ok": False, "msg": "登录已过期，请重新登录"}, status_code=401
        )
    if not user:
        return None, JSONResponse(
            {"ok": False, "msg": "账号档案不存在，请重新登录"}, status_code=401
        )
    return user, None


@app.get("/api/health")
async def health():
    return {
        "ok": True,
        "time": store.now_ms(),
        "baseUrl": config.base_url or None,
    }


@app.post("/api/qq/webhook")
async def qq_webhook(request: Request):
    """QQ 官方机器人回调：op=13 地址验证，op=0 事件（Ed25519 验签）。"""
    raw = await request.body()
    try:
        payload = json.loads(raw or b"{}")
    except ValueError:
        return JSONResponse({"ok": False, "msg": "invalid json"}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse({"ok": False, "msg": "invalid payload"}, status_code=400)

    if payload.get("op") == 13:
        ua = str(request.headers.get("user-agent") or "")
        appid = str(request.headers.get("x-bot-appid") or "")
        if not ua.startswith("QQBot-Callback") or (config.qq_app_id and appid != config.qq_app_id):
            return JSONResponse({"ok": False, "msg": "bad handshake"}, status_code=401)
        data = payload.get("d") if isinstance(payload.get("d"), dict) else {}
        try:
            result = qqbot.validation_response(
                str(data.get("plain_token") or ""), str(data.get("event_ts") or "")
            )
        except ValueError as exc:
            return JSONResponse({"ok": False, "msg": str(exc)}, status_code=503)
        return JSONResponse(result)

    timestamp = str(request.headers.get("x-signature-timestamp") or "")
    signature = str(request.headers.get("x-signature-ed25519") or "")
    if not qqbot.verify_signature(timestamp, raw, signature):
        return JSONResponse({"ok": False, "msg": "bad signature"}, status_code=401)
    if payload.get("op") == 0:
        qqbot.handle_event(payload)
    return JSONResponse({"op": 12})


def _panel_secret_ok(request: Request) -> bool:
    expected = config.web_panel_secret
    if not expected:
        return False
    supplied = str(request.headers.get("x-panel-secret") or "")
    try:
        return hmac.compare_digest(supplied, expected)
    except TypeError:
        return False


def _subject_list() -> list[dict]:
    return [
        {"key": key, "name": SUBJECT_NAMES.get(key, key)}
        for key in SUBJECT_KEYS
    ]


# 网页数据上传页专用：用共享密钥换取某个学号的会话
# 名单内的学号首次登录时自动派生网页身份
@app.post("/api/web/token")
async def web_token(request: Request):
    if not _panel_secret_ok(request):
        return JSONResponse({"ok": False, "msg": "网页上传未启用"}, status_code=403)
    body = await _json_body(request)
    student_id = str(body.get("studentId") or "").strip()
    if not student_id:
        return {"ok": False, "msg": "缺少学号"}
    roster = store.find_one("roster", lambda r: r.get("studentId") == student_id)
    user = store.find_one("users", lambda u: u.get("studentId") == student_id)

    if not user or not user.get("registered") or not user.get("_openid"):
        if not roster:
            return {"ok": False, "msg": "该学号不在班级名单"}
        position = (roster or {}).get("position") or "普通同学"
        if position in ("管理员", "班长"):
            role = "admin"
        elif position == "普通同学":
            role = "student"
        else:
            role = "committee"
        payload = {
            "name": roster.get("name") or "",
            "studentId": student_id,
            "className": roster.get("className") or "",
            "position": position,
            "role": role,
            "nickname": roster.get("name") or "",
            "registered": True,
            "webOnly": True,
            "updateTime": store.now_ms(),
        }
        if user:
            payload["_openid"] = user.get("_openid") or ("web:" + student_id)
            user = store.update("users", user.get("_id"), payload)
        else:
            payload["_openid"] = "web:" + student_id
            payload["createTime"] = store.now_ms()
            user = store.insert("users", payload)

    openid = user.get("_openid")
    if not openid:
        return {"ok": False, "msg": "该账号缺少网页身份"}
    token = store.create_session(openid)
    return {
        "ok": True,
        "token": token,
        "user": user,
        "roster": roster,
        "repSubjects": (roster or {}).get("repSubjects") or [],
        "subjects": _subject_list(),
    }


# 业务接口：网页通过分享密钥换取会话后调用
# 业务接口：网页版与上传页共用 { action, data }
@app.post("/api/action")
async def action(request: Request):
    user, error = _require_user(request)
    if error:
        return error

    body = await _json_body(request)
    action_name = body.get("action")
    if not action_name:
        return {"ok": False, "msg": "缺少 action"}
    try:
        return await handle_action(user, str(action_name), body.get("data") or {})
    except Exception as exc:
        print("api error", action_name, exc)
        return {"ok": False, "msg": str(exc) or "服务异常，请稍后重试"}


# 附件上传：multipart 字段 file，可选 dir
@app.post("/api/upload")
async def upload(
    request: Request, file: Optional[UploadFile] = File(None), dir: str = Form("misc")
):
    _user, error = _require_user(request)
    if error:
        return error
    try:
        result = await files.save_upload(file, dir)
        return {"ok": True, **result}
    except files.UploadTooLarge:
        return {"ok": False, "msg": f"文件超过 {config.max_upload_mb}MB 限制"}
    except ValueError as exc:
        return {"ok": False, "msg": str(exc) or "附件上传失败"}
    except Exception as exc:
        print("保存附件失败", exc)
        return {"ok": False, "msg": str(exc) or "附件上传失败"}


# 附件静态目录：图片可直接预览，其它类型强制下载，避免被当作网页执行
@app.get("/uploads/{full_path:path}")
async def serve_upload(full_path: str):
    parts = [part for part in full_path.split("/") if part]
    if not parts or any(part.startswith(".") for part in parts):
        return JSONResponse({"ok": False, "msg": "接口不存在"}, status_code=404)

    target = files.resolve_upload_path("/uploads/" + full_path)
    if not target or not target.is_file():
        return JSONResponse({"ok": False, "msg": "接口不存在"}, status_code=404)

    headers = {
        "Cache-Control": "public, max-age=604800",
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
    }
    if target.suffix.lower() not in INLINE_EXTS:
        headers["Content-Disposition"] = "attachment"
    return FileResponse(target, headers=headers)


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(_request: Request, exc: StarletteHTTPException):
    if exc.status_code == 404:
        return JSONResponse({"ok": False, "msg": "接口不存在"}, status_code=404)
    return JSONResponse(
        {"ok": False, "msg": str(exc.detail)}, status_code=exc.status_code
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(_request: Request, exc: Exception):
    print("未处理异常", exc)
    return JSONResponse(
        {"ok": False, "msg": "服务异常，请稍后重试"}, status_code=500
    )
